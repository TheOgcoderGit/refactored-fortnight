"""
ChannelFlow AI - Persistent Job Queue
=======================================

Database-backed job queue that survives restarts. Jobs are claimed
atomically via UPDATE ... WHERE status='pending' (row-level locking via
SQLite's write lock), so multiple workers can share the queue safely.

Job types: forward, ai_process, media_process, publish, renew,
notification, giveaway_delivery, broadcast.

Workers poll this table. Each worker process handles one job type
family, so a crashed AI worker doesn't block forwarding.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from database.db import get_connection

logger = logging.getLogger(__name__)

# Retry policy per job type: (max_attempts, base_delay_seconds)
RETRY_POLICIES = {
    "forward": (5, 10),
    "ai_process": (3, 15),
    "media_process": (3, 20),
    "publish": (5, 30),
    "renew": (3, 60),
    "notification": (5, 5),
    "giveaway_delivery": (3, 30),
    "broadcast": (2, 60),
}

DEFAULT_POLICY = (3, 30)

# Error classification -> retryable?
PERMANENT_ERRORS = {"AUTHENTICATION", "INVALID_REQUEST", "PERMISSION_DENIED", "CONTENT_UNSUPPORTED", "PERMANENT"}


class Job:
    """Lightweight wrapper over a job row."""

    def __init__(self, row):
        self.id = row["id"]
        self.type = row["type"]
        self.payload = json.loads(row["payload"] or "{}")
        self.status = row["status"]
        self.priority = row["priority"]
        self.attempts = row["attempts"]
        self.max_attempts = row["max_attempts"]
        self.next_run_at = row["next_run_at"]
        self.error = row["error"]

    def __repr__(self):
        return f"<Job #{self.id} {self.type} attempt={self.attempts}/{self.max_attempts}>"


def enqueue(job_type: str, payload: dict, priority: int = 0, run_at: datetime = None, max_attempts: int = None) -> int:
    """Adds a job to the queue. Returns the job id.

    priority: higher runs first within the same scheduling pass.
    run_at: for scheduled jobs (None = as soon as a worker is free)."""

    if max_attempts is None:
        max_attempts = RETRY_POLICIES.get(job_type, DEFAULT_POLICY)[0]

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO jobs(type, payload, status, priority, attempts, max_attempts, next_run_at)
        VALUES (?, ?, 'pending', ?, 0, ?, ?)
        """,
        (
            job_type,
            json.dumps(payload),
            priority,
            max_attempts,
            (run_at or datetime.now(timezone.utc)).isoformat(),
        ),
    )

    conn.commit()
    job_id = cur.lastrowid
    conn.close()

    return job_id


def claim_next(worker_id: str, job_types=None) -> Job:
    """Atomically claims the next runnable job.

    Order: highest priority first, then oldest. Only picks jobs whose
    next_run_at has passed. Marks it 'running' and stamps the worker."""

    conn = get_connection()
    cur = conn.cursor()

    type_filter = ""
    params = [worker_id]

    if job_types:
        placeholders = ",".join("?" for _ in job_types)
        type_filter = f"AND type IN ({placeholders})"
        params.extend(job_types)

    now_iso = datetime.now(timezone.utc).isoformat()
    params.append(now_iso)

    # SQLite serializes writes; UPDATE ... WHERE with subselect claims
    # exactly one row even with concurrent workers.
    cur.execute(
        f"""
        UPDATE jobs SET
            status='running',
            locked_at=CURRENT_TIMESTAMP,
            locked_by=?,
            attempts=attempts+1
        WHERE id = (
            SELECT id FROM jobs
            WHERE status='pending'
              AND next_run_at <= ?
              {type_filter}
            ORDER BY priority DESC, id ASC
            LIMIT 1
        )
        RETURNING *
        """,
        params,
    )

    row = cur.fetchone()

    if row is None:
        # Fallback for SQLite builds without RETURNING support
        conn.rollback()
        cur.execute(
            f"""
            SELECT id FROM jobs
            WHERE status='pending' AND next_run_at <= ?
              {type_filter}
            ORDER BY priority DESC, id ASC LIMIT 1
            """,
            [now_iso] + ([jt for jt in job_types] if job_types else []),
        )
        id_row = cur.fetchone()
        if id_row is None:
            conn.commit()
            conn.close()
            return None

        cur.execute(
            """
            UPDATE jobs SET status='running', locked_at=CURRENT_TIMESTAMP,
                   locked_by=?, attempts=attempts+1
            WHERE id=? AND status='pending'
            """,
            (worker_id, id_row["id"]),
        )
        if cur.rowcount == 0:
            conn.commit()
            conn.close()
            return None

        cur.execute("SELECT * FROM jobs WHERE id=?", (id_row["id"],))
        row = cur.fetchone()

    conn.commit()
    conn.close()

    return Job(row) if row else None


def complete(job_id: int, result_note: str = None):
    """Marks a job done."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE jobs SET status='completed', completed_at=CURRENT_TIMESTAMP,
               error=?, locked_by=NULL, locked_at=NULL
        WHERE id=?
        """,
        (result_note, job_id),
    )

    conn.commit()
    conn.close()


def fail(job_id: int, error_code: str = None, error_message: str = "", retryable: bool = True):
    """Handles failure: retry with exponential backoff or dead-letter."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
    row = cur.fetchone()

    if row is None:
        conn.close()
        return

    if not retryable or error_code in PERMANENT_ERRORS:
        # Permanent - straight to dead letter
        _move_to_dead_letter(cur, row, error_code, error_message)
    elif row["attempts"] >= row["max_attempts"]:
        # Retries exhausted - dead letter
        _move_to_dead_letter(cur, row, error_code, error_message)
    else:
        # Schedule retry with exponential backoff
        _, base_delay = RETRY_POLICIES.get(row["type"], DEFAULT_POLICY)
        delay = min(base_delay * (2 ** (row["attempts"] - 1)), 3600)
        next_run = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()

        cur.execute(
            """
            UPDATE jobs SET status='pending', next_run_at=?, locked_by=NULL,
                   locked_at=NULL, error=? WHERE id=?
            """,
            (next_run, f"{error_code}: {error_message}"[:500], job_id),
        )

    conn.commit()
    conn.close()


def _move_to_dead_letter(cur, job_row, error_code, error_message):

    cur.execute(
        """
        INSERT INTO dead_letter_jobs(job_id, type, payload, attempts, error_code, error_message)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            job_row["id"], job_row["type"], job_row["payload"],
            job_row["attempts"], error_code, error_message[:1000],
        ),
    )

    cur.execute(
        "UPDATE jobs SET status='dead_letter', completed_at=CURRENT_TIMESTAMP, "
        "error=?, locked_by=NULL, locked_at=NULL WHERE id=?",
        (f"{error_code}: {error_message}"[:500], job_row["id"]),
    )


def retry_dead_letter(dead_letter_id: int) -> int:
    """Admin manual retry - re-enqueues a dead-lettered job."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM dead_letter_jobs WHERE id=?", (dead_letter_id,))
    dl = cur.fetchone()

    if dl is None:
        conn.close()
        return 0

    new_job_id = enqueue(dl["type"], json.loads(dl["payload"] or "{}"))

    cur.execute(
        "UPDATE dead_letter_jobs SET retried_as_job_id=?, resolved_at=CURRENT_TIMESTAMP WHERE id=?",
        (new_job_id, dead_letter_id),
    )

    conn.commit()
    conn.close()

    return new_job_id


def recover_stuck_jobs(older_than_minutes: int = 30):
    """Requeues jobs stuck in 'running' past the threshold (crashed worker).
    Called from the subscription scheduler loop."""

    conn = get_connection()
    cur = conn.cursor()

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()

    cur.execute(
        """
        UPDATE jobs SET status='pending', locked_by=NULL, locked_at=NULL
        WHERE status='running'
          AND locked_at IS NOT NULL
          AND locked_at <= ?
        """,
        (cutoff,),
    )
    recovered = cur.rowcount

    conn.commit()
    conn.close()

    if recovered:
        logger.warning("Recovered %s stuck jobs (running > %smin)", recovered, older_than_minutes)

    return recovered


def get_queue_stats():
    """Queue depth by status/type for health dashboard."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT status, COUNT(*) as count FROM jobs GROUP BY status
    """)
    by_status = {r["status"]: r["count"] for r in cur.fetchall()}

    cur.execute("""
        SELECT type, COUNT(*) as count FROM jobs
        WHERE status IN ('pending', 'running')
        GROUP BY type
    """)
    pending_by_type = {r["type"]: r["count"] for r in cur.fetchall()}

    cur.execute("SELECT COUNT(*) FROM dead_letter_jobs WHERE resolved_at IS NULL")
    dead_letters = cur.fetchone()[0]

    conn.close()

    return {
        "by_status": by_status,
        "pending_by_type": pending_by_type,
        "unresolved_dead_letters": dead_letters,
    }