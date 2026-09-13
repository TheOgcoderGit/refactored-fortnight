"""
ChannelFlow AI - Processing Job Service
==========================================

State machine for posts detected in a project's processing_chat_id
(the external affiliate-converter bot's output channel), on their way
to an Instagram destination.

    DETECTED -> PROCESSING -> READY -> QUEUED -> PUBLISHING -> PUBLISHED
                                  \\                  \\
                                   -> SKIPPED          -> FAILED -> RETRY -> ...
                                                                      \\
                                                                       -> FAILED_PERMANENTLY

For broadcast_channel targets, "QUEUED"/"READY" is where a job sits
in the Ready-to-Publish approval queue until an admin taps "Mark
Published" in the bot - there is no automated PUBLISHING step for
those, by design (see destinations/instagram_destination.py).

Duplicate protection is NOT primarily this module's job - it's the
UNIQUE(project_id, converter_chat_id, converter_message_id) constraint
on processing_jobs (database/db.py). create_job() below just turns the
IntegrityError that constraint raises into a clean "already exists,
here's the existing row" result instead of a crash, so a restart,
retry, or duplicate Telegram update can call this function as many
times as it wants for the same message and only ever get one job.
"""

import json
import re
import sqlite3

from database.db import get_connection

URL_RE = re.compile(r"https?://\S+")

TERMINAL_STATUSES = ("PUBLISHED", "FAILED_PERMANENTLY", "SKIPPED")
MAX_ATTEMPTS = 5


def extract_urls(text: str):
    """Read-only extraction for logging/formatting/dedupe purposes.
    Returned URLs are never modified - see the module docstring in
    destinations/instagram_destination.py and spec section 8: 'DO NOT
    let AI modify affiliate URLs'."""

    if not text:
        return []

    return URL_RE.findall(text)


def create_job(
    project_id,
    converter_chat_id,
    converter_message_id,
    source_chat_id=None,
    source_message_id=None,
    media_type=None,
    media_count=0,
    text_content=None,
):
    """Returns (job_id, created) - created=False means this converter
    message was already seen and no new row was inserted."""

    urls = extract_urls(text_content)

    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO processing_jobs
            (project_id, source_chat_id, source_message_id,
             converter_chat_id, converter_message_id,
             media_type, media_count, text_content, detected_urls, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'DETECTED')
            """,
            (
                project_id,
                source_chat_id,
                source_message_id,
                converter_chat_id,
                converter_message_id,
                media_type,
                media_count,
                text_content,
                json.dumps(urls),
            ),
        )

        conn.commit()
        job_id = cur.lastrowid
        created = True

    except sqlite3.IntegrityError:

        cur.execute(
            """
            SELECT id FROM processing_jobs
            WHERE project_id=? AND converter_chat_id=? AND converter_message_id=?
            """,
            (project_id, converter_chat_id, converter_message_id),
        )
        job_id = cur.fetchone()["id"]
        created = False

    conn.close()

    return job_id, created


def set_status(job_id, status, error=None, destination_id=None):

    conn = get_connection()
    cur = conn.cursor()

    fields = ["status=?", "updated_at=CURRENT_TIMESTAMP"]
    values = [status]

    if error is not None:
        fields.append("error=?")
        values.append(error)

    if destination_id is not None:
        fields.append("destination_id=?")
        values.append(destination_id)

    if status == "PUBLISHED":
        fields.append("published_at=CURRENT_TIMESTAMP")

    values.append(job_id)

    cur.execute(f"UPDATE processing_jobs SET {', '.join(fields)} WHERE id=?", values)

    conn.commit()
    conn.close()


def record_attempt(job_id, status, error=None):
    """Appends to publish_logs and increments processing_jobs.attempts.
    Call this once per real publish/queue attempt (not per status read)."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT attempts FROM processing_jobs WHERE id=?", (job_id,))
    row = cur.fetchone()
    attempt_number = (row["attempts"] if row else 0) + 1

    cur.execute(
        "UPDATE processing_jobs SET attempts=? WHERE id=?",
        (attempt_number, job_id),
    )

    cur.execute(
        "INSERT INTO publish_logs (job_id, attempt, status, error) VALUES (?, ?, ?, ?)",
        (job_id, attempt_number, status, error),
    )

    conn.commit()
    conn.close()

    return attempt_number


def should_retry(job_id) -> bool:

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT attempts FROM processing_jobs WHERE id=?", (job_id,))
    row = cur.fetchone()
    conn.close()

    return bool(row) and row["attempts"] < MAX_ATTEMPTS


def get_job(job_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM processing_jobs WHERE id=?", (job_id,))
    row = cur.fetchone()

    conn.close()

    return row


def get_approval_queue(project_id=None, limit=20):
    """Jobs sitting in READY status - the Ready-to-Publish queue an
    admin reviews and manually marks as published inside Instagram."""

    conn = get_connection()
    cur = conn.cursor()

    if project_id:
        cur.execute(
            "SELECT * FROM processing_jobs WHERE project_id=? AND status='READY' "
            "ORDER BY id ASC LIMIT ?",
            (project_id, limit),
        )
    else:
        cur.execute(
            "SELECT * FROM processing_jobs WHERE status='READY' ORDER BY id ASC LIMIT ?",
            (limit,),
        )

    rows = cur.fetchall()
    conn.close()

    return rows


def get_retryable_jobs(limit=20):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM processing_jobs WHERE status='RETRY' AND attempts<? "
        "ORDER BY id ASC LIMIT ?",
        (MAX_ATTEMPTS, limit),
    )

    rows = cur.fetchall()
    conn.close()

    return rows


def get_project_job_counts(project_id):
    """Powers the project dashboard's Processed/Published/Failed line
    (spec section 18)."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT COUNT(*) FROM processing_jobs WHERE project_id=?", (project_id,)
    )
    processed = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM processing_jobs WHERE project_id=? AND status='PUBLISHED'",
        (project_id,),
    )
    published = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM processing_jobs WHERE project_id=? AND status IN "
        "('FAILED', 'FAILED_PERMANENTLY')",
        (project_id,),
    )
    failed = cur.fetchone()[0]

    conn.close()

    return {"processed": processed, "published": published, "failed": failed}
