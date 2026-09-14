"""
ChannelFlow AI - Owner Authentication Panel

Three-step auth flow: Telegram ID → username → password → security answer.
Rate-limited, lockout after N failed attempts, session expiry.

Owner session is tracked in-memory; a restart invalidates all sessions
(owner must re-authenticate). Failed attempts are persisted in app_config
so lockout survives restart.
"""

import hashlib
import time
import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationHandlerStop, ContextTypes

from config import (
    OWNER_ID, OWNER_USERNAME, OWNER_PASSWORD_HASH,
    OWNER_SECURITY_ANSWER_HASH, OWNER_MAX_ATTEMPTS, OWNER_LOCKOUT_SECONDS,
)
from services.app_config import get, set as cfg_set
from bot.admin_panel import admin_root_keyboard

logger = logging.getLogger(__name__)

# In-memory owner sessions: {user_id: {"expires_at": float, "authenticated": int}}
_owner_sessions = {}

# In-memory step tracker: {user_id: {"step": str, "attempts": int, "started_at": float}}
_owner_auth_flow = {}

SESSION_DURATION = 3600  # 1 hour


def _make_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def is_owner_authenticated(user_id: int) -> bool:
    session = _owner_sessions.get(user_id)
    if session and session["expires_at"] > time.time():
        return True
    _owner_sessions.pop(user_id, None)
    return False


def _is_locked_out(user_id: int) -> bool:
    key = f"owner_lockout:{user_id}"
    until = get(key)
    if until:
        try:
            until_ts = float(until)
            if time.time() < until_ts:
                return True
        except (ValueError, TypeError):
            pass
    cfg_set(key, "")  # clear expired lockout
    return False


def _record_failed_attempt(user_id: int):
    key = f"owner_attempts:{user_id}"
    attempts = int(get(key, "0")) + 1
    cfg_set(key, str(attempts))
    if attempts >= OWNER_MAX_ATTEMPTS:
        until = time.time() + OWNER_LOCKOUT_SECONDS
        cfg_set(f"owner_lockout:{user_id}", str(until))
        cfg_set(key, "0")
        logger.warning("Owner lockout triggered for user %s (%ds)", user_id, OWNER_LOCKOUT_SECONDS)


def _reset_attempts(user_id: int):
    cfg_set(f"owner_attempts:{user_id}", "0")
    cfg_set(f"owner_lockout:{user_id}", "")


def owner_session_expired(user_id: int) -> bool:
    return not is_owner_authenticated(user_id)


async def _end_owner_session(query, user_id: int):
    _owner_sessions.pop(user_id, None)
    _owner_auth_flow.pop(user_id, None)
    await query.message.reply_text("🔒 Owner session ended. Use /owner to start again.")


def _credentials_configured() -> bool:
    """True when the 3-step challenge can actually be answered.

    If any of these are empty the challenge is unwinnable: every answer
    hashes to something that never equals "". Starting the flow anyway
    locked the owner out of their own console with no explanation, so
    /owner now says what is missing instead.
    """
    return all((OWNER_USERNAME, OWNER_PASSWORD_HASH, OWNER_SECURITY_ANSWER_HASH))


OWNER_SETUP_HELP = (
    "⚙️ Owner credentials are not configured.\n\n"
    "The owner console is protected by a 3-step challenge, and these "
    "environment variables are not all set:\n\n"
    "• OWNER_ID — your Telegram user ID\n"
    "• OWNER_USERNAME — the username for step 1\n"
    "• OWNER_PASSWORD_HASH — sha256 of the step 2 password\n"
    "• OWNER_SECURITY_ANSWER_HASH — sha256 of the step 3 answer\n\n"
    "Generate a hash with:\n"
    "    python -c \"import hashlib;print(hashlib.sha256("
    "b'your-value').hexdigest())\"\n\n"
    "Set them in your environment (or .env) and restart the bot. "
    "Ask the bot to /status in the meantime to check everything else."
)


async def owner_auth_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Consumes the challenge answers before the menu router sees them.

    Registered ahead of menu_handler. Most messages are not part of an
    auth flow and pass straight through; when one is, the answer is
    handled here and the update is stopped, so a password is never also
    interpreted as a menu command.
    """
    user = update.effective_user
    if user is None or user.id not in _owner_auth_flow:
        return

    await owner_auth_text_handler(update, context)
    raise ApplicationHandlerStop


async def owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return

    user_id = user.id

    # Step 0: verify Telegram ID against OWNER_ID
    if OWNER_ID is None:
        # Not "unauthorised" - the owner has simply never been configured.
        # Sending them to re-auth on a box with no owner would be a lie.
        await update.message.reply_text(
            "⚙️ No owner is configured for this bot.\n\n"
            "Set OWNER_ID (your Telegram user ID) in the environment and "
            "restart the bot to enable the owner console.")
        return

    if user_id != OWNER_ID:
        await update.message.reply_text("⛔ You are not authorized as the owner.")
        return

    # The ID matches, but with no credentials the challenge cannot be
    # completed - say so rather than starting a flow with no exit.
    if not _credentials_configured():
        await update.message.reply_text(OWNER_SETUP_HELP)
        return

    # Check lockout
    if _is_locked_out(user_id):
        remaining = int(float(get(f"owner_lockout:{user_id}", "0")) - time.time())
        await update.message.reply_text(
            f"🔒 Too many failed attempts. Please wait {max(remaining, 0)} seconds before trying again."
        )
        return

    # Check existing session
    if is_owner_authenticated(user_id):
        await update.message.reply_text(
            "✅ You are already authenticated as the owner.",
            reply_markup=admin_root_keyboard(),
        )
        return

    # Start step 1: username
    _owner_auth_flow[user_id] = {"step": "username", "attempts": 0, "started_at": time.time()}
    await update.message.reply_text(
        "🛡 Owner Authentication\n\nStep 1/3: Enter your owner username."
    )


async def owner_auth_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the text input during the 3-step owner auth flow."""
    user = update.effective_user
    if user is None:
        return

    user_id = user.id
    flow = _owner_auth_flow.get(user_id)
    if flow is None:
        return  # not in auth flow

    # Session/flow expired after 5 minutes
    if time.time() - flow["started_at"] > 300:
        _owner_auth_flow.pop(user_id, None)
        await update.message.reply_text("⏱ Authentication expired. Send /owner to start over.")
        return

    text = (update.message.text or "").strip()

    # /cancel escapes the flow
    if text == "/cancel":
        _owner_auth_flow.pop(user_id, None)
        await update.message.reply_text("❌ Owner authentication cancelled.")
        return

    step = flow["step"]

    if step == "username":
        if text == OWNER_USERNAME:
            flow["step"] = "password"
            await update.message.reply_text("✅ Username correct.\n\nStep 2/3: Enter your owner password.")
        else:
            _record_failed_attempt(user_id)
            flow["attempts"] += 1
            await update.message.reply_text("❌ Incorrect username. Try again or send /cancel.")

    elif step == "password":
        if _make_hash(text) == OWNER_PASSWORD_HASH:
            flow["step"] = "security"
            await update.message.reply_text("✅ Password correct.\n\nStep 3/3: Enter your security answer (birth date).")
        else:
            _record_failed_attempt(user_id)
            flow["attempts"] += 1
            await update.message.reply_text("❌ Incorrect password. Try again or send /cancel.")

    elif step == "security":
        if _make_hash(text) == OWNER_SECURITY_ANSWER_HASH:
            # All steps passed
            _owner_sessions[user_id] = {"expires_at": time.time() + SESSION_DURATION, "authenticated": 1}
            _reset_attempts(user_id)
            _owner_auth_flow.pop(user_id, None)
            await update.message.reply_text(
                "✅ Owner authentication complete. Welcome!",
                reply_markup=admin_root_keyboard(),
            )
            logger.info("Owner authenticated: user %s", user_id)
        else:
            _record_failed_attempt(user_id)
            flow["attempts"] += 1
            await update.message.reply_text("❌ Incorrect security answer. Try again or send /cancel.")

    # Re-check lockout after failed attempt
    if _is_locked_out(user_id):
        _owner_auth_flow.pop(user_id, None)
        remaining = int(float(get(f"owner_lockout:{user_id}", "0")) - time.time())
        await update.message.reply_text(
            f"🔒 Too many failed attempts. Please wait {max(remaining, 0)} seconds."
        )