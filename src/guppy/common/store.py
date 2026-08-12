"""Shared SQLite state store.

Used by both the scheduler (poll/dedup/dispatch bookkeeping) and worker
containers (writing their own job's progress and final result). The DB file
lives on a Docker volume mounted into both, so every connection opens in
WAL mode with a busy_timeout to tolerate concurrent writers from separate
containers touching the same file.
"""

from __future__ import annotations

import enum
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_BUSY_TIMEOUT_MS = 10_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_issues (
    repo_slug     TEXT NOT NULL,
    issue_number  INTEGER NOT NULL,
    job_id        TEXT NOT NULL,
    processed_at  TEXT NOT NULL,
    PRIMARY KEY (repo_slug, issue_number)
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    repo_slug           TEXT NOT NULL,
    issue_number        INTEGER NOT NULL,
    issue_title         TEXT NOT NULL,
    status              TEXT NOT NULL,
    stage               TEXT,
    pr_url              TEXT,
    skip_reason         TEXT,
    error_message       TEXT,
    plan_artifact_path       TEXT,
    plan_review_artifact_path TEXT,
    diff_artifact_path       TEXT,
    log_path                 TEXT,
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    finished_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class Job:
    id: str
    repo_slug: str
    issue_number: int
    issue_title: str
    status: JobStatus
    stage: str | None = None
    pr_url: str | None = None
    skip_reason: str | None = None
    error_message: str | None = None
    plan_artifact_path: str | None = None
    plan_review_artifact_path: str | None = None
    diff_artifact_path: str | None = None
    log_path: str | None = None
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> "Job":
        data = dict(row)
        data["status"] = JobStatus(data["status"])
        return cls(**data)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # -- dedup -----------------------------------------------------------

    def is_issue_processed(self, repo_slug: str, issue_number: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_issues WHERE repo_slug = ? AND issue_number = ?",
                (repo_slug, issue_number),
            ).fetchone()
        return row is not None

    def _mark_issue_processed(
        self, conn: sqlite3.Connection, repo_slug: str, issue_number: int, job_id: str
    ) -> None:
        conn.execute(
            "INSERT INTO processed_issues (repo_slug, issue_number, job_id, processed_at) "
            "VALUES (?, ?, ?, ?)",
            (repo_slug, issue_number, job_id, _now()),
        )

    # -- job lifecycle -----------------------------------------------------

    def create_job(self, repo_slug: str, issue_number: int, issue_title: str) -> str:
        """Creates a job and marks the issue processed in the same
        transaction -- an issue is considered "processed" the instant a job
        exists for it, regardless of eventual outcome (see DESIGN.md: each
        qualifying issue processed exactly once, ever)."""
        job_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, repo_slug, issue_number, issue_title, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, repo_slug, issue_number, issue_title, JobStatus.QUEUED.value, _now()),
            )
            self._mark_issue_processed(conn, repo_slug, issue_number, job_id)
        return job_id

    def get_job(self, job_id: str) -> Job | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job._from_row(row) if row else None

    def get_queued_jobs(self) -> list[Job]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC",
                (JobStatus.QUEUED.value,),
            ).fetchall()
        return [Job._from_row(r) for r in rows]

    def count_running_jobs(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status = ?",
                (JobStatus.RUNNING.value,),
            ).fetchone()
        return row["n"]

    def mark_running(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?",
                (JobStatus.RUNNING.value, _now(), job_id),
            )

    def set_stage(self, job_id: str, stage: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE jobs SET stage = ? WHERE id = ?", (stage, job_id))

    def set_artifact(self, job_id: str, field_name: str, path: str) -> None:
        allowed = {
            "plan_artifact_path",
            "plan_review_artifact_path",
            "diff_artifact_path",
            "log_path",
        }
        if field_name not in allowed:
            raise ValueError(f"unknown artifact field: {field_name}")
        with self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {field_name} = ? WHERE id = ?", (path, job_id))

    def mark_succeeded(self, job_id: str, pr_url: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, pr_url = ?, finished_at = ? WHERE id = ?",
                (JobStatus.SUCCEEDED.value, pr_url, _now(), job_id),
            )

    def mark_skipped(self, job_id: str, reason: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, skip_reason = ?, finished_at = ? WHERE id = ?",
                (JobStatus.SKIPPED.value, reason, _now(), job_id),
            )

    def mark_failed(self, job_id: str, error_message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, error_message = ?, finished_at = ? WHERE id = ?",
                (JobStatus.FAILED.value, error_message, _now(), job_id),
            )
