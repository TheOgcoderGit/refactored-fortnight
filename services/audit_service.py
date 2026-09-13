"""
ChannelFlow AI - Admin RBAC + Audit Log Service
=================================================

Two-level model (Prompt 1 §15):

    OWNER   - env-configured via ADMIN_IDS. Implicitly has every
              permission; can manage other admins.
    ADMIN   - a row in `admins` with granular permissions in
              `admin_permissions`. Every sensitive callback checks
              require_permission() SERVER-SIDE - hiding buttons in the
              UI is cosmetic only.

Every mutating admin action goes through log_action() -> admin_audit_logs
(append-only; no update/delete paths exist anywhere).
"""

import json
import logging

from config import ADMIN_IDS
from database.db import get_connection

logger = logging.getLogger(__name__)

# Canonical permission list (kept in sync with db.py schema comment)
PERMISSIONS = (
    "dashboard.view", "payments.view", "payments.approve", "payments.reject",
    "wallet.view", "wallet.adjust",
    "users.view", "users.suspend", "users.ban",
    "projects.view", "projects.manage",
    "plans.view", "plans.edit",
    "coupons.view", "coupons.manage",
    "giveaways.view", "giveaways.manage",
    "broadcast.send",
    "support.view", "support.manage",
    "analytics.view",
    "platforms.manage",
    "audit.view",
    "admins.manage",
)

# Default grants for a newly-added normal admin: read-mostly
DEFAULT_GRANTS = (
    "dashboard.view", "payments.view", "wallet.view", "users.view",
    "projects.view", "plans.view", "coupons.view", "giveaways.view",
    "support.view", "analytics.view",
)


def is_owner(admin_id) -> bool:
    return admin_id in ADMIN_IDS


def is_admin(admin_id) -> bool:
    """Owner OR an active row in admins."""

    if is_owner(admin_id):
        return True

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM admins WHERE telegram_id=? AND is_active=1", (admin_id,))
    ok = cur.fetchone() is not None
    conn.close()
    return ok


def get_permissions(admin_id) -> set:
    """Effective permission set. Owner = everything."""

    if is_owner(admin_id):
        return set(PERMISSIONS)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT permission FROM admin_permissions WHERE admin_id=? AND granted=1",
        (admin_id,),
    )
    perms = {r["permission"] for r in cur.fetchall()}
    conn.close()
    return perms


def has_permission(admin_id, permission) -> bool:
    if is_owner(admin_id):
        return True
    return permission in get_permissions(admin_id)


def require_permission(admin_id, permission) -> bool:
    """Alias with the audit-friendly name used by handlers."""
    return has_permission(admin_id, permission)


def can(admin_id, permission) -> bool:
    """Convenience gate used by legacy (non-panel) admin commands:
    True for the owner (env) or an active admin granted `permission`.
    This is the single place legacy commands should check so DB admins
    are not silently locked out and owners keep full access."""
    return has_permission(admin_id, permission)


def gate(admin_id, permission) -> bool:
    """Same as can(), named for readability in command handlers:
    `if not RBAC.gate(user.id, "payments.approve"): deny`."""
    return has_permission(admin_id, permission)


# ==========================================
# ADMIN MANAGEMENT (owner-only)
# ==========================================

def add_admin(telegram_id, username=None, added_by=None):
    """Idempotent add with default read permissions. The users row must
    exist BEFORE the admins insert (FK target) - order matters inside
    the same transaction."""

    conn = get_connection()
    cur = conn.cursor()

    # FK target first
    cur.execute(
        "INSERT OR IGNORE INTO users(telegram_id, username) VALUES (?, ?)",
        (telegram_id, username),
    )
    if username:
        cur.execute("UPDATE users SET username=? WHERE telegram_id=?", (username, telegram_id))

    cur.execute(
        "INSERT OR IGNORE INTO admins(telegram_id, username, added_by) VALUES (?, ?, ?)",
        (telegram_id, username, added_by),
    )

    for perm in DEFAULT_GRANTS:
        cur.execute(
            "INSERT OR IGNORE INTO admin_permissions(admin_id, permission, granted) VALUES (?, ?, 1)",
            (telegram_id, perm),
        )

    conn.commit()
    conn.close()


def remove_admin(telegram_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM admins WHERE telegram_id=?", (telegram_id,))
    conn.commit()
    conn.close()


def set_admin_active(telegram_id, active: bool):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE admins SET is_active=? WHERE telegram_id=?", (1 if active else 0, telegram_id))
    conn.commit()
    conn.close()


def set_permission(telegram_id, permission, granted: bool):
    if permission not in PERMISSIONS:
        raise ValueError(f"Unknown permission: {permission}")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO admin_permissions(admin_id, permission, granted) VALUES (?, ?, ?) "
        "ON CONFLICT(admin_id, permission) DO UPDATE SET granted=excluded.granted",
        (telegram_id, permission, 1 if granted else 0),
    )
    conn.commit()
    conn.close()


def list_admins():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM admins ORDER BY created_at ASC")
    rows = cur.fetchall()
    conn.close()
    return rows


# ==========================================
# AUDIT LOG (append-only)
# ==========================================

def log_action(admin_id, action, target_type=None, target_id=None,
               before=None, after=None, ip=None):
    """Records one immutable audit entry. `before`/`after` may be dicts
    or None - stored as JSON. Never raises into caller flows."""

    try:
        username = None
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT username FROM users WHERE telegram_id=?", (admin_id,))
            row = cur.fetchone()
            username = row["username"] if row else None

            cur.execute(
                """
                INSERT INTO admin_audit_logs(
                    admin_id, admin_username, action, target_type, target_id,
                    before_state, after_state, ip
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    admin_id, username, action, target_type,
                    str(target_id) if target_id is not None else None,
                    json.dumps(before) if before is not None else None,
                    json.dumps(after) if after is not None else None,
                    ip,
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    except Exception:
        logger.exception("Audit log write failed (action=%s)", action)


def query_audit_logs(action=None, admin_id=None, limit=20, offset=0):
    conn = get_connection()
    cur = conn.cursor()

    where = []
    params = []

    if action:
        where.append("action=?")
        params.append(action)
    if admin_id:
        where.append("admin_id=?")
        params.append(admin_id)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    cur.execute(
        f"SELECT * FROM admin_audit_logs {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    rows = cur.fetchall()
    conn.close()
    return rows