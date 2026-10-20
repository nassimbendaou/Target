"""Local Target prototype: one fixed Ansible job and its execution history."""

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


def now():
    return datetime.now(timezone.utc).isoformat()


class TargetApp:
    def __init__(self, database, ansible_bin):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database.parent.chmod(0o700)
        self.ansible_bin = ansible_bin
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                return_code INTEGER,
                output TEXT NOT NULL DEFAULT ''
            )""")
            db.execute(
                """UPDATE jobs SET status = 'failed', finished_at = ?,
                output = output || '\nTarget stopped before this run finished.'
                WHERE status IN ('queued', 'running')""",
                (now(),),
            )
        self.database.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def jobs(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100"
                )
            ]

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def launch(self):
        with self.lock:
            with self.connect() as db:
                active = db.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('queued', 'running') LIMIT 1"
                ).fetchone()
                if active:
                    return None
                job_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO jobs (id, status, created_at) VALUES (?, 'queued', ?)",
                    (job_id, now()),
                )
            threading.Thread(target=self.run, args=(job_id,), daemon=True).start()
            return self.job(job_id)

    def run(self, job_id):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
                (now(), job_id),
            )

        command = [
            self.ansible_bin,
            "-i",
            "localhost,",
            "-c",
            "local",
            "playbooks/smoke.yml",
        ]
        environment = os.environ.copy()
        environment.update(
            ANSIBLE_CONFIG=str(ROOT / "ansible.cfg"), ANSIBLE_NOCOLOR="1"
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                start_new_session=True,
            )
            try:
                output, _ = process.communicate(timeout=90)
                status = "succeeded" if process.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                output, _ = process.communicate()
                output += "\nTarget stopped this run after 90 seconds."
                status = "timed_out"
            return_code = process.returncode
        except (OSError, subprocess.SubprocessError) as error:
            output = f"Could not start Ansible: {error}"
            status = "failed"
            return_code = None

        with self.connect() as db:
            db.execute(
                """UPDATE jobs SET status = ?, finished_at = ?, return_code = ?, output = ?
                WHERE id = ?""",
                (status, now(), return_code, output[-100_000:], job_id),
            )


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, body, content_type="application/json; charset=utf-8"):
            data = (
                json.dumps(body).encode()
                if content_type.startswith("application/json")
                else body
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def allowed_host(self):
            return self.headers.get("Host") in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }

        def do_GET(self):
            if not self.allowed_host():
                return self.respond(403, {"error": "Invalid host"})
            path = urlsplit(self.path).path
            if path == "/api/jobs":
                return self.respond(200, app.jobs())
            if path.startswith("/api/jobs/"):
                job = app.job(path.removeprefix("/api/jobs/"))
                return (
                    self.respond(200, job)
                    if job
                    else self.respond(404, {"error": "Job not found"})
                )
            if path in STATIC:
                filename, content_type = STATIC[path]
                return self.respond(200, (ROOT / filename).read_bytes(), content_type)
            return self.respond(404, {"error": "Not found"})

        def do_POST(self):
            if not self.allowed_host():
                return self.respond(403, {"error": "Invalid host"})
            origin = self.headers.get("Origin")
            if origin and origin not in {
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            }:
                return self.respond(403, {"error": "Invalid origin"})
            if urlsplit(self.path).path != "/api/jobs" or self.headers.get(
                "Content-Length"
            ) not in (None, "0"):
                return self.respond(400, {"error": "Unsupported request"})
            job = app.launch()
            return (
                self.respond(201, job)
                if job
                else self.respond(409, {"error": "A job is already running"})
            )

    return Handler


def main():
    ansible_bin = shutil.which("ansible-playbook")
    if not ansible_bin:
        raise SystemExit(
            "ansible-playbook is required; run with nix develop --command python server.py"
        )
    server = ThreadingHTTPServer(("127.0.0.1", 8000), BaseHTTPRequestHandler)
    app = TargetApp(ROOT / ".data" / "target.sqlite3", ansible_bin)
    server.RequestHandlerClass = make_handler(app)
    print("Target is ready at http://127.0.0.1:8000", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
