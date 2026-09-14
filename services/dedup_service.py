"""
ChannelFlow AI - Message Deduplication Service
================================================

Prevents duplicate delivery of the same source message to the same
destination. Identity = deterministic hash of:

    source_platform + source_account_id + source_channel_id
    + source_message_id + project_id + destination_id

Backed by a DB table with a UNIQUE constraint, so dedup survives:
worker restart, server restart, retry, duplicate webhook, queue replay.

Usage (from forwarder/pipeline):
    if not dedup_service.claim(source_msg_id, project_id, dest_id, ...):
        continue  # already forwarded - skip silently
    ... do the actual forwarding ...
    dedup_service.confirm(claim_id)   # or it auto-expires
"""

import hashlib
import logging
import sqlite3

from database.db import get_connection

logger = logging.getLogger(__name__)

# How long an unconfirmed claim stays reserved (seconds) before another
# attempt is allowed - protects against crashed workers leaving stale locks.
CLAIM_TTL_SECONDS = 3600  # 1 hour


def compute_content_hash(text=None, media_ref=None):
    """SHA-256 of normalized content - used as a secondary identity for
    detecting near-duplicates across different message IDs."""

    parts = [text or "", media_ref or ""]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _identity_key(source_platform, source_account_id, source_channel_id,
                  source_message_id, project_id, destination_id):

    raw = "|".join(str(p) for p in [
        source_platform,
        source_account_id,
        source_channel_id,
        source_message_id,
        project_id,
        destination_id,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def claim(source_platform="telegram", source_account_id=None, source_channel_id=None,
          source_message_id=None, project_id=None, destination_id=None,
          content_hash=None) -> int:
    """Atomically reserves this message->destination pair.

    Returns claim id (>0) if newly claimed and forwarding should proceed.
    Returns 0 if this exact delivery was already recorded (duplicate)."""

    key = _identity_key(
        source_platform, source_account_id, source_channel_id,
        source_message_id, project_id, destination_id,
    )

    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO message_deduplication(
                dedup_key, source_platform, source_account_id,
                source_channel_id, source_message_id,
                project_id, destination_id, content_hash, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'claimed')
            """,
            (
                key, source_platform, str(source_account_id),
                str(source_channel_id), source_message_id,
                project_id, destination_id, content_hash,
            ),
        )
        conn.commit()
        claim_id = cur.lastrowid
        conn.close()
        return claim_id

    except sqlite3.IntegrityError:
        # UNIQUE constraint hit -> duplicate delivery
        conn.close()
        return 0


def confirm(claim_id: int, delivered_message_id: str = None):
    """Marks a claim as fully delivered."""

    if not claim_id:
        return

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE message_deduplication
        SET status='delivered',
            delivered_at=CURRENT_TIMESTAMP,
            delivered_message_id=?
        WHERE id=?
        """,
        (delivered_message_id, claim_id),
    )

    conn.commit()
    conn.close()


def release(claim_id: int):
    """Releases a claim without recording delivery (e.g. permanent
    failure where a later retry SHOULD be allowed)."""

    if not claim_id:
        return

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("DELETE FROM message_deduplication WHERE id=? AND status='claimed'", (claim_id,))
    conn.commit()
    conn.close()


def cleanup_expired_claims():
    """Do not replay ambiguous sends automatically. Operator reconciliation needed.

    The legacy TTL deletion could duplicate posts sent before a process crash.
    Explicit known failures still release their claims normally.
    """
    return 0


def get_duplicate_count_today() -> int:
    """How many duplicates were blocked today (for analytics)."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT COUNT(*) FROM message_deduplication "
        "WHERE status='claimed' AND created_at >= date('now')"
    )
    count = cur.fetchone()[0]
    conn.close()

    return count


def was_forwarded(source_platform, source_account_id, source_channel_id,
                  source_message_id, project_id, destination_id) -> bool:
    """Read-only check - did we already deliver this? Use claim() in the
    actual pipeline; this exists for admin inspection/tests."""

    key = _identity_key(
        source_platform, source_account_id, source_channel_id,
        source_message_id, project_id, destination_id,
    )

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM message_deduplication WHERE dedup_key=?", (key,))
    row = cur.fetchone()
    conn.close()

    return row is not None