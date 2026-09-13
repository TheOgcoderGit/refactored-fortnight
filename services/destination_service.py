from database.db import get_connection
from config import PAIRING_CODE_EXPIRY_MINUTES

import secrets
import string
import time
import hmac
import hashlib


def _pairing_hmac_secret():
    from config import PAIRING_HMAC_SECRET
    return PAIRING_HMAC_SECRET


def sign_pairing_code(code: str) -> str:
    """Sign a pairing code with HMAC-SHA256. Returns the hex signature.
    Empty secret -> empty signature (signing disabled, plain code only)."""
    secret = _pairing_hmac_secret()
    if not secret:
        return ""
    return hmac.new(secret.encode(), code.encode(), hashlib.sha256).hexdigest()


def verify_pairing_signature(code: str, signature: str) -> bool:
    """Constant-time HMAC comparison. Empty configured secret means no
    signature checking is required (returns True for empty signature)."""
    secret = _pairing_hmac_secret()
    if not secret:
        return True
    if not signature:
        return False
    expected = sign_pairing_code(code)
    return hmac.compare_digest(expected, signature)


def _generate_unique_pairing_code(cur) -> str:
    """Generate a unique pairing code with HMAC signature, checking against existing codes."""
    max_attempts = 10
    for _ in range(max_attempts):
        code = "CF" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        # Sign the code with HMAC to prevent replay attacks
        signed = sign_pairing_code(code)
        # Check if this exact signed code already exists
        cur.execute(
            "SELECT 1 FROM destinations WHERE pairing_code=? AND pairing_signature=?",
            (code, signed),
        )
        if not cur.fetchone():
            return code
    # Fallback - should be extremely rare
    code = "CF" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10))
    signed = sign_pairing_code(code)
    return code


def add_destination(
    project_id,
    chat_id,
    username,
    title,
    chat_type,
    platform_account_id=None
):

    conn = get_connection()
    cur = conn.cursor()

    # Generate a unique pairing code with HMAC signature
    max_attempts = 10
    code = None
    signed = None
    for _ in range(max_attempts):
        code = "CF" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        signed = sign_pairing_code(code)
        cur.execute(
            "SELECT 1 FROM destinations WHERE pairing_code=? AND pairing_signature=?",
            (code, signed),
        )
        if not cur.fetchone():
            break
    if code is None:
        # Fallback - should be extremely rare
        code = "CF" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10))
        signed = sign_pairing_code(code)

    # Duplicate Check
    cur.execute(
        """
        SELECT id
        FROM destinations
        WHERE project_id=?
        AND chat_id=?
        """,
        (project_id, chat_id)
    )

    if cur.fetchone():

        conn.close()

        return False

    cur.execute(
        """
        INSERT INTO destinations
        (
            project_id,
            chat_id,
            username,
            title,
            chat_type,
            platform_account_id,
            pairing_code,
            pairing_signature
        )
        VALUES
        (
            ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            project_id,
            chat_id,
            username,
            title,
            chat_type,
            platform_account_id,
            code,
            signed,
        )
    )

    conn.commit()
    conn.close()

    return True


def set_destination_pairing(destination_id, pairing_code, status="pending"):
    """Stores the project-scoped pairing code for a WhatsApp/Threads
    destination and its verification status."""

    conn = get_connection()
    cur = conn.cursor()
    # Sign the code with HMAC to prevent replay attacks
    signed = sign_pairing_code(pairing_code)
    cur.execute(
        "UPDATE destinations SET pairing_code=?, pairing_signature=?, pairing_status=?, pairing_created_at=?, pairing_expires_at=? WHERE id=?",
        (pairing_code, signed, status, int(time.time()), int(time.time()) + PAIRING_CODE_EXPIRY_MINUTES * 60, destination_id),
    )
    conn.commit()
    conn.close()


def update_pairing_status(destination_id, status):
    """Update the pairing status with valid transitions:
    pending → verifying → verified
    pending/verifying → failed
    any → expired (handled by expiry check)
    """
    valid_transitions = {
        "pending": ["verifying", "verified", "failed", "expired"],
        "verifying": ["verified", "failed", "expired"],
        "verified": ["expired"],
        "failed": ["pending"],  # allow retry
        "expired": ["pending"],  # allow new code generation
    }

    conn = get_connection()
    cur = conn.cursor()

    # Get current status
    cur.execute("SELECT pairing_status FROM destinations WHERE id=?", (destination_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return False

    current_status = row["pairing_status"] or "pending"

    if status not in valid_transitions.get(current_status, []):
        conn.close()
        return False

    cur.execute(
        "UPDATE destinations SET pairing_status=? WHERE id=?",
        (status, destination_id),
    )
    conn.commit()
    conn.close()
    return True


def _row_to_dict(row):
    return dict(row) if row is not None else None


def get_destination_by_pairing_code(pairing_code):
    """Find a destination by pairing code, verifying it's not expired and
    has a valid HMAC signature (if HMAC is configured). Returns a dict (or None)."""
    conn = get_connection()
    cur = conn.cursor()
    # First try by pairing_code only (for backward compatibility with
    # databases that don't have pairing_signature yet)
    cur.execute(
        """
        SELECT * FROM destinations
        WHERE pairing_code=?
        AND (pairing_expires_at IS NULL OR pairing_expires_at > ?)
        """,
        (pairing_code, int(time.time())),
    )
    row = cur.fetchone()
    conn.close()
    
    if row is None:
        return None
    
    row_dict = dict(row) if not isinstance(row, dict) else row
    
    # If the row has a pairing_signature, verify it
    if row_dict.get("pairing_signature"):
        from services.destination_service import verify_pairing_signature
        if not verify_pairing_signature(pairing_code, row_dict["pairing_signature"]):
            return None  # Signature mismatch - potential replay attack
    
    return row_dict


def verify_pairing_code(pairing_code):
    """Verify a pairing code and mark the destination as verified.
    Returns the destination row if successful, None otherwise."""
    dest = get_destination_by_pairing_code(pairing_code)
    if dest is None:
        return None

    # Only pending/verifying can be verified
    if dest["pairing_status"] not in ("pending", "verifying"):
        return None

    update_pairing_status(dest["id"], "verified")
    return get_destination(dest["id"])


def mark_pairing_failed(destination_id):
    """Mark a pairing as failed (e.g., invalid code entered)."""
    return update_pairing_status(destination_id, "failed")


def mark_pairing_expired(destination_id):
    """Mark a pairing as expired (called by cleanup job)."""
    return update_pairing_status(destination_id, "expired")


def cleanup_expired_pairings():
    """Clean up expired pairing codes. Returns count of expired."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE destinations
        SET pairing_status='expired'
        WHERE pairing_status IN ('pending', 'verifying')
        AND pairing_expires_at IS NOT NULL
        AND pairing_expires_at < ?
        """,
        (int(time.time()),),
    )
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed


def get_destination(destination_id):
    """Returns a dict (or None)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM destinations WHERE id=?", (destination_id,))
    row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def get_destinations(project_id):
    """Returns a list of dicts."""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM destinations
        WHERE project_id=?
        ORDER BY id DESC
        """,
        (project_id,)
    )

    rows = cur.fetchall()

    conn.close()

    return [dict(r) for r in rows]


def delete_destination(destination_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        DELETE FROM destinations
        WHERE id=?
        """,
        (destination_id,)
    )

    conn.commit()
    conn.close()


def get_destination_chat_ids(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT chat_id
        FROM destinations
        WHERE project_id=?
        """,
        (project_id,)
    )

    rows = [str(row["chat_id"]) for row in cur.fetchall()]

    conn.close()

    return rows


def toggle_destination_enabled(destination_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT enabled FROM destinations WHERE id=?", (destination_id,))
    row = cur.fetchone()

    if row is None:
        conn.close()
        return None

    new_value = 0 if row["enabled"] else 1

    cur.execute(
        "UPDATE destinations SET enabled=? WHERE id=?",
        (new_value, destination_id)
    )

    conn.commit()
    conn.close()

    return new_value


def get_enabled_destination_chat_ids(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT chat_id
        FROM destinations
        WHERE project_id=?
        AND enabled=1
        """,
        (project_id,)
    )

    rows = [int(row["chat_id"]) for row in cur.fetchall()]

    conn.close()

    return rows


def count_destinations(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT COUNT(*)
        FROM destinations
        WHERE project_id=?
        """,
        (project_id,)
    )

    total = cur.fetchone()[0]

    conn.close()

    return total