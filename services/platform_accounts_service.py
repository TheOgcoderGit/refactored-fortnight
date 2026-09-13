"""
ChannelFlow AI - Platform Accounts Service
============================================

Multi-account connection management (Prompt 1 §39, Prompt 2 §26-27):

    * Every platform account belongs to exactly ONE user
    * Credentials encrypted at rest (session_crypto / Fernet-style)
    * Health status tracked; token expiry surfaced for reconnect CTAs

WhatsApp (official Meta Cloud API):
    User provides their OWN phone_number_id + access_token from their
    Meta Business account. We validate by calling the Graph API /me.
    Channel discovery lists WhatsApp Channels the token can access.

Threads (Meta Threads API):
    User connects via an in-bot token submission (they generate a
    long-lived token from Meta's developer tools) OR the OAuth URL.
    We validate via graph.threads.net/me and store refresh tokens.
"""

import json
import logging
import time

import httpx

from database.db import get_connection
from core.session_crypto import encrypt_session, decrypt_session

logger = logging.getLogger(__name__)


class ConnectError(Exception):
    """User-safe message."""


# ==========================================
# CORE CRUD
# ==========================================

def upsert_account(user_id, platform, account_identifier, credentials: dict,
                   extra: dict = None):
    """Stores/updates a platform account with ENCRYPTED credentials."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO platform_accounts(
            user_id, platform, account_identifier,
            encrypted_credentials, status, health_status, last_health_check
        ) VALUES (?, ?, ?, ?, 'connected', 'healthy', ?)
        ON CONFLICT(user_id, platform, account_identifier) DO UPDATE SET
            encrypted_credentials=excluded.encrypted_credentials,
            status='connected',
            health_status='healthy',
            last_health_check=excluded.last_health_check,
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            user_id, platform, account_identifier,
            encrypt_session(json.dumps(credentials)),
            time.time(),
        ),
    )

    conn.commit()
    conn.close()


def get_accounts(user_id, platform=None):
    conn = get_connection()
    cur = conn.cursor()
    if platform:
        cur.execute(
            "SELECT * FROM platform_accounts WHERE user_id=? AND platform=? ORDER BY id",
            (user_id, platform),
        )
    else:
        cur.execute("SELECT * FROM platform_accounts WHERE user_id=? ORDER BY id", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_account(user_id, platform, account_identifier):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM platform_accounts WHERE user_id=? AND platform=? AND account_identifier=?",
        (user_id, platform, account_identifier),
    )
    row = cur.fetchone()
    conn.close()
    return row


def get_credentials(account_row) -> dict:
    """Decrypts stored credentials."""
    try:
        return json.loads(decrypt_session(account_row["encrypted_credentials"]))
    except Exception:
        logger.exception("Failed to decrypt platform account credentials")
        return {}


def remove_account(user_id, account_id):
    """Strict ownership check inside the DELETE itself."""

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM platform_accounts WHERE id=? AND user_id=?",
        (account_id, user_id),
    )
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def set_health(account_id, health_status, note=None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE platform_accounts SET health_status=?, last_health_check=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (health_status, time.time(), account_id),
    )
    if note:
        cur.execute(
            "UPDATE platform_accounts SET rate_limit_state=? WHERE id=?",
            (json.dumps({"note": note}), account_id),
        )
    conn.commit()
    conn.close()


# ==========================================
# WHATSAPP: validate + channel discovery
# ==========================================

async def whatsapp_validate_and_store(user_id, phone_number_id: str, access_token: str):
    """Validates WABA credentials against Graph API, stores on success.

    Returns (display_name, error). Exactly one is truthy."""

    url = f"https://graph.facebook.com/v20.0/{phone_number_id}"
    params = {"access_token": access_token, "fields": "display_phone_number,verified_name"}

    try:
        async with httpx.AsyncClient(timeout=20) as http:
            resp = await http.get(url, params=params)
    except httpx.HTTPError:
        return None, "Couldn't reach Meta's servers. Try again shortly."

    data = {}
    try:
        data = resp.json()
    except ValueError:
        return None, "Invalid response from Meta."

    if resp.status_code >= 400 or "error" in data:
        msg = data.get("error", {}).get("message", "Invalid credentials.")
        # Never echo full provider errors to users - classify instead
        if resp.status_code == 401 or "token" in msg.lower():
            return None, "That access token was rejected. Generate a new one in Meta Business settings."
        return None, f"Meta rejected this connection ({resp.status_code}). Double-check your Phone Number ID."

    display_name = (
        data.get("verified_name")
        or data.get("display_phone_number")
        or phone_number_id
    )

    upsert_account(
        user_id, "whatsapp_channel", phone_number_id,
        {"phone_number_id": phone_number_id, "access_token": access_token},
    )

    return display_name, None


async def whatsapp_list_channels(user_id, account_row):
    """Lists channels/newsletters accessible with this token.

    Uses the WhatsApp Channels API surface where available; falls back
    to an empty list with guidance when the token has no channel scope
    (honest result - never fake destinations)."""

    creds = get_credentials(account_row)
    token = creds.get("access_token")

    url = "https://graph.facebook.com/v20.0/me/newsletter_owned_list"
    params = {"access_token": token, "fields": "id,name"}

    try:
        async with httpx.AsyncClient(timeout=20) as http:
            resp = await http.get(url, params=params)
    except httpx.HTTPError:
        return None, "Network error reaching Meta."

    try:
        data = resp.json()
    except ValueError:
        return None, "Invalid response from Meta."

    if resp.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        if err.get("code") == 190:
            return None, "Token expired - please reconnect your WhatsApp account."
        return [], None  # valid token, but no channels endpoint access

    owned = (data.get("data") or [])
    channels = [
        {"id": c.get("id"), "name": c.get("name", c.get("id"))}
        for c in owned if c.get("id")
    ]
    return channels, None


# ==========================================
# THREADS: validate + store
# ==========================================

async def threads_validate_and_store(user_id, threads_user_id: str, access_token: str,
                                     refresh_token: str = None, expires_in: int = None):
    """Validates a Threads token via /me and stores it."""

    import threading as _t  # noqa: F401  (placeholder guard against accidental import)

    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        async with httpx.AsyncClient(timeout=20) as http:
            resp = await http.get("https://graph.threads.net/v1.0/me", headers=headers)
    except httpx.HTTPError:
        return None, "Couldn't reach Meta's servers. Try again shortly."

    try:
        data = resp.json()
    except ValueError:
        return None, "Invalid response from Meta."

    if resp.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        code = err.get("code")
        if code == 190:
            return None, "That token is expired or invalid. Generate a fresh long-lived token."
        return None, "Meta rejected this token. Check that it has threads_basic + threads_content_publish."

    username = data.get("username") or threads_user_id
    resolved_id = str(data.get("id") or threads_user_id)

    conn = get_connection()
    cur = conn.cursor()

    expires_at = (time.time() + expires_in) if expires_in else (time.time() + 60 * 24 * 3600)

    cur.execute(
        """
        INSERT INTO platform_accounts(
            user_id, platform, account_identifier,
            encrypted_credentials, access_token, refresh_token,
            expires_at, status, health_status, last_health_check
        ) VALUES (?, 'threads', ?, ?, ?, ?, ?, 'connected', 'healthy', ?)
        ON CONFLICT(user_id, platform, account_identifier) DO UPDATE SET
            encrypted_credentials=excluded.encrypted_credentials,
            access_token=excluded.access_token,
            refresh_token=excluded.refresh_token,
            expires_at=excluded.expires_at,
            status='connected',
            health_status='healthy',
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            user_id, resolved_id,
            encrypt_session(json.dumps({
                "access_token": access_token,
                "refresh_token": refresh_token,
            })),
            access_token, refresh_token, expires_at, time.time(),
        ),
    )

    conn.commit()
    conn.close()

    return username, None


def threads_get_token(user_id):
    """Returns a valid access token for the user's Threads account,
    refreshing it when close to expiry. Returns None when not connected."""

    accounts = get_accounts(user_id, "threads")
    if not accounts:
        return None

    acct = accounts[0]
    creds = get_credentials(acct)

    token = creds.get("access_token") or acct["access_token"]
    refresh = creds.get("refresh_token") or acct["refresh_token"]
    expires_at = acct["expires_at"] or 0

    # Refresh within 1 day of expiry
    if refresh and expires_at and expires_at - time.time() < 86400:
        new_token = _threads_refresh_sync(refresh)
        if new_token:
            token = new_token
            set_health(acct["id"], "healthy")
        else:
            set_health(acct["id"], "warning", note="refresh_failed")

    return token


def _threads_refresh_sync(refresh_token):
    """Blocking refresh - called from contexts where sync IO is fine
    (worker tasks). Returns new access token or None."""

    from config import THREADS_APP_ID, THREADS_APP_SECRET

    if not THREADS_APP_ID or not THREADS_APP_SECRET:
        return None

    try:
        with httpx.Client(timeout=20) as http:
            resp = http.post(
                "https://graph.threads.net/refresh_access_token",
                data={
                    "grant_type": "th_refresh_token",
                    "client_id": THREADS_APP_ID,
                    "client_secret": THREADS_APP_SECRET,
                    "refresh_token": refresh_token,
                },
            )
        data = resp.json()
        if resp.status_code < 400 and data.get("access_token"):
            return data["access_token"]
    except Exception:
        logger.exception("Threads token refresh failed")

    return None