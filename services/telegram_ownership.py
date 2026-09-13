"""Atomic ownership repository. IDs refer to different identity domains."""
import sqlite3
from database.db import get_connection

class AccountAlreadyOwned(Exception):
    pass

def claim_session(owner_id, external_user_id, encrypted_session, phone):
    if not isinstance(external_user_id, int) or external_user_id <= 0:
        raise ValueError("Telegram account ID must come from get_me()")
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""INSERT INTO user_telegram_sessions
            (telegram_id,external_user_id,encrypted_session,phone_number,status)
            VALUES(?,?,?,?,'connected') ON CONFLICT(telegram_id) DO UPDATE SET
            external_user_id=excluded.external_user_id,
            encrypted_session=excluded.encrypted_session,phone_number=excluded.phone_number,
            status='connected',connected_at=CURRENT_TIMESTAMP""",
            (owner_id,external_user_id,encrypted_session,phone))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        if 'external_user_id' in str(e):
            raise AccountAlreadyOwned("This Telegram account is already connected to another ChannelFlow account. Disconnect it there first.") from None
        raise
    finally:
        conn.close()

def require_reconnect(owner_id):
    conn=get_connection()
    try:
        with conn:
            conn.execute("UPDATE user_telegram_sessions SET status='reconnect_required' WHERE telegram_id=?", (owner_id,))
            conn.execute("UPDATE projects SET status=0 WHERE user_id=?", (owner_id,))
    finally:
        conn.close()
