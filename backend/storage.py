"""SQLite schema, recovery, transactions, and job-history queries."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .catalog import LEGACY_TEMPLATE


def now():
    return datetime.now(timezone.utc).isoformat()


def decode_job(row):
    job = dict(row)
    job["template"] = (
        json.loads(job["template"]) if job["template"] else LEGACY_TEMPLATE
    )
    return job


def record_event(db, job_id, event):
    db.execute(
        "INSERT INTO job_events (job_id, event, occurred_at) VALUES (?, ?, ?)",
        (job_id, event, now()),
    )


class JobStore:
    def __init__(self, database):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database.parent.chmod(0o700)
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
            db.execute("""CREATE TABLE IF NOT EXISTS job_inputs (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id),
                files TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id),
                event TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS job_events_by_job ON job_events(job_id, id)"
            )
            db.execute(
                "UPDATE jobs SET template = ? WHERE template IS NULL",
                (json.dumps(LEGACY_TEMPLATE),),
            )
            interrupted = db.execute(
                """UPDATE jobs SET status = 'failed', finished_at = ?,
                output = output || '\nTarget stopped before this run finished.'
                WHERE status IN ('queued', 'running', 'cancelling') RETURNING id""",
                (now(),),
            ).fetchall()
            for row in interrupted:
                record_event(db, row["id"], "interrupted")
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
            db.execute("BEGIN")
            jobs = {
                row["id"]: {**decode_job(row), "events": []}
                for row in db.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100"
                )
            }
            for row in db.execute(
                """SELECT * FROM job_events WHERE job_id IN (
                SELECT id FROM jobs ORDER BY created_at DESC LIMIT 100
                ) ORDER BY id"""
            ):
                event = dict(row)
                jobs[event.pop("job_id")]["events"].append(event)
            return list(jobs.values())

    def job(self, job_id):
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return None
            job = decode_job(row)
            job["events"] = [
                dict(event)
                for event in db.execute(
                    "SELECT id, event, occurred_at FROM job_events WHERE job_id = ? ORDER BY id",
                    (job_id,),
                )
            ]
            return job
