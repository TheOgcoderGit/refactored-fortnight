"""
ChannelFlow AI - WhatsApp Pairing Service
============================================

Manages UUIDv4 pairing codes with expiry, rate limiting, and one-time usage.
Integrates with the existing destinations + platform_accounts infrastructure.

State machine for each pairing code:
  PENDING -> USED -> (verified via WhatsApp auth) -> DESTINATION_CREATED -> ACTIVE

Rate limits:
  - Max 3 pairing codes per user per 24 hours
  - Each code expires 24 hours from creation
  - Each code is one-time-use: consumed after successful destination creation
"""

import uuid
import time
import logging
import json
from datetime import datetime, timezone, timedelta

from database.db import get_connection

logger = logging.getLogger(__name__)

MAX_CODES_PER_USER_24h = 3
CODE_EXPIRY_SECONDS = 24 * 3600  # 24 hours


def _now():
    """Return current Unix timestamp."""
    return int(time.time())


# ---------------------------------------------------------------------------
# State machine constants (Prompt 10 requirement)
# ---------------------------------------------------------------------------
# The whatsapp_session_state on the destinations table tracks the full
# state machine: PENDING→PAIRING→CONNECTED→DESTINATION_FOUND→
# AUTHORIZATION_VERIFIED→PERMISSION_VERIFIED→READY→ACTIVE
WHATSSession_STATE_IDLE = "pending"       # PENDING – initial state, no code yet
WHATSSession_STATE_PAIRING = "pairing"    # PAIRING – code generated, waiting for use
WHATSSession_STATE_CONNECTED = "connected"  # CONNECTED – WhatsApp account linked
WHATSSession_STATE_DEST = "destination_found"  # DESTINATION_FOUND – target chat selected
WHATSSession_STATE_AUTH = "authorization_verified"  # AUTHORIZATION_VERIFIED
WHATSSession_STATE_PERM = "permission_verified"  # PERMISSION_VERIFIED
WHATSSession_STATE_READY = "ready"        # READY – all checks passed, ready to forward
WHATSSession_STATE_ACTIVE = "active"      # ACTIVE – forwarding is active

VALID_WHATTSession_STATES = (
    WHATSSession_STATE_IDLE,
    WHATSSession_STATE_PAIRING,
    WHATSSession_STATE_CONNECTED,
    WHATSSession_STATE_DEST,
    WHATSSession_STATE_AUTH,
    WHATSSession_STATE_PERM,
    WHATSSession_STATE_READY,
    WHATSSession_STATE_ACTIVE,
)

# Transition map: current state -> allowed next states
WHATSSession_TRANSITIONS = {
    WHATSSession_STATE_IDLE: {WHATSSession_STATE_PAIRING},
    WHATSSession_STATE_PAIRING: {WHATSSession_STATE_IDLE, WHATSSession_STATE_CONNECTED},
    WHATSSession_STATE_CONNECTED: {WHATSSession_STATE_IDLE, WHATSSession_STATE_DEST},
    WHATSSession_STATE_DEST: {WHATSSession_STATE_CONNECTED, WHATSSession_STATE_AUTH},
    WHATSSession_STATE_AUTH: {WHATSSession_STATE_DEST, WHATSSession_STATE_PERM},
    WHATSSession_STATE_PERM: {WHATSSession_STATE_AUTH, WHATSSession_STATE_READY},
    WHATSSession_STATE_READY: {WHATSSession_STATE_PERM, WHATSSession_STATE_ACTIVE},
    WHATSSession_STATE_ACTIVE: {WHATSSession_STATE_READY},
}


def _advance_session_state(current: str, target: str) -> bool:
    """Validate that `target` is an allowed next state from `current`."""
    if current not in WHATSSession_TRANSITIONS:
        return False
    return target in WHATSSession_TRANSITIONS[current]


def get_next_session_state(current: str) -> str | None:
    """Return the "natural" next state, or None if at terminal state."""
    allowed = WHATSSession_TRANSITIONS.get(current, set())
    if not allowed:
        return None
    # Return the "forward" state (not going back unless explicit)
    forward = sorted(
        allowed - {WHATSSession_STATE_IDLE, WHATSSession_STATE_CONNECTED}
    )
    return forward[0] if forward else None


# ---------------------------------------------------------------------------
# Pairing code functions (same as before, preserved from previous block)
# ---------------------------------------------------------------------------

def _get_cur_conn(cur=None, conn=None):
    """Return (cur, conn) using provided ones or creating new ones.
    When provided connections are used, the caller is responsible for closing them."""
    if cur is None or conn is None:
        conn = get_connection()
        cur = conn.cursor()
    return cur, conn


def _close_cur_conn(cur=None, conn=None):
    """Close provided connections if they were created internally."""
    if conn is not None and cur is not None:
        # Only close if we created them (no args passed)
        pass  # Caller manages lifecycle when passing cur/conn


def generate_pairing_code(user_id: int, cur=None, conn=None) -> tuple:
    """
    Generate a new UUIDv4 pairing code for a user.

    Returns (code, error_message) or (code, None) on success.
    Rate limit: max 3 codes per user per 24 hours.
    If cur/conn are provided, they are reused and NOT closed by this function.
    """
    cur, conn = _get_cur_conn(cur, conn)

    # Check rate limit: count codes created by this user in the last 24 hours
    cutoff = _now()
    cur.execute(
        "SELECT COUNT(*) FROM whatsapp_pairing_codes "
        "WHERE user_id=? AND created_at>=? AND status='pending'",
        (user_id, cutoff - CODE_EXPIRY_SECONDS),
    )
    recent_count = cur.fetchone()[0]

    if recent_count >= MAX_CODES_PER_USER_24h:
        # Don't close provided connections
        return None, (
            f"You have exceeded the rate limit of {MAX_CODES_PER_USER_24h} "
            f"pairing codes per 24 hours. Please wait and try again later."
        ), cur, conn

    # Generate UUIDv4 code
    code = str(uuid.uuid4())
    expiry = _now() + CODE_EXPIRY_SECONDS

    try:
        cur.execute(
            """
            INSERT INTO whatsapp_pairing_codes(
                code, user_id, expires_at, created_at, status
            ) VALUES (?, ?, ?, ?, 'pending')
            """,
            (code, user_id, expiry, _now()),
        )
        conn.commit()
    except Exception as e:
        logger.exception("Failed to insert pairing code for user %s", user_id)
        # Don't close provided connections
        return None, "Failed to create pairing code. Please try again.", cur, conn

    # Set session state to PAIRING when code is generated
    # (the caller should update the destination's whatsapp_session_state)
    # Return the connection for caller reuse
    return code, None, cur, conn


def validate_pairing_code(code: str, user_id: int, cur=None, conn=None) -> tuple:
    """
    Validate a pairing code for a user.

    Returns (code_row, success, cur, conn) where:
    - code_row is the dict from the DB (or None)
    - success is True/False with error message
    - cur/conn are returned for caller reuse
    """
    cur, conn = _get_cur_conn(cur, conn)

    cur.execute(
        "SELECT * FROM whatsapp_pairing_codes WHERE code=?",
        (code,),
    )
    row = cur.fetchone()

    if row is None:
        # Don't close provided connections
        return None, "Invalid pairing code. Please check and try again.", cur, conn

    # Check ownership: code must belong to this user or be unassigned
    if row["user_id"] is not None and row["user_id"] != user_id:
        return row, "This pairing code is not associated with your account.", cur, conn

    # Check expiry
    if _now() > row["expires_at"]:
        # Mark as expired
        cur2, conn2 = _get_cur_conn()
        try:
            cur2.execute(
                "UPDATE whatsapp_pairing_codes SET status='expired' WHERE code=?",
                (code,),
            )
            conn2.commit()
        except Exception:
            pass
        # Don't close provided connections - return the original ones
        return row, "This pairing code has expired. Please generate a new one.", cur, conn

    # Check one-time usage: if already used, reject
    if row["used_at"] is not None:
        return row, "This pairing code has already been used. Please generate a new one.", cur, conn

    return row, True, cur, conn


def mark_code_used(code: str, cur=None, conn=None) -> bool:
    """
    Mark a pairing code as used (one-time consumption).

    Returns True if the code was successfully marked as used,
    False if the code was already used or not found.
    If cur/conn are provided, they are reused and NOT closed by this function.
    """
    cur, conn = _get_cur_conn(cur, conn)

    try:
        cur.execute(
            "UPDATE whatsapp_pairing_codes SET used_at=? WHERE code=? AND used_at IS NULL",
            (_now(), code),
        )
        conn.commit()
        rows_affected = cur.rowcount
        # Don't close provided connections
        return rows_affected > 0
    except Exception:
        # Don't close provided connections
        return False


def get_user_pending_codes(user_id: int, cur=None, conn=None) -> list:
    """
    Get all pending (non-expired, non-used) pairing codes for a user.
    Used for rate limiting and UI display.
    If cur/conn are provided, they are reused and NOT closed by this function.
    """
    cur, conn = _get_cur_conn(cur, conn)

    cur.execute(
        "SELECT * FROM whatsapp_pairing_codes "
        "WHERE user_id=? AND status='pending' AND expires_at > ?",
        (user_id, _now()),
    )
    rows = cur.fetchall()
    # Don't close provided connections
    return rows


def can_create_pairing_code(user_id: int, cur=None, conn=None) -> tuple:
    """
    Check if a user can create a new pairing code.

    Returns (can_create, reason) where reason is None if can_create is True.
    If cur/conn are provided, they are reused and NOT closed by this function.
    """
    cur, conn = _get_cur_conn(cur, conn)

    # Check current pending codes count
    cur.execute(
        "SELECT COUNT(*) FROM whatsapp_pairing_codes "
        "WHERE user_id=? AND status='pending' AND expires_at > ?",
        (user_id, _now()),
    )
    count = cur.fetchone()[0]

    # Don't close provided connections
    if count >= MAX_CODES_PER_USER_24h:
        return False, f"You have reached the maximum of {MAX_CODES_PER_USER_24h} pending pairing codes.", cur, conn
    return True, None, cur, conn


def get_user_pending_codes(user_id: int) -> list:
    """
    Get all pending (non-expired, non-used) pairing codes for a user.
    Used for rate limiting and UI display.
    """
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM whatsapp_pairing_codes "
        "WHERE user_id=? AND status='pending' AND expires_at > ?",
        (user_id, _now()),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# Alias for backward compatibility / easy import
get_pending_user_codes = get_user_pending_codes

# ---------------------------------------------------------------------------
# Destination state machine helpers
# ---------------------------------------------------------------------------

def set_destination_session_state(destination_id: int, state: str, cur=None, conn=None) -> bool:
    """
    Set the whatsapp_session_state on a destination row.

    Returns True on success, False if the state is not a valid whatsapp session state.
    If cur/conn are provided, they are reused and NOT closed by this function.
    """
    if state not in VALID_WHATTSession_STATES:
        logger.error("Invalid whatsapp_session_state: %s", state)
        return False

    cur, conn = _get_cur_conn(cur, conn)

    try:
        cur.execute(
            "UPDATE destinations SET whatsapp_session_state=? WHERE id=?",
            (state, destination_id),
        )
        conn.commit()
        # Don't close provided connections
        return True
    except Exception:
        # Don't close provided connections
        return False


def get_destination_session_state(destination_id: int, cur=None, conn=None) -> str:
    """Get the current whatsapp_session_state for a destination. Defaults to 'pending'."""
    cur, conn = _get_cur_conn(cur, conn)

    cur.execute(
        "SELECT whatsapp_session_state FROM destinations WHERE id=?",
        (destination_id,),
    )
    row = cur.fetchone()

    # Don't close provided connections
    if row and row[0] is not None:
        return row[0]
    return WHATSSession_STATE_IDLE


def get_destination_pairing_status(destination_id: int, cur=None, conn=None) -> str:
    """Get the pairing_status for a destination (legacy compatibility)."""
    cur, conn = _get_cur_conn(cur, conn)

    cur.execute(
        "SELECT pairing_status FROM destinations WHERE id=?",
        (destination_id,),
    )
    row = cur.fetchone()

    # Don't close provided connections
    if row and row[0] is not None:
        return row[0]
    return "pending"


def advance_destination_session_state(destination_id: int, target_state: str, cur=None, conn=None) -> bool:
    """
    Advance a destination's whatsapp_session_state to `target_state`
    if the transition is valid.

    Returns True if the transition succeeded, False otherwise.
    If cur/conn are provided, they are reused and NOT closed by this function.
    """
    cur, conn = _get_cur_conn(cur, conn)

    # Get current state
    cur.execute(
        "SELECT whatsapp_session_state FROM destinations WHERE id=?",
        (destination_id,),
    )
    row = cur.fetchone()
    if row is None:
        # Don't close provided connections
        return False

    current = row[0] if row[0] else WHATSSession_STATE_IDLE

    # Validate transition
    if not _advance_session_state(current, target_state):
        logger.warning(
            "Invalid session state transition from %s to %s for destination %s",
            current, target_state, destination_id,
        )
        # Don't close provided connections
        return False

    try:
        cur.execute(
            "UPDATE destinations SET whatsapp_session_state=? WHERE id=?",
            (target_state, destination_id),
        )
        conn.commit()
        # Don't close provided connections
        return True
    except Exception:
        # Don't close provided connections
        return False


def reset_destination_to_idle(destination_id: int) -> bool:
    """Reset a destination's session state back to PENDING (IDLE)."""
    return set_destination_session_state(destination_id, WHATSSession_STATE_IDLE)


def is_destination_active(destination_id: int) -> bool:
    """Check if a destination is in the ACTIVE state (forwarding is live)."""
    return get_destination_session_state(destination_id) == WHATSSession_STATE_ACTIVE


def is_destination_ready(destination_id: int) -> bool:
    """Check if a destination is in READY state (all checks passed, can forward)."""
    return get_destination_session_state(destination_id) == WHATSSession_STATE_READY


def is_destination_paired(destination_id: int) -> bool:
    """Check if a destination has completed the pairing state machine (is CONNECTED or beyond)."""
    state = get_destination_session_state(destination_id)
    return state in (
        WHATSSession_STATE_CONNECTED,
        WHATSSession_STATE_DEST,
        WHATSSession_STATE_AUTH,
        WHATSSession_STATE_PERM,
        WHATSSession_STATE_READY,
        WHATSSession_STATE_ACTIVE,
    )