# ChannelFlow AI - Resilient Per-User Telegram Login (/connect)
# ==============================================================

import asyncio
import logging
import time
import os
import re
import uuid
from functools import wraps
from collections import defaultdict

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    PhoneNumberBannedError,
    PasswordHashInvalidError,
    FloodWaitError,
    RPCError,
)

from config import API_ID, API_HASH
from database.db import get_connection
from core.session_crypto import encrypt_session

logger = logging.getLogger(__name__)

PENDING_TTL_SECONDS = int(os.getenv("LOGIN_TTL_SECONDS", "600"))
MAX_ATTEMPTS = 3
OTP_COMMAND_PREFIX = "FLOW"


def extract_otp_from_command(text: str):
    if not text:
        return False, "Message is empty."

    if not text.startswith(OTP_COMMAND_PREFIX):
        return False, (
            f"Send the code in this format: {OTP_COMMAND_PREFIX}<code>\n"
            f"Example: {OTP_COMMAND_PREFIX}12345"
        )

    code = text[len(OTP_COMMAND_PREFIX):].strip()

    if not code.isascii() or not code.isdigit() or not (1 <= len(code) <= 8):
        return False, (
            "Code me sirf numbers hone chahiye. Example: "
            f"{OTP_COMMAND_PREFIX}12345"
        )

    return True, code


_pending = {}
_pending_lock = asyncio.Lock()
_submitted_codes = {}
_user_locks = defaultdict(asyncio.Lock)
_last_start = {}
_cleanup_tasks = set()


class ConnectError(Exception):
    pass


class NeedsPassword(Exception):
    pass


async def _drop_pending(user_id):
    state = _pending.pop(user_id, None)
    _submitted_codes.pop(user_id, None)
    if state is not None:
        try:
            await state["client"].disconnect()
        except Exception:
            pass


async def _sweep_expired():
    now = time.monotonic()
    expired = [
        uid for uid, s in _pending.items()
        if now - s["started_at"] > PENDING_TTL_SECONDS
    ]
    for uid in expired:
        async with _user_locks[uid]:
            state = _pending.get(uid)
            if state and time.monotonic() - state["started_at"] > PENDING_TTL_SECONDS:
                state["state"] = "EXPIRED"
                await _drop_pending(uid)


async def start_cleanup_task():
    while True:
        await asyncio.sleep(60)
        try:
            await _sweep_expired()
        except Exception:
            pass


async def close_pending():
    for uid in list(_pending):
        await _drop_pending(uid)
    if _cleanup_tasks:
        await asyncio.gather(*list(_cleanup_tasks), return_exceptions=True)


def _serialized(fn):
    @wraps(fn)
    async def wrapped(user_id, *args, **kwargs):
        async with _user_locks[user_id]:
            return await fn(user_id, *args, **kwargs)
    return wrapped


def pending_stage(user_id):
    state = _pending.get(user_id)
    if not state or time.monotonic() - state["started_at"] > PENDING_TTL_SECONDS:
        return None
    return state["stage"]


@_serialized
async def start_connect(user_id: int, phone_number: str):
    phone_number = phone_number.replace(" ", "")
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone_number):
        raise ConnectError("Please use valid international format with country code (e.g. +919876543210).")

    if is_connected(user_id):
        raise ConnectError("Your Telegram account is already connected.")

    now = time.monotonic()
    if now - _last_start.get(user_id, -1e10) < 15:
        raise ConnectError("Please wait 15 seconds before requesting another code.")

    from core.session_crypto import _key_bytes
    _key_bytes()
    _last_start[user_id] = now

    await _drop_pending(user_id)

    client = TelegramClient(
        StringSession(),
        API_ID,
        API_HASH,
        connection_retries=5,
        retry_delay=2,
        timeout=30.0
    )

    try:
        await client.connect()
        sent = await client.send_code_request(phone_number)

    except PhoneNumberInvalidError:
        await client.disconnect()
        raise ConnectError("That phone number is not valid. Check the country "
                        "code, e.g. +919876543210.")
    except PhoneNumberBannedError:
        await client.disconnect()
        raise ConnectError("Telegram has banned this number.")
    except FloodWaitError as e:
        await client.disconnect()
        raise ConnectError(f"Telegram flood limit: please wait {e.seconds}s.")
    except Exception as e:
        await client.disconnect()
        raise ConnectError(f"Could not request the code ({e}). Check your "
                        "connection and try again.")

    _pending[user_id] = {
        "client": client,
        "phone": phone_number,
        "phone_code_hash": sent.phone_code_hash,
        "stage": "code",
        "state": "WAITING_CODE",
        "attempt_id": uuid.uuid4().hex,
        "attempts": 0,
        "started_at": time.monotonic(),
    }
    _submitted_codes[user_id] = set()


@_serialized
async def submit_code(user_id: int, code: str):
    state = _pending.get(user_id)
    if state is None or state["stage"] != "code":
        raise ConnectError("No active login session. Run /connect first.")

    if time.monotonic() - state["started_at"] > PENDING_TTL_SECONDS:
        await _drop_pending(user_id)
        raise ConnectError("This login session has expired. Run /connect again.")

    if code in _submitted_codes.get(user_id, set()):
        raise ConnectError("That code has already been used. Enter the newest one.")

    client = state["client"]

    # Reconnect only if explicitly disconnected
    try:
        if not client.is_connected():
            await client.connect()
    except Exception as ex:
        logger.warning("Reconnection before sign-in failed: %s", ex)

    try:
        await client.sign_in(phone=state["phone"], code=code, phone_code_hash=state["phone_code_hash"])

    except SessionPasswordNeededError:
        _submitted_codes.setdefault(user_id, set()).add(code)
        state["stage"] = "password"
        state["state"] = "WAITING_2FA"
        raise NeedsPassword()
    except PhoneCodeExpiredError:
        await _drop_pending(user_id)
        raise ConnectError("The login code has expired. Run /connect again.")
    except PhoneCodeInvalidError:
        _submitted_codes.setdefault(user_id, set()).add(code)
        state["attempts"] += 1
        if state["attempts"] >= MAX_ATTEMPTS:
            await _drop_pending(user_id)
            raise ConnectError("Too many wrong codes. Start again with /connect.")
        raise ConnectError(f"Wrong code. Send the fresh one Telegram just sent: "
        f"{OTP_COMMAND_PREFIX}<code>")
    except FloodWaitError as e:
        await _drop_pending(user_id)
        raise ConnectError(f"Telegram flood wait: {e.seconds}s.")
    except Exception as e:
        logger.warning("Sign-in exception for user %s: %s", user_id, e)
        raise ConnectError(f"Login error: {e}")

    await _finalize(user_id)
    return True


@_serialized
async def submit_password(user_id: int, password: str):
    state = _pending.get(user_id)
    if state is None or state["stage"] != "password":
        raise ConnectError("No active 2FA attempt.")

    client = state["client"]

    try:
        if not client.is_connected():
            await client.connect()
    except Exception:
        pass

    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError:
        state["attempts"] += 1
        if state["attempts"] >= MAX_ATTEMPTS:
            await _drop_pending(user_id)
            raise ConnectError("Too many incorrect password attempts. Start over with /connect.")
        raise ConnectError("❌ Incorrect password. Try again:")
    except FloodWaitError as e:
        await _drop_pending(user_id)
        raise ConnectError(f"Telegram flood wait: {e.seconds}s.")
    except Exception as e:
        await _drop_pending(user_id)
        raise ConnectError(f"Login failed: {e}")

    await _finalize(user_id)
    return True


async def _finalize(user_id):
    from services.telegram_ownership import claim_session, AccountAlreadyOwned
    state = _pending[user_id]
    client = state["client"]
    try:
        me = await client.get_me()
        encrypted = encrypt_session(client.session.save())
        claim_session(user_id, me.id, encrypted, state["phone"])
        state["state"] = "CONNECTED"

        conn = get_connection(); cur = conn.cursor()
        cur.execute("UPDATE users SET status='active' WHERE telegram_id=?", (user_id,))
        conn.commit(); conn.close()
    except AccountAlreadyOwned as e:
        state["state"] = "FAILED"
        raise ConnectError(str(e)) from None
    except Exception as e:
        state["state"] = "FAILED"
        logger.exception("Finalization failed for %s: %s", user_id, e)
        # Don't leak the raw exception (may contain internal/session
        # details) to the user - it's already logged above for debugging.
        raise ConnectError("Could not finish the login. Run /connect again.") from None
    finally:
        await _drop_pending(user_id)


def cancel_connect(user_id):
    state = _pending.pop(user_id, None)
    _submitted_codes.pop(user_id, None)
    if state:
        # Track this task in _cleanup_tasks so close_pending() actually
        # waits for the disconnect instead of firing-and-forgetting it
        # (previously _cleanup_tasks was declared but never populated).
        task = asyncio.create_task(state["client"].disconnect())
        _cleanup_tasks.add(task)
        task.add_done_callback(_cleanup_tasks.discard)


def is_connected(telegram_id) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM user_telegram_sessions WHERE telegram_id=? AND status='connected'", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def disconnect_user(telegram_id):
    cancel_connect(telegram_id)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE projects SET status=0 WHERE user_id=?", (telegram_id,))
    cur.execute("DELETE FROM user_telegram_sessions WHERE telegram_id=?", (telegram_id,))
    conn.commit()
    conn.close()