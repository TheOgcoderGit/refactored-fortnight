"""ChannelFlow AI - Text Commands
================================

Real implementations for the commands ``main.py`` registers.

Before this module existed, ``bot/handlers.py`` defined most of these as
one-line stubs (``pass`` or "Usage: ..."), even though the README and
PROJECT_TRACKER documented them as working. That is the definition of a
dead feature: the command was wired to the Application, so Telegram showed
it as available, but it never touched a service or the database.

Every command here follows the PRD chain:
    command -> handler -> service -> database -> user-visible result
and every one validates input and returns an actionable error instead of
failing silently.
"""

import html
import json
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from config import ADMIN_IDS
from core.forwarder import force_refresh_routes
from database.db import get_connection
from services import (
    formatting_service,
    forward_credit_service,
    plan_service,
    pricing_service,
    project_service,
    support_service,
    wallet_service,
)

logger = logging.getLogger(__name__)

# Rate-limit for user-created tickets: 5 per rolling hour (PRD §33).
TICKET_WINDOW_SECONDS = 3600
TICKET_MAX_PER_WINDOW = 5


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _reply(message, text):
    """Send an HTML message, degrading to plain text if Telegram rejects it."""
    try:
        return message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        return message.reply_text(text)


# ==========================================
# /format  and  /preview
# ==========================================

def _parse_scope(raw: str):
    """Accept ``10`` (project) or ``10:100`` (project:destination)."""
    raw = (raw or "").strip()
    if ":" in raw:
        project_raw, dest_raw = raw.split(":", 1)
        if not project_raw.isdigit() or not dest_raw.isdigit():
            return None, None, "Project and destination ids must be numbers."
        return int(project_raw), int(dest_raw), None
    if not raw.isdigit():
        return None, None, "Project id must be a number."
    return int(raw), None, None


async def format_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/format PROJECT_ID JSON   |   /format PROJECT_ID:DESTINATION_ID JSON"""
    message = update.effective_message
    user_id = update.effective_user.id

    if not context.args or len(context.args) < 2:
        await _reply(message,
                     "🧹 <b>Advanced formatting</b>\n\n"
                     "Usage:\n"
                     "<code>/format PROJECT_ID JSON</code>\n"
                     "<code>/format PROJECT_ID:DESTINATION_ID JSON</code>\n\n"
                     "Example:\n"
                     "<code>/format 10 {\"remove_usernames\":true,\"link_preview\":false,"
                     "\"header\":\"Deals\",\"mono\":true}</code>\n\n"
                     "Keys: link_preview, remove_usernames, remove_links, disable_hidden_links, mono, "
                     "remove_first_words, remove_last_words, remove_first_lines, remove_last_lines, "
                     "keep_first_words, keep_first_lines, header, footer.\n"
                     "A null header/footer inherits the project prefix/suffix.")
        return

    project_id, destination_id, error = _parse_scope(context.args[0])
    if error:
        await _reply(message, f"⚠️ {error}")
        return

    raw_json = " ".join(context.args[1:])
    try:
        config = json.loads(raw_json)
    except ValueError as exc:
        await _reply(message, f"⚠️ That is not valid JSON: <code>{_e(exc)}</code>")
        return

    try:
        formatting_service.configure(user_id, project_id, config, destination_id)
    except PermissionError:
        await _reply(message, "🔒 Project unavailable.")
        return
    except ValueError as exc:
        await _reply(message, f"⚠️ {_e(exc)}")
        return

    await force_refresh_routes()
    scope = f"project {project_id}" if destination_id is None else f"destination {destination_id}"
    await _reply(message,
                 f"✅ Formatting saved for {scope}.\n\n"
                 f"Preview it with <code>/preview {context.args[0]} your sample text</code>")


async def preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/preview PROJECT_ID text   |   /preview PROJECT_ID:DESTINATION_ID text"""
    message = update.effective_message
    user_id = update.effective_user.id

    if not context.args or len(context.args) < 2:
        await _reply(message,
                     "👁 <b>Formatting preview</b>\n\n"
                     "Usage:\n"
                     "<code>/preview PROJECT_ID sample text</code>\n"
                     "<code>/preview PROJECT_ID:DESTINATION_ID sample text</code>\n\n"
                     "This runs the saved rules locally — no AI call and nothing is sent.")
        return

    project_id, destination_id, error = _parse_scope(context.args[0])
    if error:
        await _reply(message, f"⚠️ {error}")
        return

    sample = " ".join(context.args[1:])
    try:
        output = formatting_service.preview(user_id, project_id, sample, destination_id)
    except PermissionError:
        await _reply(message, "🔒 Project unavailable.")
        return

    await _reply(message, "👁 <b>Formatted output</b>\n\n<pre>" + _e(output) + "</pre>")


# ==========================================
# /credits  and  /grantcredits
# ==========================================

async def credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/credits - the caller's extra forwarding-unit balance."""
    message = update.effective_message
    user_id = update.effective_user.id

    balance = forward_credit_service.balance(user_id)
    entitlements = plan_service.get_entitlements(user_id)
    daily_limit = entitlements.get("daily_forward_limit", 0)

    conn = get_connection()
    try:
        used_today = conn.execute(
            "SELECT COALESCE(SUM(du.forward_count),0) FROM daily_usage du "
            "JOIN projects p ON p.id = du.project_id "
            "WHERE p.user_id=? AND du.usage_date=?",
            (user_id, datetime.now(timezone.utc).strftime("%Y-%m-%d")),
        ).fetchone()[0]
    finally:
        conn.close()

    await _reply(message,
                 "⚡ <b>Forwarding credits</b>\n\n"
                 f"• Extra units: <b>{balance}</b>\n"
                 f"• Daily plan allowance: <b>{daily_limit}</b>\n"
                 f"• Used today: <b>{used_today}</b>\n\n"
                 "Your daily allowance is consumed first; extra units are only "
                 "used once it runs out. ChannelFlow never charges money per forward.")


async def grantcredits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/grantcredits USER_ID UNITS UNIQUE_REFERENCE reason ..."""
    message = update.effective_message
    user_id = update.effective_user.id

    if not _is_admin(user_id):
        await _reply(message, "🔒 Admins only.")
        return

    if len(context.args) < 4:
        await _reply(message,
                     "Usage:\n"
                     "<code>/grantcredits USER_ID UNITS UNIQUE_REFERENCE reason</code>\n\n"
                     "UNITS may be negative to revoke. The reference is the idempotency "
                     "key — reusing it with different values is rejected.")
        return

    try:
        target = int(context.args[0])
        units = int(context.args[1])
    except ValueError:
        await _reply(message, "⚠️ USER_ID and UNITS must be whole numbers.")
        return

    reference = context.args[2]
    reason = " ".join(context.args[3:])

    try:
        applied = forward_credit_service.adjust(target, units, reference, reason, actor_id=user_id)
    except ValueError as exc:
        await _reply(message, f"⚠️ {_e(exc)}")
        return

    new_balance = forward_credit_service.balance(target)
    if applied:
        await _reply(message,
                     f"✅ {units:+d} units recorded for <code>{target}</code>.\n"
                     f"New balance: <b>{new_balance}</b>\n"
                     f"Reference: <code>{_e(reference)}</code>")
    else:
        await _reply(message,
                     f"ℹ️ Reference <code>{_e(reference)}</code> was already applied — "
                     f"nothing changed. Balance: <b>{new_balance}</b>")


# ==========================================
# Support tickets
# ==========================================

def _ticket_count_last_hour(user_id: int) -> int:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM support_tickets "
            "WHERE user_id=? AND created_at >= datetime('now','-1 hour')",
            (user_id,),
        ).fetchone()[0]
    finally:
        conn.close()


async def tickets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Single entry point for /ticket /tickets /ticketview /ticketreply
    /ticketclose /ticketreopen — main.py registers each name to this."""
    message = update.effective_message
    user_id = update.effective_user.id
    text = (message.text or "").strip()
    command = text.split()[0].lstrip("/").split("@")[0].lower()
    arg_text = text.split(" ", 1)[1].strip() if " " in text else ""

    if command == "ticket":
        if "|" not in arg_text:
            await _reply(message,
                         "🎫 <b>New support ticket</b>\n\n"
                         "Usage:\n<code>/ticket subject | your message</code>\n\n"
                         "Example:\n<code>/ticket Forwarding stopped | My news project paused itself yesterday.</code>")
            return
        subject, _, body = arg_text.partition("|")
        subject, body = subject.strip(), body.strip()
        if not subject or not body:
            await _reply(message, "⚠️ Both a subject and a message are required.")
            return

        if _ticket_count_last_hour(user_id) >= TICKET_MAX_PER_WINDOW:
            await _reply(message,
                         f"⏳ You can open at most {TICKET_MAX_PER_WINDOW} tickets per hour. "
                         "Please wait a bit, or reply on an existing ticket with "
                         "<code>/ticketreply ID message</code>.")
            return

        ticket_id = support_service.create_ticket(user_id, "general", subject, body)
        await _reply(message,
                     f"✅ Ticket <b>#{ticket_id}</b> created.\n\n"
                     f"• View: <code>/ticketview {ticket_id}</code>\n"
                     f"• Reply: <code>/ticketreply {ticket_id} your message</code>")

    elif command == "tickets":
        rows = support_service.list_user_tickets(user_id, limit=10)
        if not rows:
            await _reply(message, "🎫 You have no support tickets yet.")
            return
        lines = "\n".join(
            f"• <b>#{r['id']}</b> [{_e(r['status'])}] {_e(r['subject'])}"
            for r in rows
        )
        await _reply(message, "🎫 <b>Your tickets</b>\n\n" + lines +
                     "\n\n<code>/ticketview ID</code> to open one.")

    elif command == "ticketview":
        if not arg_text.isdigit():
            await _reply(message, "Usage: <code>/ticketview TICKET_ID</code>")
            return
        ticket_id = int(arg_text)
        ticket = support_service.get_ticket(ticket_id)
        if not ticket or ticket["user_id"] != user_id:
            await _reply(message, "🔒 Ticket unavailable.")
            return
        messages = [m for m in support_service.get_messages(ticket_id)
                    if not m["is_internal_note"]]
        body = "\n\n".join(
            f"<b>{'You' if m['sender_type'] == 'user' else 'Support'}</b>: {_e(m['message'])}"
            for m in messages
        ) or "No messages yet."
        await _reply(message,
                     f"🎫 <b>Ticket #{ticket_id}</b>\n"
                     f"Subject: {_e(ticket['subject'])}\n"
                     f"Status: {_e(ticket['status'])}\n\n" + body)

    elif command == "ticketreply":
        parts = arg_text.split(" ", 1)
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].strip():
            await _reply(message, "Usage: <code>/ticketreply TICKET_ID your message</code>")
            return
        ticket_id, body = int(parts[0]), parts[1].strip()
        ticket = support_service.get_ticket(ticket_id)
        if not ticket or ticket["user_id"] != user_id:
            await _reply(message, "🔒 Ticket unavailable.")
            return
        if ticket["status"] == "closed":
            await _reply(message,
                         f"This ticket is closed. Reopen it first with "
                         f"<code>/ticketreopen {ticket_id}</code>.")
            return
        support_service.add_message(ticket_id, user_id, "user", body)
        await _reply(message, f"✅ Reply added to ticket <b>#{ticket_id}</b>.")

    elif command == "ticketclose":
        if not arg_text.isdigit():
            await _reply(message, "Usage: <code>/ticketclose TICKET_ID</code>")
            return
        ticket_id = int(arg_text)
        ticket = support_service.get_ticket(ticket_id)
        if not ticket or ticket["user_id"] != user_id:
            await _reply(message, "🔒 Ticket unavailable.")
            return
        support_service.set_status(ticket_id, "closed")
        await _reply(message,
                     f"✅ Ticket <b>#{ticket_id}</b> closed. "
                     f"Reopen anytime with <code>/ticketreopen {ticket_id}</code>.")

    elif command == "ticketreopen":
        if not arg_text.isdigit():
            await _reply(message, "Usage: <code>/ticketreopen TICKET_ID</code>")
            return
        ticket_id = int(arg_text)
        ticket = support_service.get_ticket(ticket_id)
        if not ticket or ticket["user_id"] != user_id:
            await _reply(message, "🔒 Ticket unavailable.")
            return
        support_service.set_status(ticket_id, "open")
        await _reply(message, f"🔄 Ticket <b>#{ticket_id}</b> reopened.")

    else:
        await _reply(message,
                     "🎫 Ticket commands:\n"
                     "<code>/ticket subject | message</code>\n"
                     "<code>/tickets</code>\n"
                     "<code>/ticketview ID</code>\n"
                     "<code>/ticketreply ID message</code>\n"
                     "<code>/ticketclose ID</code>\n"
                     "<code>/ticketreopen ID</code>")


# ==========================================
# Admin: plans, pricing, durations, wallet
# ==========================================

PLAN_NAMES = ("FREE", "STARTER", "PRO", "CREATOR")

PLAN_CONFIG_FIELDS = {
    "display_name": str,
    "symbol": str,
    "monthly_price_inr": float,
    "crypto_monthly_price_usd": float,
    "stars_monthly_price": int,
    "max_projects": int,
    "max_sources_per_project": int,
    "max_destinations_per_project": int,
    "daily_forward_limit": int,
    "per_project_daily_forward_limit": int,
    "requires_attribution": int,
    "active": int,
}


async def setplan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setplan USER_ID PLAN [DAYS]"""
    message = update.effective_message
    if not _is_admin(update.effective_user.id):
        await _reply(message, "🔒 Admins only.")
        return
    if len(context.args) < 2:
        await _reply(message,
                     "Usage: <code>/setplan USER_ID PLAN [DAYS]</code>\n"
                     f"Plans: {', '.join(PLAN_NAMES)}")
        return

    try:
        target = int(context.args[0])
    except ValueError:
        await _reply(message, "⚠️ USER_ID must be a number.")
        return

    plan = context.args[1].upper()
    if plan == "BEGINNER":
        plan = "STARTER"
    if plan not in PLAN_NAMES:
        await _reply(message, f"⚠️ Unknown plan. Use one of: {', '.join(PLAN_NAMES)}")
        return

    expiry = None
    if len(context.args) >= 3:
        try:
            days = int(context.args[2])
            expiry = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            await _reply(message, "⚠️ DAYS must be a whole number.")
            return

    plan_service.set_user_plan(target, plan, expiry)
    plan_service.invalidate_plan_configs_cache()
    await _reply(message,
                 f"✅ Plan for <code>{target}</code> set to <b>{plan}</b>"
                 + (f" until {expiry} UTC." if expiry else " with no expiry."))


async def setprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setprice PLAN MONTHLY_INR [CRYPTO_USD] [STARS]"""
    message = update.effective_message
    if not _is_admin(update.effective_user.id):
        await _reply(message, "🔒 Admins only.")
        return
    if len(context.args) < 2:
        await _reply(message,
                     "Usage: <code>/setprice PLAN MONTHLY_INR [CRYPTO_USD] [STARS]</code>\n"
                     f"Plans: {', '.join(PLAN_NAMES)}")
        return

    plan = context.args[0].upper()
    if plan not in PLAN_NAMES:
        await _reply(message, f"⚠️ Unknown plan. Use one of: {', '.join(PLAN_NAMES)}")
        return

    try:
        inr = float(context.args[1])
        usd = float(context.args[2]) if len(context.args) >= 3 else None
        stars = int(context.args[3]) if len(context.args) >= 4 else None
    except ValueError:
        await _reply(message, "⚠️ Prices must be numbers.")
        return

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT 1 FROM plan_configs WHERE plan_name=?", (plan,)).fetchone()
        if not row:
            conn.rollback()
            await _reply(message, f"⚠️ No plan_configs row for {plan}.")
            return
        conn.execute("UPDATE plan_configs SET monthly_price_inr=? WHERE plan_name=?", (inr, plan))
        if usd is not None:
            conn.execute("UPDATE plan_configs SET crypto_monthly_price_usd=? WHERE plan_name=?", (usd, plan))
        if stars is not None:
            conn.execute("UPDATE plan_configs SET stars_monthly_price=? WHERE plan_name=?", (stars, plan))
        conn.commit()
    finally:
        conn.close()

    plan_service.invalidate_plan_configs_cache()
    await _reply(message,
                 f"✅ Pricing updated for <b>{plan}</b>: ₹{inr:.0f}/mo"
                 + (f", ${usd:.2f} crypto" if usd is not None else "")
                 + (f", ⭐{stars}" if stars is not None else ""))


async def setduration_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setduration PLAN MONTHS DISCOUNT_PERCENT [PRICE_INR_OVERRIDE]"""
    message = update.effective_message
    if not _is_admin(update.effective_user.id):
        await _reply(message, "🔒 Admins only.")
        return
    if len(context.args) < 3:
        await _reply(message,
                     "Usage: <code>/setduration PLAN MONTHS DISCOUNT_PERCENT [PRICE_INR_OVERRIDE]</code>\n"
                     "Use <code>none</code> for the override to clear it.")
        return

    plan = context.args[0].upper()
    if plan not in PLAN_NAMES:
        await _reply(message, f"⚠️ Unknown plan. Use one of: {', '.join(PLAN_NAMES)}")
        return

    try:
        months = int(context.args[1])
        discount = float(context.args[2])
    except ValueError:
        await _reply(message, "⚠️ MONTHS and DISCOUNT_PERCENT must be numbers.")
        return

    override = None
    if len(context.args) >= 4 and context.args[3].lower() != "none":
        try:
            override = float(context.args[3])
        except ValueError:
            await _reply(message, "⚠️ The price override must be a number or <code>none</code>.")
            return

    pricing_service.set_duration(plan, months, discount, override, active=True)
    await _reply(message,
                 f"✅ Duration for <b>{plan}</b> at {months} month(s) set to "
                 f"{discount:.0f}% discount"
                 + (f" with a ₹{override:.0f} override." if override is not None else "."))


async def planconfig_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/planconfig PLAN FIELD VALUE   (or /planconfig PLAN to list fields)"""
    message = update.effective_message
    if not _is_admin(update.effective_user.id):
        await _reply(message, "🔒 Admins only.")
        return
    if not context.args:
        await _reply(message,
                     "Usage: <code>/planconfig PLAN FIELD VALUE</code>\n"
                     f"Plans: {', '.join(PLAN_NAMES)}\n"
                     "Fields: " + ", ".join(PLAN_CONFIG_FIELDS))
        return

    plan = context.args[0].upper()
    if plan not in PLAN_NAMES:
        await _reply(message, f"⚠️ Unknown plan. Use one of: {', '.join(PLAN_NAMES)}")
        return

    if len(context.args) < 3:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM plan_configs WHERE plan_name=?", (plan,)).fetchone()
        finally:
            conn.close()
        if not row:
            await _reply(message, f"⚠️ No plan_configs row for {plan}.")
            return
        lines = "\n".join(f"• <code>{k}</code> = <code>{_e(dict(row)[k])}</code>"
                          for k in PLAN_CONFIG_FIELDS)
        await _reply(message, f"⚙️ <b>{plan} configuration</b>\n\n" + lines)
        return

    field = context.args[1]
    if field not in PLAN_CONFIG_FIELDS:
        await _reply(message,
                     f"⚠️ Unknown field. Use one of: {', '.join(PLAN_CONFIG_FIELDS)}")
        return

    try:
        value = PLAN_CONFIG_FIELDS[field](" ".join(context.args[2:]))
    except ValueError:
        await _reply(message, f"⚠️ <code>{field}</code> expects a "
                              f"{PLAN_CONFIG_FIELDS[field].__name__} value.")
        return

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT 1 FROM plan_configs WHERE plan_name=?", (plan,)).fetchone()
        if not row:
            conn.rollback()
            await _reply(message, f"⚠️ No plan_configs row for {plan}.")
            return
        conn.execute(f"UPDATE plan_configs SET {field}=? WHERE plan_name=?", (value, plan))
        conn.commit()
    finally:
        conn.close()

    plan_service.invalidate_plan_configs_cache()
    await _reply(message, f"✅ <b>{plan}</b>.<code>{field}</code> set to <code>{_e(value)}</code>.")


async def walletadjust_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/walletadjust USER_ID AMOUNT_INR reason ..."""
    message = update.effective_message
    if not _is_admin(update.effective_user.id):
        await _reply(message, "🔒 Admins only.")
        return
    if len(context.args) < 3:
        await _reply(message,
                     "Usage: <code>/walletadjust USER_ID AMOUNT_INR reason</code>\n"
                     "Use a negative amount to debit. The reason is stored on the transaction.")
        return

    try:
        target = int(context.args[0])
        amount = float(context.args[1])
    except ValueError:
        await _reply(message, "⚠️ USER_ID and AMOUNT must be numbers.")
        return

    reason = " ".join(context.args[2:])
    reference = f"admin-walletadjust:{update.effective_user.id}:{target}:{datetime.now(timezone.utc).timestamp()}"

    try:
        if amount >= 0:
            wallet_service.credit(target, amount, reference=reference)
        else:
            if not wallet_service.debit(target, -amount, reference=reference):
                await _reply(message,
                             f"⚠️ Insufficient balance — <code>{target}</code> has "
                             f"₹{wallet_service.get_balance_inr(target):.2f}.")
                return
    except Exception as exc:
        logger.exception("Wallet adjustment failed")
        await _reply(message, f"⚠️ {_e(exc)}")
        return

    await _reply(message,
                 f"✅ Wallet for <code>{target}</code> adjusted by ₹{amount:+.2f}.\n"
                 f"New balance: <b>₹{wallet_service.get_balance_inr(target):.2f}</b>\n"
                 f"Reason: {_e(reason)}")


# ==========================================
# Admin: payments queue shortcut
# ==========================================

async def payments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/payments - pending manual-payment approvals (admin only)."""
    from bot import handlers_admin
    message = update.effective_message
    if not _is_admin(update.effective_user.id):
        await _reply(message, "🔒 Admins only.")
        return
    await handlers_admin.render_admin_payments(message, update.effective_user.id)
