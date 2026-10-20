"""Local Target prototype: approved Ansible workflows and execution history."""

import codecs
import json
import os
import selectors
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from catalog import JobCatalog, LEGACY_TEMPLATE

ROOT = Path(__file__).resolve().parent
RUN_TIMEOUT_SECONDS = 90
OUTPUT_LIMIT = 100_000


def now():
    return datetime.now(timezone.utc).isoformat()


def decode_job(row):
    job = dict(row)
    job["template"] = (
        json.loads(job["template"]) if job["template"] else LEGACY_TEMPLATE
    )
    return job


class TargetApp:
    def __init__(self, database, ansible_bin, catalog=None):
        self.catalog = catalog or JobCatalog(shutil.which("ansible-inventory"))
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database.parent.chmod(0o700)
        self.ansible_bin = ansible_bin
        self.lock = threading.Lock()
        self.processes = {}
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
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "template" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN template TEXT")
            db.execute(
                "UPDATE jobs SET template = ? WHERE template IS NULL",
                (json.dumps(LEGACY_TEMPLATE),),
            )
            db.execute(
                """UPDATE jobs SET status = 'failed', finished_at = ?,
                output = output || '\nTarget stopped before this run finished.'
                WHERE status IN ('queued', 'running', 'cancelling')""",
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
                decode_job(row)
                for row in db.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100"
                )
            ]

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return decode_job(row) if row else None

    def launch(self, template_id="local-smoke"):
        template = self.catalog.template(template_id)
        with self.lock:
            with self.connect() as db:
                active = db.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('queued', 'running', 'cancelling') LIMIT 1"
                ).fetchone()
                if active:
                    return None
                job_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO jobs (id, status, created_at, template) VALUES (?, 'queued', ?, ?)",
                    (job_id, now(), json.dumps(template)),
                )
            threading.Thread(target=self.run, args=(job_id,), daemon=True).start()
            return self.job(job_id)

    def run(self, job_id):
        environment = os.environ.copy()
        environment.update(
            ANSIBLE_CONFIG=str(ROOT / "ansible.cfg"),
            ANSIBLE_NOCOLOR="1",
            PYTHONUNBUFFERED="1",
        )
        with self.lock:
            job = self.job(job_id)
            if job["status"] != "queued":
                return
            command = self.catalog.command(self.ansible_bin, job["template"]["id"])
            try:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except (OSError, subprocess.SubprocessError) as error:
                with self.connect() as db:
                    db.execute(
                        "UPDATE jobs SET status = 'failed', finished_at = ?, output = ? WHERE id = ?",
                        (now(), f"Could not start Ansible: {error}", job_id),
                    )
                return
            self.processes[job_id] = process
            with self.connect() as db:
                db.execute(
                    "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
                    (now(), job_id),
                )

        output = ""
        persisted_output = ""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
        next_flush = 0
        timed_out = False
        with process.stdout, selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                if not timed_out and time.monotonic() >= deadline:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    timed_out = True
                for key, _ in selector.select(timeout=0.25):
                    chunk = os.read(key.fd, 8192)
                    if chunk:
                        output = (output + decoder.decode(chunk))[-OUTPUT_LIMIT:]
                    else:
                        selector.unregister(key.fileobj)
                if output != persisted_output and time.monotonic() >= next_flush:
                    with self.connect() as db:
                        db.execute(
                            "UPDATE jobs SET output = ? WHERE id = ?", (output, job_id)
                        )
                    persisted_output = output
                    next_flush = time.monotonic() + 0.25
        process.wait()
        output = (output + decoder.decode(b"", final=True))[-OUTPUT_LIMIT:]
        status = "succeeded" if process.returncode == 0 else "failed"
        if timed_out:
            output += f"\nTarget stopped this run after {RUN_TIMEOUT_SECONDS} seconds."
            status = "timed_out"

        with self.lock:
            self.processes.pop(job_id)
            if self.job(job_id)["status"] == "cancelling":
                status = "cancelled"
                output += "\nTarget cancelled this run."
            with self.connect() as db:
                db.execute(
                    """UPDATE jobs SET status = ?, finished_at = ?, return_code = ?, output = ?
                    WHERE id = ?""",
                    (status, now(), process.returncode, output[-OUTPUT_LIMIT:], job_id),
                )

    def cancel(self, job_id):
        with self.lock:
            job = self.job(job_id)
            if not job:
                return 404, None
            if job["status"] == "queued":
                with self.connect() as db:
                    db.execute(
                        "UPDATE jobs SET status = 'cancelled', finished_at = ?, output = ? WHERE id = ?",
                        (now(), "Target cancelled this run before it started.", job_id),
                    )
            elif job["status"] == "running":
                process = self.processes[job_id]
                if process.poll() is not None:
                    return 409, job
                with self.connect() as db:
                    db.execute(
                        "UPDATE jobs SET status = 'cancelling' WHERE id = ?", (job_id,)
                    )
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif job["status"] != "cancelling":
                return 409, job
            return 202, self.job(job_id)


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
            if path == "/api/catalog":
                return self.respond(200, app.catalog.describe())
            if path == "/api/jobs":
                return self.respond(200, app.jobs())
            if path.startswith("/api/jobs/"):
                job = app.job(path.removeprefix("/api/jobs/"))
                return (
                    self.respond(200, job)
                    if job
                    else self.respond(404, {"error": "Job not found"})
                )
            return self.respond(404, {"error": "Not found"})

        def do_POST(self):
            if not self.allowed_host():
                return self.respond(403, {"error": "Invalid host"})
            origin = self.headers.get("Origin")
            if origin and origin not in {
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
                "http://127.0.0.1:5173",
                "http://localhost:5173",
                "http://127.0.0.1:4173",
                "http://localhost:4173",
            }:
                return self.respond(403, {"error": "Invalid origin"})
            path = urlsplit(self.path).path
            length = self.headers.get("Content-Length", "0")
            if (
                self.headers.get("Transfer-Encoding")
                or len(length) > 4
                or not length.isascii()
                or not length.isdigit()
                or int(length) > 1024
            ):
                return self.respond(400, {"error": "Unsupported request"})
            length = int(length)
            if path == "/api/jobs":
                template_id = "local-smoke"
                if length:
                    if (
                        self.headers.get("Content-Type", "").split(";")[0].strip()
                        != "application/json"
                    ):
                        return self.respond(400, {"error": "Expected JSON"})
                    try:
                        self.connection.settimeout(5)
                        parameters = json.loads(self.rfile.read(length))
                    except (ValueError, UnicodeDecodeError, OSError):
                        return self.respond(400, {"error": "Invalid JSON"})
                    if (
                        not isinstance(parameters, dict)
                        or set(parameters) != {"template_id"}
                        or not isinstance(parameters["template_id"], str)
                    ):
                        return self.respond(
                            400, {"error": "Choose an approved template"}
                        )
                    template_id = parameters["template_id"]
                try:
                    job = app.launch(template_id)
                except KeyError:
                    return self.respond(404, {"error": "Unknown job template"})
                return (
                    self.respond(201, job)
                    if job
                    else self.respond(409, {"error": "A job is already running"})
                )
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                if length:
                    return self.respond(400, {"error": "Unsupported request"})
                job_id = path.removeprefix("/api/jobs/").removesuffix("/cancel")
                if not job_id or "/" in job_id:
                    return self.respond(400, {"error": "Unsupported request"})
                status, job = app.cancel(job_id)
                if status == 404:
                    return self.respond(404, {"error": "Job not found"})
                if status == 409:
                    return self.respond(409, {"error": "Job is no longer running"})
                return self.respond(202, job)
            return self.respond(400, {"error": "Unsupported request"})

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
    print("Target API is ready at http://127.0.0.1:8000", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
