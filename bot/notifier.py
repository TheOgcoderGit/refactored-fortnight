"""
ChannelFlow AI - Push Notifications
======================================

The forward engine (core/forwarder.py, core/listener.py) runs on the
shared Telethon client and has no direct handle to the bot's
``Application``/``bot`` object. This module is the bridge: main.py
calls ``set_bot(app.bot)`` once during startup, and anything else in
the project can then call ``notify_user`` / ``notify_admins`` to push
a real Telegram message - e.g. "forwarding to X has been failing" -
instead of the problem only ever showing up in the 📜 Logs screen or
the server console.

Every call is throttled per ``throttle_key`` so a chat that's failing
on every single message doesn't spam the owner once per message -
just once per cooldown window, with a fresh alert if the situation
changes.
"""

import logging
import time

logger = logging.getLogger(__name__)

_bot = None

_last_sent = {}  # throttle_key -> unix timestamp
THROTTLE_SECONDS = 300  # at most one identical alert every 5 minutes


def set_bot(bot) -> None:
    """Called once from main.py's post_init, after the Application (and
    therefore its .bot) exists."""

    global _bot
    _bot = bot


def _is_throttled(key) -> bool:

    if key is None:
        return False

    now = time.time()
    last = _last_sent.get(key, 0)

    if now - last < THROTTLE_SECONDS:
        return True

    _last_sent[key] = now
    return False


async def notify_user(user_id, text, throttle_key=None) -> None:
    """Best-effort DM to a specific Telegram user id. Never raises -
    a notification failure should never take down the forward engine."""

    if _bot is None:
        logger.warning("notify_user called before bot was ready: %s", text)
        return

    if _is_throttled(throttle_key):
        return

    try:
        await _bot.send_message(user_id, text)
    except Exception:
        logger.exception("Failed to notify user %s", user_id)


async def notify_admins(text, throttle_key=None) -> None:
    """Best-effort DM to every configured admin - used for system-level
    problems (engine crashed, session unauthorized) rather than a single
    project's routing problem."""

    from config import ADMIN_IDS

    if _is_throttled(throttle_key):
        return

    if not ADMIN_IDS:
        logger.warning(
            "notify_admins called but ADMIN_IDS is empty - set ADMIN_IDS "
            "in .env to receive system alerts: %s", text
        )
        return

    for admin_id in ADMIN_IDS:
        await notify_user(admin_id, text)
