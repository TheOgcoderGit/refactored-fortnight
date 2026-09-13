"""
ChannelFlow AI - Promotional Post Service
=============================================

Backing store for /post (one admin-picked target) and /broadcast (many
targets) - see spec sections 20-23. Both commands create one
promotional_posts row and one promotional_deliveries row per actual
target, so a partial failure ("31 Telegram sent, 2 failed") is a query,
not a guess.
"""

from database.db import get_connection


def create_promo_post(admin_id, kind, text_content, media_file_id, media_type, target_scope):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO promotional_posts
        (admin_id, kind, text_content, media_file_id, media_type, target_scope, status)
        VALUES (?, ?, ?, ?, ?, ?, 'DRAFT')
        """,
        (admin_id, kind, text_content, media_file_id, media_type, target_scope),
    )

    conn.commit()
    post_id = cur.lastrowid
    conn.close()

    return post_id


def set_post_status(post_id, status):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("UPDATE promotional_posts SET status=? WHERE id=?", (status, post_id))

    conn.commit()
    conn.close()


def record_delivery(post_id, project_id, destination_type, destination_ref, ok, error=None):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO promotional_deliveries
        (promo_post_id, project_id, destination_type, destination_ref, status, error)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            post_id,
            project_id,
            destination_type,
            destination_ref,
            "SENT" if ok else "FAILED",
            error,
        ),
    )

    conn.commit()
    conn.close()


def get_delivery_report(post_id):
    """Returns per-platform sent/failed counts for the confirmation
    summary shown after a /post or /broadcast run."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT destination_type, status, COUNT(*) as n
        FROM promotional_deliveries
        WHERE promo_post_id=?
        GROUP BY destination_type, status
        """,
        (post_id,),
    )

    rows = cur.fetchall()
    conn.close()

    report = {}

    for row in rows:
        platform = report.setdefault(row["destination_type"], {"sent": 0, "failed": 0})
        if row["status"] == "SENT":
            platform["sent"] = row["n"]
        else:
            platform["failed"] = row["n"]

    return report


def get_recent_promo_posts(limit=10):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM promotional_posts ORDER BY id DESC LIMIT ?", (limit,)
    )

    rows = cur.fetchall()
    conn.close()

    return rows
