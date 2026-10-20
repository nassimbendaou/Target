import http.client
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from server import TargetApp, make_handler


class TargetServerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "jobs.sqlite3"
        self.app = TargetApp(self.database, shutil.which("true"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        with self.app.lock:
            active = list(self.app.processes)
        for job_id in active:
            self.app.cancel(job_id)
            self.wait_for_job(job_id, lambda job: job["finished_at"] is not None)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, method, path, headers=None, body=b""):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(
            method, path, body=body if method == "POST" else None, headers=headers or {}
        )
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def program(self, source):
        program = Path(self.directory.name) / "runner"
        program.write_text(f"#!{sys.executable}\n{source}\n")
        program.chmod(0o700)
        self.app.ansible_bin = str(program)

    def wait_for_job(self, job_id, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.app.job(job_id)
            if predicate(job):
                return job
            time.sleep(0.02)
        self.fail(f"Job did not reach the expected state: {job}")

    def test_output_is_available_before_job_finishes(self):
        release = Path(self.directory.name) / "release"
        self.program(
            "import os, time\n"
            "from pathlib import Path\n"
            "os.write(1, b'live stdout')\n"
            "os.write(2, b' + stderr')\n"
            f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
            "print(' finished')"
        )
        _, launched = self.request("POST", "/api/jobs")
        self.wait_for_job(launched["id"], lambda job: "stderr" in job["output"])
        _, live = self.request("GET", f"/api/jobs/{launched['id']}")
        self.assertEqual(live["status"], "running")
        self.assertIsNone(live["finished_at"])
        self.assertEqual(live["output"], "live stdout + stderr")
        release.touch()
        finished = self.wait_for_job(
            launched["id"], lambda job: job["status"] == "succeeded"
        )
        self.assertEqual(finished["output"], "live stdout + stderr finished\n")

    def test_output_keeps_a_bounded_unicode_tail(self):
        self.program(
            "import sys\n"
            "sys.stdout.buffer.write(('x' * 100000 + '€' * 40000 + 'tail').encode())"
        )
        _, launched = self.request("POST", "/api/jobs")
        finished = self.wait_for_job(
            launched["id"], lambda job: job["status"] == "succeeded"
        )
        self.assertEqual(finished["output"], "x" * 59996 + "€" * 40000 + "tail")

    def test_timeout_still_applies_after_output_pipe_closes(self):
        self.program(
            "import os, time\n"
            "os.write(1, b'before timeout')\n"
            "os.close(1)\n"
            "os.close(2)\n"
            "time.sleep(30)"
        )
        with patch("server.RUN_TIMEOUT_SECONDS", 2):
            _, launched = self.request("POST", "/api/jobs")
            finished = self.wait_for_job(
                launched["id"], lambda job: job["status"] == "timed_out"
            )
        self.assertTrue(finished["output"].startswith("before timeout"))
        self.assertIn("Target stopped this run", finished["output"])
        self.assertNotEqual(finished["return_code"], 0)

    def test_launch_persists_real_process_result(self):
        status, launched = self.request("POST", "/api/jobs")
        self.assertEqual(status, 201)
        for _ in range(50):
            _, job = self.request("GET", f"/api/jobs/{launched['id']}")
            if job["status"] == "succeeded":
                break
            time.sleep(0.02)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["return_code"], 0)
        self.assertEqual(self.request("GET", "/api/jobs")[1][0]["id"], launched["id"])

    def test_rejects_cross_origin_launch(self):
        status, _ = self.request("POST", "/api/jobs", {"Origin": "https://example.com"})
        self.assertEqual(status, 403)
        self.assertEqual(self.request("GET", "/api/jobs")[1], [])

    def test_rejects_job_parameters(self):
        status, _ = self.request("POST", "/api/jobs", body=b'{"playbook":"other.yml"}')
        self.assertEqual(status, 400)
        self.assertEqual(self.request("GET", "/api/jobs")[1], [])

    def test_rejects_parallel_job(self):
        with self.app.connect() as db:
            db.execute(
                "INSERT INTO jobs (id, status, created_at) VALUES ('active', 'running', '2026-01-01')"
            )
        status, _ = self.request("POST", "/api/jobs")
        self.assertEqual(status, 409)

    def test_cancel_queued_job_before_start(self):
        with self.app.connect() as db:
            db.execute(
                "INSERT INTO jobs (id, status, created_at) VALUES ('queued', 'queued', '2026-01-01')"
            )
        status, job = self.request("POST", "/api/jobs/queued/cancel")
        self.assertEqual(status, 202)
        self.assertEqual(job["status"], "cancelled")
        self.app.run("queued")
        self.assertIsNone(self.app.job("queued")["started_at"])
        self.assertEqual(self.request("POST", "/api/jobs/queued/cancel")[0], 409)

    def test_cancel_running_process_and_allow_next_run(self):
        sleeper = Path(self.directory.name) / "sleeper"
        sleeper.write_text("#!/bin/sh\nprintf 'before cancellation'\nexec sleep 30\n")
        sleeper.chmod(0o700)
        self.app.ansible_bin = str(sleeper)
        status, launched = self.request("POST", "/api/jobs")
        self.assertEqual(status, 201)
        job = self.wait_for_job(
            launched["id"], lambda job: "before cancellation" in job["output"]
        )
        self.assertEqual(job["status"], "running")
        status, _ = self.request("POST", f"/api/jobs/{launched['id']}/cancel")
        self.assertEqual(status, 202)
        for _ in range(100):
            _, job = self.request("GET", f"/api/jobs/{launched['id']}")
            if job["status"] == "cancelled":
                break
            time.sleep(0.02)
        self.assertEqual(job["status"], "cancelled")
        self.assertIsNotNone(job["finished_at"])
        self.assertNotEqual(job["return_code"], 0)
        self.assertTrue(job["output"].startswith("before cancellation"))
        self.app.ansible_bin = shutil.which("true")
        self.assertEqual(self.request("POST", "/api/jobs")[0], 201)

    def test_cancel_rejects_unknown_job_and_cross_origin(self):
        path = "/api/jobs/missing/cancel"
        self.assertEqual(self.request("POST", path)[0], 404)
        self.assertEqual(
            self.request("POST", path, {"Origin": "https://example.com"})[0], 403
        )

    def test_restart_marks_interrupted_run_failed(self):
        with self.app.connect() as db:
            db.execute(
                "INSERT INTO jobs (id, status, created_at) VALUES ('interrupted', 'running', '2026-01-01')"
            )
        restarted = TargetApp(self.database, shutil.which("true"))
        self.assertEqual(restarted.job("interrupted")["status"], "failed")

    def test_restart_marks_cancelling_run_failed(self):
        with self.app.connect() as db:
            db.execute(
                "INSERT INTO jobs (id, status, created_at) VALUES ('interrupted', 'cancelling', '2026-01-01')"
            )
        restarted = TargetApp(self.database, shutil.which("true"))
        self.assertEqual(restarted.job("interrupted")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
