import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from server import TargetApp, make_handler
from catalog import JobCatalog, TEMPLATES, ROOT


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

    def queue_without_running(self):
        with patch.object(self.app, "run"):
            return self.app.launch()

    def assert_events(self, job, expected):
        self.assertEqual([event["event"] for event in job["events"]], expected)
        ids = [event["id"] for event in job["events"]]
        self.assertEqual(ids, sorted(set(ids)))
        for event in job["events"]:
            self.assertEqual(set(event), {"id", "event", "occurred_at"})
            self.assertTrue(event["occurred_at"].endswith("+00:00"))

    def test_events_persist_and_match_list_and_detail_without_crossing_jobs(self):
        first = self.queue_without_running()
        self.assert_events(first, ["queued"])
        self.app.run(first["id"])
        second = self.queue_without_running()
        self.app.cancel(second["id"])
        self.assertEqual(self.app.cancel(second["id"])[0], 409)
        _, detail = self.request("GET", f"/api/jobs/{first['id']}")
        self.assert_events(detail, ["queued", "started", "succeeded"])
        _, jobs = self.request("GET", "/api/jobs")
        listed = {job["id"]: job for job in jobs}
        self.assertEqual(listed[first["id"]], detail)
        self.assert_events(listed[second["id"]], ["queued", "cancelled"])
        self.assertTrue(
            {event["id"] for event in detail["events"]}.isdisjoint(
                event["id"] for event in listed[second["id"]]["events"]
            )
        )
        restarted = TargetApp(self.database, shutil.which("true"), self.catalog)
        self.assertEqual(restarted.job(first["id"]), detail)

    def test_event_insert_failure_rolls_back_the_launch_and_snapshot(self):
        with patch(
            "server.record_event", side_effect=sqlite3.OperationalError("disk full")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.app.launch()
        self.assertEqual(self.app.jobs(), [])
        with self.app.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM job_inputs").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM job_events").fetchone()[0], 0
            )

    def test_failed_process_and_startup_are_recorded_without_false_success(self):
        for binary, expected in [
            (shutil.which("false"), ["queued", "started", "failed"]),
            ("/nonexistent/target-ansible", ["queued", "failed"]),
        ]:
            with self.subTest(binary=binary):
                self.app.ansible_bin = binary
                launched = self.queue_without_running()
                self.app.run(launched["id"])
                job = self.app.job(launched["id"])
                self.assertEqual(job["status"], "failed")
                self.assert_events(job, expected)

    def test_restart_records_interruption_once_without_inventing_a_start(self):
        launched = self.queue_without_running()
        for _ in range(2):
            restarted = TargetApp(self.database, shutil.which("true"), self.catalog)
            job = restarted.job(launched["id"])
            self.assertEqual(job["status"], "failed")
            self.assertIsNone(job["started_at"])
            self.assert_events(job, ["queued", "interrupted"])

    def test_cancel_during_preparation_does_not_record_a_false_failure(self):
        launched = self.queue_without_running()

        def cancel_then_fail(directory):
            self.app.cancel(launched["id"])
            raise OSError("preparation failed after cancellation")

        with patch("server.execution_environment", side_effect=cancel_then_fail):
            self.app.run(launched["id"])
        job = self.app.job(launched["id"])
        self.assertEqual(job["status"], "cancelled")
        self.assert_events(job, ["queued", "cancelled"])

    def test_list_keeps_only_the_latest_100_jobs_and_their_own_events(self):
        with self.app.connect() as db:
            for index in range(101):
                job_id = str(index)
                created = f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00"
                db.execute(
                    "INSERT INTO jobs (id, status, created_at) VALUES (?, 'succeeded', ?)",
                    (job_id, created),
                )
                db.execute(
                    "INSERT INTO job_events (job_id, event, occurred_at) VALUES (?, 'succeeded', ?)",
                    (job_id, created),
                )
        jobs = self.app.jobs()
        self.assertEqual(
            [job["id"] for job in jobs], [str(i) for i in range(100, 0, -1)]
        )
        for job in jobs:
            self.assert_events(job, ["succeeded"])
            self.assertEqual(job["events"][0]["occurred_at"], job["created_at"])

    def test_runner_receives_only_allowed_environment_and_private_directories(self):
        self.program(
            "import json, os\n"
            "from pathlib import Path\n"
            "print(json.dumps({'env': dict(os.environ), 'modes': {name: Path(os.environ[name]).stat().st_mode & 0o777 for name in ['HOME', 'TMPDIR']}}))"
        )
        ambient = {
            "AWS_SECRET_ACCESS_KEY": "not-for-the-runner",
            "GITHUB_TOKEN": "not-for-the-runner",
            "SSH_AUTH_SOCK": "/ambient/agent",
            "PYTHONPATH": "/ambient/python",
            "ANSIBLE_CONFIG": "/ambient/ansible.cfg",
            "ANSIBLE_CALLBACK_PLUGINS": "/ambient/plugins",
            "ANSIBLE_HOME": "/ambient/ansible",
            "ANSIBLE_REMOTE_TEMP": "/ambient/modules",
        }
        launched = self.queue_without_running()
        with (
            patch.dict(os.environ, ambient),
            patch("server.subprocess.Popen", wraps=subprocess.Popen) as started,
        ):
            self.app.run(launched["id"])
        passed_environment = started.call_args.kwargs["env"]
        finished = self.app.job(launched["id"])
        self.assertEqual(finished["status"], "succeeded", finished["output"])
        captured = json.loads(finished["output"])
        environment = captured["env"]
        for name in ambient:
            self.assertNotEqual(environment.get(name), ambient[name], name)
        self.assertEqual(
            set(passed_environment),
            {
                "PATH",
                "HOME",
                "TMPDIR",
                "ANSIBLE_HOME",
                "ANSIBLE_LOCAL_TEMP",
                "ANSIBLE_REMOTE_TEMP",
                "ANSIBLE_CONFIG",
                "ANSIBLE_NOCOLOR",
                "PYTHONUNBUFFERED",
            }
            | (set(os.environ) & {"LANG", "LC_ALL", "LC_CTYPE"}),
        )
        self.assertEqual(captured["modes"], {"HOME": 0o700, "TMPDIR": 0o700})
        for name, value in passed_environment.items():
            self.assertEqual(environment[name], value)
        self.assertEqual(environment["PATH"], os.environ["PATH"])
        for name in ("HOME", "TMPDIR"):
            self.assertFalse(Path(environment[name]).exists())

    def test_inventory_uses_the_same_clean_environment_and_cleans_up(self):
        captured = {}
        original_run = subprocess.run

        def inspect_run(*args, **kwargs):
            captured.update(kwargs["env"])
            self.assertTrue(Path(captured["HOME"]).is_dir())
            self.assertTrue(Path(captured["TMPDIR"]).is_dir())
            return original_run(*args, **kwargs)

        with (
            patch.dict(
                os.environ,
                {
                    "AWS_SECRET_ACCESS_KEY": "not-for-inventory",
                    "ANSIBLE_CONFIG": "/missing/config",
                    "ANSIBLE_INVENTORY": "/missing/inventory",
                    "ANSIBLE_STDOUT_CALLBACK": "missing_callback",
                },
            ),
            patch("catalog.subprocess.run", side_effect=inspect_run),
        ):
            catalog = JobCatalog(shutil.which("ansible-inventory"))
        self.assertEqual(catalog.group_hosts("all"), ["localhost"])
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", captured)
        self.assertNotIn("ANSIBLE_INVENTORY", captured)
        self.assertNotIn("ANSIBLE_STDOUT_CALLBACK", captured)
        self.assertNotEqual(captured["ANSIBLE_CONFIG"], "/missing/config")
        self.assertFalse(Path(captured["HOME"]).exists())
        self.assertFalse(Path(captured["TMPDIR"]).exists())

    def test_run_uses_frozen_files_after_repository_and_catalog_change(self):
        repository = Path(self.directory.name) / "repository"
        for name in [
            "inventories/local.yml",
            "ansible.cfg",
            "playbooks/smoke.yml",
            "playbooks/connectivity.yml",
        ]:
            destination = repository / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        with (
            patch("catalog.ROOT", repository),
            patch("catalog.INVENTORY_SOURCE", repository / "inventories/local.yml"),
        ):
            catalog = JobCatalog(shutil.which("ansible-inventory"))
        originals = catalog.execution_files("local-smoke")
        self.app.catalog = catalog
        for name in ["inventories/local.yml", "ansible.cfg", "playbooks/smoke.yml"]:
            (repository / name).write_text("changed before launch")
        self.program(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "print(json.dumps({'files': {'inventory.yml': Path(sys.argv[2]).read_text(), 'playbook.yml': Path(sys.argv[-1]).read_text(), 'ansible.cfg': Path(os.environ['ANSIBLE_CONFIG']).read_text()}, 'directory': str(Path.cwd()), 'limit': sys.argv[sys.argv.index('--limit') + 1]}))"
        )
        launched = self.queue_without_running()
        self.app.catalog = self.catalog
        with patch.dict(
            self.catalog._templates["local-smoke"],
            {"target_group": "changed-after-queue"},
        ):
            self.app.run(launched["id"])
        finished = self.app.job(launched["id"])
        self.assertEqual(finished["status"], "succeeded", finished["output"])
        captured = json.loads(finished["output"])
        self.assertEqual(captured["files"], originals)
        self.assertEqual(captured["limit"], "control")
        self.assertFalse(Path(captured["directory"]).exists())
        self.assertEqual(
            finished["template"]["revision"], launched["template"]["revision"]
        )
        for name, content in originals.items():
            self.assertEqual(
                finished["template"]["source_hashes"][name],
                sha256(content.encode()).hexdigest(),
            )

    def test_changed_files_or_target_fail_closed_before_process_start(self):
        for change in ["files", "target"]:
            with self.subTest(change=change):
                launched = self.queue_without_running()
                with self.app.connect() as db:
                    if change == "files":
                        files = self.catalog.execution_files("local-smoke")
                        files["playbook.yml"] = "altered playbook"
                        db.execute(
                            "UPDATE job_inputs SET files = ? WHERE job_id = ?",
                            (json.dumps(files), launched["id"]),
                        )
                    else:
                        template = launched["template"]
                        template["target_group"] = "altered"
                        db.execute(
                            "UPDATE jobs SET template = ? WHERE id = ?",
                            (json.dumps(template), launched["id"]),
                        )
                with patch("server.subprocess.Popen") as process:
                    self.app.run(launched["id"])
                    process.assert_not_called()
                job = self.app.job(launched["id"])
                self.assertEqual(job["status"], "failed")
                self.assertIn("integrity verification", job["output"])
                self.assert_events(job, ["queued", "failed"])

    def test_missing_execution_snapshot_does_not_fall_back_to_repository(self):
        launched = self.queue_without_running()
        with self.app.connect() as db:
            db.execute("DELETE FROM job_inputs WHERE job_id = ?", (launched["id"],))
        with patch("server.subprocess.Popen") as process:
            self.app.run(launched["id"])
            process.assert_not_called()
        self.assertEqual(self.app.job(launched["id"])["status"], "failed")

    def test_source_contents_are_not_returned_as_job_metadata(self):
        launched = self.queue_without_running()
        status, job = self.request("GET", f"/api/jobs/{launched['id']}")
        self.assertEqual(status, 200)
        self.assertNotIn("files", job)
        self.assertNotIn("ansible_python_interpreter", json.dumps(job))
        self.assertEqual(len(job["template"]["revision"]), 64)
        self.app.cancel(launched["id"])

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
        with patch.dict(
            self.catalog._templates["local-connectivity"], {"name": "Renamed template"}
        ):
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
        with closing(sqlite3.connect(database)) as db, db:
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
        self.assertNotIn("revision", migrated["template"])
        self.assertEqual(migrated["events"], [])

    def test_approved_playbooks_run_against_their_inventory_groups(self):
        self.app.ansible_bin = shutil.which("ansible-playbook")
        for template_id in TEMPLATES:
            with self.subTest(template=template_id):
                launched = self.app.launch(template_id)
                finished = self.wait_for_job(
                    launched["id"], lambda job: job["finished_at"] is not None
                )
                self.assertEqual(finished["status"], "succeeded", finished["output"])
                self.assertIn("localhost", finished["output"])
                self.assertIn("changed=0", finished["output"])

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
        self.assert_events(finished, ["queued", "started", "timed_out"])

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
        self.assert_events(
            job, ["queued", "started", "cancellation_requested", "cancelled"]
        )
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
