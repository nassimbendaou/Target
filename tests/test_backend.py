import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import server
from backend.storage import JobStore


class ServerStartupTest(unittest.TestCase):
    def test_entry_point_wires_the_same_local_api_and_closes_on_interrupt(self):
        with (
            patch(
                "backend.server.shutil.which", return_value="/trusted/ansible-playbook"
            ),
            patch("backend.server.TargetApp") as application,
            patch("backend.server.ThreadingHTTPServer") as http_server,
            patch("builtins.print"),
        ):
            http_server.return_value.serve_forever.side_effect = KeyboardInterrupt
            server.main()
        application.assert_called_once_with(
            server.ROOT / ".data" / "target.sqlite3", "/trusted/ansible-playbook"
        )
        http_server.assert_called_once_with(
            ("127.0.0.1", 8000), server.BaseHTTPRequestHandler
        )
        self.assertTrue(
            issubclass(
                http_server.return_value.RequestHandlerClass,
                server.BaseHTTPRequestHandler,
            )
        )
        http_server.return_value.server_close.assert_called_once()

    def test_missing_ansible_exits_without_opening_a_database_or_listener(self):
        with (
            patch("backend.server.shutil.which", return_value=None),
            patch("backend.server.TargetApp") as application,
            patch("backend.server.ThreadingHTTPServer") as http_server,
        ):
            with self.assertRaisesRegex(SystemExit, "nix develop"):
                server.main()
        application.assert_not_called()
        http_server.assert_not_called()


class JobStoreTest(unittest.TestCase):
    def test_store_works_without_loading_ansible_or_starting_threads(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("backend.catalog.subprocess.run") as inventory,
            patch("backend.jobs.threading.Thread") as worker,
        ):
            store = JobStore(Path(directory) / "backend.jobs.sqlite3")
            self.assertEqual(store.jobs(), [])
            self.assertIsNone(store.job("missing"))
        inventory.assert_not_called()
        worker.assert_not_called()

    def test_failed_transaction_rolls_back_before_the_next_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "backend.jobs.sqlite3")
            with self.assertRaisesRegex(ValueError, "abort"):
                with store.connect() as db:
                    db.execute(
                        "INSERT INTO jobs (id, status, created_at) VALUES ('rolled-back', 'queued', '2026-01-01')"
                    )
                    raise ValueError("abort")
            self.assertEqual(store.jobs(), [])
