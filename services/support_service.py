"""
ChannelFlow AI - Support Ticket Service
=========================================

Replaces the static "contact @ChannelFlowSupport_bot" with a real
ticket system (Prompt 1 §17):

    User:   create ticket -> category -> subject -> message
            -> view own tickets -> reply -> close
    Admin:  inbox by status -> assign -> reply (user-visible) ->
            internal note -> change status -> reopen
"""

import logging

from database.db import get_connection

logger = logging.getLogger(__name__)

CATEGORIES = ("general", "billing", "forwarding", "connection", "feature")
STATUSES = ("open", "pending", "in_progress", "resolved", "closed")

STATUS_ICONS = {
    "open": "🔴", "pending": "🟡", "in_progress": "🔵",
    "resolved": "✅", "closed": "⚫",
}


def create_ticket(user_id, category, subject, message, media_file_id=None):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO support_tickets(user_id, category, subject)
        VALUES (?, ?, ?)
        """,
        (user_id, category if category in CATEGORIES else "general", subject[:200]),
    )
    ticket_id = cur.lastrowid

    cur.execute(
        """
        INSERT INTO support_messages(ticket_id, sender_id, sender_type, message, media_file_id)
        VALUES (?, ?, 'user', ?, ?)
        """,
        (ticket_id, user_id, message[:4000], media_file_id),
    )

    conn.commit()
    conn.close()

    return ticket_id


def get_ticket(ticket_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM support_tickets WHERE id=?", (ticket_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_messages(ticket_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM support_messages WHERE ticket_id=? ORDER BY id ASC",
        (ticket_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def list_user_tickets(user_id, limit=10):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM support_tickets WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def list_tickets_by_status(status=None, limit=20):
    conn = get_connection()
    cur = conn.cursor()
    if status:
        cur.execute(
            "SELECT * FROM support_tickets WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, limit),
        )
    else:
        cur.execute("SELECT * FROM support_tickets ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def add_message(ticket_id, sender_id, sender_type, message,
                media_file_id=None, internal_note=False):
    """Appends a reply. User replies flip an admin-answered ticket back
    to 'pending' so admins notice; admin replies set it to in_progress."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO support_messages(ticket_id, sender_id, sender_type, message, media_file_id, is_internal_note)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ticket_id, sender_id, sender_type, message[:4000], media_file_id,
         1 if internal_note else 0),
    )

    new_status = None
    if not internal_note:
        new_status = "pending" if sender_type == "user" else "in_progress"
        cur.execute(
            "UPDATE support_tickets SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (ticket_id,),
        )
        _apply_status(cur, ticket_id, new_status)

    conn.commit()
    conn.close()


def _apply_status(cur, ticket_id, status):
    if status in STATUSES:
        cur.execute(
            "UPDATE support_tickets SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, ticket_id),
        )


def set_status(ticket_id, status):
    if status not in STATUSES:
        raise ValueError(f"Invalid status {status}")

    conn = get_connection()
    cur = conn.cursor()
    _apply_status(cur, ticket_id, status)
    conn.commit()
    conn.close()


def assign(ticket_id, admin_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE support_tickets SET assigned_admin=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (admin_id, ticket_id),
    )
    conn.commit()
    conn.close()


def open_count():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM support_tickets WHERE status IN ('open','pending')")
    n = cur.fetchone()[0]
    conn.close()
    return n