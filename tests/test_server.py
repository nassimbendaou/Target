import http.client
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from server import TargetApp, make_handler
from catalog import JobCatalog, TEMPLATES


class TargetServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = JobCatalog(shutil.which("ansible-inventory"))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "jobs.sqlite3"
        self.app = TargetApp(self.database, shutil.which("true"), self.catalog)
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

    def test_catalog_exposes_real_inventory_hierarchy_and_membership(self):
        status, catalog = self.request("GET", "/api/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(
            {item["group"] for item in catalog["templates"]},
            {"Diagnostics", "Connectivity"},
        )
        inventory = catalog["inventories"][0]
        self.assertIn("development", inventory["groups"]["all"]["children"])
        self.assertEqual(inventory["groups"]["development"]["children"], ["control"])
        self.assertEqual(inventory["groups"]["control"]["hosts"], ["localhost"])
        self.assertEqual(inventory["groups"]["all"]["host_count"], 1)
        self.assertEqual(inventory["groups"]["ungrouped"]["host_count"], 0)
        host = inventory["hosts"][0]
        self.assertEqual(host["direct_groups"], ["control"])
        self.assertEqual(set(host["groups"]), {"all", "development", "control"})

    def test_selected_template_is_persisted_as_a_snapshot(self):
        status, launched = self.request(
            "POST",
            "/api/jobs",
            {"Content-Type": "application/json"},
            b'{"template_id":"local-connectivity"}',
        )
        self.assertEqual(status, 201)
        finished = self.wait_for_job(
            launched["id"], lambda job: job["status"] == "succeeded"
        )
        self.assertEqual(finished["template"]["id"], "local-connectivity")
        self.assertEqual(finished["template"]["inventory"]["id"], "local")
        self.assertEqual(finished["template"]["target_group"], "development")
        self.assertEqual(finished["template"]["target_hosts"], ["localhost"])
        with patch.dict(TEMPLATES["local-connectivity"], {"name": "Renamed template"}):
            restarted = TargetApp(self.database, shutil.which("true"), self.catalog)
            self.assertEqual(
                restarted.job(launched["id"])["template"]["name"],
                "Local connectivity check",
            )

    def test_rejects_unapproved_templates_and_launch_overrides(self):
        bodies = [
            ({"template_id": "unknown"}, 404),
            ({"template_id": "../../server.py"}, 404),
            ({"template_id": ["local-smoke"]}, 400),
            ({"template_id": "local-smoke", "inventory": "other.yml"}, 400),
            ({"playbook": "other.yml"}, 400),
            (["local-smoke"], 400),
        ]
        for body, expected in bodies:
            with self.subTest(body=body):
                status, _ = self.request(
                    "POST",
                    "/api/jobs",
                    {"Content-Type": "application/json"},
                    json.dumps(body).encode(),
                )
                self.assertEqual(status, expected)
        for body in [b'{"template_id":', b"x" * 1025]:
            self.assertEqual(
                self.request(
                    "POST", "/api/jobs", {"Content-Type": "application/json"}, body
                )[0],
                400,
            )
        self.assertEqual(self.app.jobs(), [])

    def test_migrates_history_without_changing_legacy_results(self):
        database = Path(self.directory.name) / "legacy.sqlite3"
        with sqlite3.connect(database) as db:
            db.execute(
                "CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, return_code INTEGER, output TEXT NOT NULL DEFAULT '')"
            )
            db.execute(
                "INSERT INTO jobs (id, status, created_at, return_code, output) VALUES ('old', 'succeeded', '2026-01-01', 0, 'Original output')"
            )
        migrated = TargetApp(database, shutil.which("true"), self.catalog).job("old")
        self.assertEqual(migrated["status"], "succeeded")
        self.assertEqual(migrated["output"], "Original output")
        self.assertEqual(migrated["template"]["inventory"]["id"], "localhost-inline")

    def test_approved_playbooks_run_against_their_inventory_groups(self):
        for template_id in TEMPLATES:
            with self.subTest(template=template_id):
                result = subprocess.run(
                    self.catalog.command(shutil.which("ansible-playbook"), template_id),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("localhost", result.stdout)
                self.assertIn("changed=0", result.stdout)

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
        restarted = TargetApp(self.database, shutil.which("true"), self.catalog)
        self.assertEqual(restarted.job("interrupted")["status"], "failed")

    def test_restart_marks_cancelling_run_failed(self):
        with self.app.connect() as db:
            db.execute(
                "INSERT INTO jobs (id, status, created_at) VALUES ('interrupted', 'cancelling', '2026-01-01')"
            )
        restarted = TargetApp(self.database, shutil.which("true"), self.catalog)
        self.assertEqual(restarted.job("interrupted")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
