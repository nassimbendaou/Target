"""Job lifecycle, captured inputs, and cancellable local execution."""

import codecs
import json
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path

from .catalog import JobCatalog
from .environment import execution_environment
from .storage import JobStore, now, record_event

RUN_TIMEOUT_SECONDS = 90
OUTPUT_LIMIT = 100_000


class TargetApp:
    def __init__(self, database, ansible_bin, catalog=None):
        self.catalog = catalog or JobCatalog(shutil.which("ansible-inventory"))
        self.store = JobStore(database)
        self.ansible_bin = ansible_bin
        self.lock = threading.Lock()
        self.processes = {}

    def jobs(self):
        return self.store.jobs()

    def job(self, job_id):
        return self.store.job(job_id)

    def launch(self, template_id="local-smoke", actor=None):
        template = self.catalog.template(template_id)
        files = self.catalog.execution_files(template_id)
        with self.lock:
            with self.store.connect() as db:
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
                db.execute(
                    "INSERT INTO job_inputs (job_id, files) VALUES (?, ?)",
                    (job_id, json.dumps(files)),
                )
                record_event(db, job_id, "queued", actor)
            threading.Thread(target=self.run, args=(job_id,), daemon=True).start()
            return self.job(job_id)

    def run(self, job_id):
        job = self.job(job_id)
        if job["status"] != "queued":
            return
        with ExitStack() as resources:
            try:
                with self.store.connect() as db:
                    row = db.execute(
                        "SELECT files FROM job_inputs WHERE job_id = ?", (job_id,)
                    ).fetchone()
                if not row:
                    raise ValueError("No captured inputs for this run; start a new run")
                files = json.loads(row["files"])
                if (
                    not isinstance(files, dict)
                    or set(files) != {"inventory.yml", "playbook.yml", "ansible.cfg"}
                    or not all(isinstance(content, str) for content in files.values())
                ):
                    raise ValueError("Invalid execution snapshot")
                template = job["template"].copy()
                revision = template.pop("revision")
                if sha256(
                    json.dumps(template, sort_keys=True).encode("utf-8")
                ).hexdigest() != revision or any(
                    sha256(content.encode("utf-8")).hexdigest()
                    != template["source_hashes"][name]
                    for name, content in files.items()
                ):
                    raise ValueError("Execution snapshot failed integrity verification")
                directory = Path(
                    resources.enter_context(
                        tempfile.TemporaryDirectory(prefix="target-run-")
                    )
                )
                for name, content in files.items():
                    path = directory / name
                    path.write_bytes(content.encode("utf-8"))
                    path.chmod(0o600)
                environment = execution_environment(directory)
            except (OSError, ValueError, KeyError, TypeError) as error:
                with self.lock, self.store.connect() as db:
                    changed = db.execute(
                        "UPDATE jobs SET status = 'failed', finished_at = ?, output = ? WHERE id = ? AND status = 'queued'",
                        (
                            now(),
                            f"Could not prepare execution snapshot: {error}",
                            job_id,
                        ),
                    )
                    if changed.rowcount:
                        record_event(db, job_id, "failed")
                return
            self._execute(job_id, directory, environment)

    def _execute(self, job_id, directory, environment):
        with self.lock:
            job = self.job(job_id)
            if job["status"] != "queued":
                return
            command = [
                self.ansible_bin,
                "-i",
                str(directory / "inventory.yml"),
                "-c",
                "local",
                "--limit",
                job["template"]["target_group"],
                str(directory / "playbook.yml"),
            ]
            try:
                process = subprocess.Popen(
                    command,
                    cwd=directory,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except (OSError, subprocess.SubprocessError) as error:
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE jobs SET status = 'failed', finished_at = ?, output = ? WHERE id = ?",
                        (now(), f"Could not start Ansible: {error}", job_id),
                    )
                    record_event(db, job_id, "failed")
                return
            self.processes[job_id] = process
            with self.store.connect() as db:
                db.execute(
                    "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
                    (now(), job_id),
                )
                record_event(db, job_id, "started")

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
                    with self.store.connect() as db:
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
            with self.store.connect() as db:
                db.execute(
                    """UPDATE jobs SET status = ?, finished_at = ?, return_code = ?, output = ?
                    WHERE id = ?""",
                    (status, now(), process.returncode, output[-OUTPUT_LIMIT:], job_id),
                )
                record_event(db, job_id, status)

    def cancel(self, job_id, actor=None):
        with self.lock:
            job = self.job(job_id)
            if not job:
                return 404, None
            if job["status"] == "queued":
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE jobs SET status = 'cancelled', finished_at = ?, output = ? WHERE id = ?",
                        (now(), "Target cancelled this run before it started.", job_id),
                    )
                    record_event(db, job_id, "cancelled", actor)
            elif job["status"] == "running":
                process = self.processes[job_id]
                if process.poll() is not None:
                    return 409, job
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE jobs SET status = 'cancelling' WHERE id = ?", (job_id,)
                    )
                    record_event(db, job_id, "cancellation_requested", actor)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif job["status"] != "cancelling":
                return 409, job
            return 202, self.job(job_id)
