"""Actionable failure screens.

Every failure in this bot used to end with a sentence and nothing else, so
the user was stuck on a dead-end message with no way forward. This module
gives each failure a typed cause and the buttons that actually fix it.

    chat resolution failed ->  🔄 Reconnect  🔁 Try Again  ❌ Cancel  🏠 Home
    session expired        ->  🔄 Reconnect  ❌ Cancel     🏠 Home
    handle not found       ->  🔁 Try Again  ❌ Cancel     🏠 Home

Handlers call ``reply_resolution_failure()`` and get all of it for free.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from core import telegram_utils as tg

logger = logging.getLogger(__name__)


# Which recovery actions make sense for each failure kind.
_ACTIONS = {
    "not_connected": ("reconnect", "cancel", "home"),
    "session_expired": ("reconnect", "cancel", "home"),
    "not_found": ("retry", "cancel", "home"),
    "no_access": ("retry", "cancel", "home"),
    "no_post_rights": ("retry", "cancel", "home"),
    "rate_limited": ("retry", "cancel", "home"),
    "invalid_input": ("retry", "cancel", "home"),
    "unknown": ("reconnect", "retry", "cancel", "home"),
}

_BUTTONS = {
    "reconnect": InlineKeyboardButton("🔄 Reconnect account", callback_data="nav:connect"),
    "retry": InlineKeyboardButton("🔁 Try again", callback_data="act:retry"),
    "cancel": InlineKeyboardButton("❌ Cancel", callback_data="act:cancel"),
    "home": InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
}


def classify(exc: Exception) -> str:
    """Maps any exception to a failure kind understood by ERROR_COPY."""
    if isinstance(exc, tg.ChatResolveError):
        return exc.kind
    text = str(exc).lower()
    if "not registered in the system" in text or "auth key" in text:
        return "session_expired"
    if "connect your telegram account" in text or "account owner is required" in text:
        return "not_connected"
    if "cannot find any entity" in text:
        return "not_found"
    return "unknown"


def failure_text(exc: Exception, what: str = "chat") -> str:
    """User-facing explanation of why resolving ``what`` failed."""
    kind = classify(exc)
    headline = tg.ERROR_COPY.get(kind, tg.ERROR_COPY["unknown"])

    if kind == "rate_limited":
        seconds = getattr(exc, "seconds", 0)
        if seconds:
            headline += f"\n\nTelegram asked us to wait **{seconds}s**."

    detail = str(getattr(exc, "detail", "") or exc).strip()
    # Telethon details are verbose and full of class names; only show them
    # when they are short enough to be readable in a chat bubble.
    if detail and len(detail) <= 160:
        return f"{headline}\n\n<code>{_escape(detail)}</code>"
    return headline


def failure_keyboard(exc: Exception, extra_row=None) -> InlineKeyboardMarkup:
    """Recovery buttons appropriate to this failure."""
    kind = classify(exc)
    rows = []
    if extra_row:
        rows.append(list(extra_row))

    actions = _ACTIONS.get(kind, _ACTIONS["unknown"])
    rows.append([_BUTTONS[a] for a in actions if a in _BUTTONS])
    return InlineKeyboardMarkup(rows)


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


async def reply_resolution_failure(message, exc: Exception, what: str = "chat",
                                   extra_row=None, parse_mode: str = "HTML"):
    """Replace the old one-line dead end with an actionable screen."""
    text = failure_text(exc, what)
    try:
        await message.reply_text(text, reply_markup=failure_keyboard(exc, extra_row),
                                 parse_mode=parse_mode)
    except Exception:
        # HTML can fail on odd Telethon strings; plain text always works.
        await message.reply_text(
            failure_text(exc, what).replace("<code>", "").replace("</code>", ""),
            reply_markup=failure_keyboard(exc, extra_row))


async def edit_resolution_failure(status_msg, exc: Exception, what: str = "chat",
                                  extra_row=None):
    """Same, for flows that report progress into an existing message."""
    try:
        await status_msg.edit_text(
            failure_text(exc, what),
            reply_markup=failure_keyboard(exc, extra_row),
            parse_mode="HTML")
    except Exception:
        await status_msg.edit_text(
            failure_text(exc, what).replace("<code>", "").replace("</code>", ""),
            reply_markup=failure_keyboard(exc, extra_row))


# ==========================================================================
# PENDING STATE - cancel and retry
# ==========================================================================
#
# Every module kept its own "waiting for the user to type something" dict,
# so /cancel only ever cleared whichever module happened to own it. A user
# who started adding a source and then walked away stayed stuck: the next
# message they typed - anything at all - was swallowed as a channel handle.
#
# cancel_pending() clears all of them, and remember_retry() records enough
# context for "Try again" to re-open the same prompt.

PENDING_RETRY = {}


def _pending_stores():
    """Every module-level dict that can hold a pending user action.

    If a store is missing here, /cancel cannot clear it - and a prompt
    the user has walked away from keeps swallowing everything they type
    afterwards. Two were missing: the template source editor (added with
    the "edit template sources" work) meant that abandoning "➕ Add a
    source channel" left the bot reading every later message as a
    channel handle, and the payment-screenshot wait meant /cancel could
    not get a user out of a payment they had decided not to make.

    tools_orphan_prompts.py checks this list against every prompt the UI
    can produce, so a future store that is added but not registered here
    gets reported instead of quietly eating messages.
    """
    from bot import (admin_promo_handlers, handlers_admin, handlers_billing,
                     handlers_features, handlers_nav, handlers_onboard,
                     handlers_projects)

    return [
        handlers_projects.WAITING_PROJECT_NAME,
        handlers_projects.WAITING_SOURCE,
        handlers_projects.WAITING_DESTINATION,
        handlers_projects.WAITING_TEMPLATE_TARGET,
        handlers_projects.WAITING_TEMPLATE_SOURCE,
        handlers_projects.PENDING_TEMPLATE_CHOICE,
        handlers_projects.CURRENT_PROJECT,
        handlers_features.PENDING_INPUT,
        handlers_nav.PENDING_INPUT,
        handlers_admin.WAITING_AI_SUPPORT,
        handlers_admin.WAITING_TICKET_SUBJ,
        handlers_admin.WAITING_TICKET_BODY,
        handlers_admin.WAITING_COUPON_CODE,
        handlers_admin.WAITING_CLONE_TOKEN,
        handlers_admin.WAITING_BROADCAST_MSG,
        # Multi-stage: promo post / broadcast drafts.
        admin_promo_handlers.WAITING_PROMO,
        handlers_onboard.WAITING_CONNECT_PHONE,
        handlers_onboard.WAITING_CONNECT_STAGE,
        # A photo, not text - but still something the user is waiting on
        # and must be able to walk away from.
        handlers_billing.WAITING_PAYMENT_SCREENSHOT,
    ]


def cancel_pending(user_id: int) -> bool:
    """Clears every pending action for this user. True if anything was open."""
    cleared = False
    for store in _pending_stores():
        if user_id in store:
            store.pop(user_id, None)
            cleared = True
    PENDING_RETRY.pop(user_id, None)
    return cleared


def remember_retry(user_id: int, kind: str, **context):
    """Records what to re-prompt if the user taps "Try again"."""
    PENDING_RETRY[user_id] = {"kind": kind, **context}


def take_retry(user_id: int):
    """Consumes and returns the remembered retry context (or None)."""
    return PENDING_RETRY.pop(user_id, None)
