"""ChannelFlow AI - /status diagnostics.

A user whose forwarding had stopped had no way to find out why. A source
the account had been kicked from, a target it could no longer post to, an
expired session - all of it failed quietly in the background loop and the
only visible symptom was silence.

/status walks the same chats the forwarder does and reports what it
finds, naming the specific chat and the specific reason, with a button
that leads to the fix.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from core import telegram_utils, user_sessions
from services import (destination_service, i18n_service, plan_service,
                      project_service, source_service)
from bot import error_actions

logger = logging.getLogger(__name__)

# Checking a chat means a real Telegram round trip, so cap the work a
# single /status will do and say so if there was more to check.
MAX_CHECKS = 12

# Short, scannable reasons for the report lines. The long-form copy in
# ERROR_COPY is written for a whole screen, not for a list.
_SHORT_REASON = {
    "not_connected": "account not connected",
    "session_expired": "session expired",
    "not_found": "not found",
    "no_access": "account is not a member",
    "no_post_rights": "cannot post here",
    "rate_limited": "rate limited",
    "invalid_input": "bad handle",
    "unknown": "unreachable",
}

_MARKDOWN_SPECIALS = ("\\", "`", "*", "_", "[", "]")


def _esc(value) -> str:
    """Escape user-controlled text before it goes into a Markdown message.

    Channel titles are the user's own text and can contain asterisks or
    underscores; unescaped they corrupt the message or raise a parse
    error and the reply never arrives.
    """
    text = "" if value is None else str(value)
    for char in _MARKDOWN_SPECIALS:
        text = text.replace(char, "\\" + char)
    return text


def _rows(rows) -> list:
    """sqlite3.Row has no .get(); every consumer here wants a plain dict."""
    return [dict(r) for r in (rows or [])]


def _label(row: dict) -> str:
    return row.get("title") or row.get("username") or row.get("chat_id") or "?"


def _reference(row: dict):
    """The handle to resolve - username resolves reliably, ids may not."""
    return row.get("username") or row.get("chat_id")


def status_keyboard(user_id: int, needs_reconnect: bool) -> InlineKeyboardMarkup:
    rows = []
    if needs_reconnect:
        rows.append([InlineKeyboardButton(
            i18n_service.t(user_id, "status.btn.reconnect"),
            callback_data="nav:connect")])
    rows.append([
        InlineKeyboardButton(i18n_service.t(user_id, "status.btn.projects"),
                             callback_data="nav:projects"),
        InlineKeyboardButton(i18n_service.t(user_id, "status.btn.home"),
                             callback_data="nav:home"),
    ])
    return InlineKeyboardMarkup(rows)


async def _probe(reference, user_id: int, for_destination: bool):
    """None if the chat is usable, otherwise a short reason string."""
    if not reference:
        return i18n_service.t(user_id, "status.reason.not_configured")
    try:
        await telegram_utils.get_chat(reference, for_destination=for_destination,
                                      user_id=user_id)
        return None
    except Exception as exc:
        kind = error_actions.classify(exc)
        key = f"status.reason.{kind}"
        text = i18n_service.t(user_id, key)
        # t() falls back to the key itself when it is missing; treat that
        # as a miss and use the English short reason instead.
        if text == key:
            text = _SHORT_REASON.get(kind, _SHORT_REASON["unknown"])
        return text


async def _chat_lines(user_id, project_id, budget: list) -> tuple:
    """Report lines for one project's sources and targets."""
    lines = []
    problems = 0

    for heading_key, fetcher, for_destination in (
        ("status.sources", source_service.get_sources, False),
        ("status.targets", destination_service.get_destinations, True),
    ):
        rows = _rows(fetcher(project_id))
        lines.append(f"*{_esc(i18n_service.t(user_id, heading_key))}* ({len(rows)})")

        if not rows:
            lines.append(f"    {_esc(i18n_service.t(user_id, 'status.none'))}")
            continue

        for row in rows:
            if not row.get("enabled", 1):
                lines.append("    " + i18n_service.t(
                    user_id, "status.disabled", name=_esc(_label(row))))
                continue

            if budget[0] <= 0:
                lines.append(f"    {_esc(i18n_service.t(user_id, 'status.not_checked'))}")
                continue

            budget[0] -= 1
            reason = await _probe(_reference(row), user_id, for_destination)
            if reason:
                problems += 1
                lines.append("    " + i18n_service.t(
                    user_id, "status.bad", name=_esc(_label(row)),
                    reason=_esc(reason)))
            else:
                lines.append("    " + i18n_service.t(
                    user_id, "status.ok", name=_esc(_label(row))))

    return lines, problems


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reports account, plan and per-chat health for this user."""
    user = update.effective_user
    if not user:
        return
    user_id = user.id

    status_msg = await update.message.reply_text(
        i18n_service.t(user_id, "status.checking"))

    connected = user_sessions.is_connected(user_id)
    lines = [f"*{_esc(i18n_service.t(user_id, 'status.title'))}*", ""]

    lines.append(i18n_service.t(
        user_id, "status.connected" if connected else "status.not_connected"))

    # Plan line - reuses the countdown built for the Subscription screen.
    try:
        plan_state = plan_service.get_plan_status(user_id)
        plan_name = plan_state.get("plan") or "FREE"
        days_left = plan_state.get("days_left")
        if plan_state.get("expired"):
            plan_line = i18n_service.t(user_id, "status.plan.expired")
        elif days_left is not None:
            plan_line = i18n_service.t(user_id, "status.plan.days",
                                       plan=_esc(plan_name), days=days_left)
        else:
            plan_line = i18n_service.t(user_id, "status.plan",
                                       plan=_esc(plan_name))
    except Exception:
        logger.exception("Status: could not read plan for %s", user_id)
        plan_line = None
    if plan_line:
        lines.append(plan_line)

    projects = _rows(project_service.get_projects(user_id))
    lines.append("")
    lines.append(i18n_service.t(user_id, "status.projects", count=len(projects)))

    problems = 0
    if not projects:
        lines.append("")
        lines.append(i18n_service.t(user_id, "status.no_projects"))
    else:
        # One shared budget across every project, so a user with many
        # projects still gets a fast answer.
        budget = [MAX_CHECKS]
        for project in projects:
            lines.append("")
            lines.append(f"*{_esc(project.get('name'))}*")
            state_key = "status.project_active" if project.get("status") else "status.project_paused"
            lines.append(i18n_service.t(user_id, state_key))

            if not connected:
                # Every probe would fail for the same reason, so don't
                # spend the user's time (or Telegram's rate limit) on it.
                lines.append(i18n_service.t(user_id, "status.needs_connection"))
                problems += 1
                continue

            chat_lines, found = await _chat_lines(user_id, project["id"], budget)
            lines.extend(chat_lines)
            problems += found

    lines.append("")
    if problems:
        lines.append(i18n_service.t(user_id, "status.footer.problems",
                                    count=problems))
    elif connected:
        lines.append(i18n_service.t(user_id, "status.footer.ok"))

    needs_reconnect = (not connected) or problems > 0
    try:
        await status_msg.edit_text(
            "\n".join(lines),
            reply_markup=status_keyboard(user_id, needs_reconnect),
            parse_mode="Markdown")
    except Exception:
        # Markdown is stricter than the escaping above anticipates; send
        # the same report unstyled rather than not at all.
        logger.warning("Status: Markdown render failed, sending plain")
        plain = "\n".join(lines).replace("\\", "")
        await status_msg.edit_text(
            plain, reply_markup=status_keyboard(user_id, needs_reconnect))


status_command = status_cmd
