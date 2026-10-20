import http.client
import json
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

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

    def test_restart_marks_interrupted_run_failed(self):
        with self.app.connect() as db:
            db.execute(
                "INSERT INTO jobs (id, status, created_at) VALUES ('interrupted', 'running', '2026-01-01')"
            )
        restarted = TargetApp(self.database, shutil.which("true"))
        self.assertEqual(restarted.job("interrupted")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
