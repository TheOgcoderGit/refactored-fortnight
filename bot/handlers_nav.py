"""ChannelFlow AI - Navigation, Account, Wallet, Settings & Help
===============================================================

PRD §8 fixes the primary navigation to:

    📁 Projects  💳 Subscription  🎁 Rewards  👤 Account  🆘 Support  ⚙️ Settings

Those labels are a *reply* keyboard (bot/keyboards.main_menu), so they arrive
as ordinary text messages - but nothing in the text router ever matched them,
which meant the whole persistent menu did nothing. ``handle_text`` below
closes that gap.

This module also covers the screens that hang off Account and Settings that
had a keyboard but no handler: wallet, wallet history, payment history,
coupon redemption, referrals, notification preferences, system status, and
the Help section (FAQ / guide / tour / feature request).

Where a screen already exists elsewhere (plans, projects, support inbox) we
delegate to the module that owns it instead of building a second copy.
"""

import html
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest

from config import ADMIN_IDS, UPI_ID, UPI_PAYEE_NAME
from core.listener import is_running
from database.db import get_connection
from services import (
    coupon_service,
    forward_credit_service,
    knowledge_service,
    notification_service,
    payment_service,
    plan_service,
    referral_service,
    support_service,
    wallet_service,
)

logger = logging.getLogger(__name__)

# Pending free-text input for this module: user_id -> {"kind": ...}
PENDING_INPUT = {}


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _onoff(value) -> str:
    return "🟢 On" if value else "🔴 Off"


async def _edit(query, text, markup, html_mode: bool = True):
    try:
        await query.edit_message_text(
            text, reply_markup=markup,
            parse_mode="HTML" if html_mode else None,
            disable_web_page_preview=True,
        )
    except BadRequest:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except BadRequest:
            pass
    except Exception:
        logger.exception("Failed to edit navigation message")


async def _send(query, text, markup=None):
    try:
        await query.message.reply_text(
            text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True
        )
    except BadRequest:
        await query.message.reply_text(text, reply_markup=markup)


def _home_row():
    return [InlineKeyboardButton("🏠 Home", callback_data="nav:home")]


# ==========================================
# SCREEN RENDERERS
# ==========================================

async def _render_plan_billing(query, user_id):
    from bot import handlers_billing
    await handlers_billing.render_plans_view(query.message, user_id, edit=True)


async def _render_wallet(query, user_id):
    balance = wallet_service.get_balance_inr(user_id)
    credits = forward_credit_service.balance(user_id)
    text = (
        "💰 <b>Wallet</b>\n\n"
        f"• Balance: <b>₹{balance:.2f}</b>\n"
        f"• Extra forwarding units: <b>{credits}</b>\n\n"
        "The wallet is for subscription renewals and coupon credit. "
        "ChannelFlow never charges it per forward — forwarding is covered by "
        "your daily plan allowance and extra credit units."
    )
    await _edit(query, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Balance", callback_data="wallet:topup")],
        [InlineKeyboardButton("🎟 Redeem Coupon", callback_data="wallet:redeem")],
        [InlineKeyboardButton("🧾 Transactions", callback_data="acct:wallethistory")],
        [InlineKeyboardButton("💳 Plan & Billing", callback_data="acct:plan")],
        _home_row(),
    ]))


async def _render_wallet_history(query, user_id):
    rows = wallet_service.get_transactions(user_id, limit=10)
    if not rows:
        lines = "No wallet transactions yet."
    else:
        lines = "\n".join(
            f"• <code>{_e(r.get('created_at'))}</code> — {r.get('amount', 0):+.2f} "
            f"{_e(r.get('currency') or 'INR')} ({_e(r.get('reference') or '—')})"
            for r in rows
        )
    await _edit(query,
                "🧾 <b>Wallet transactions</b>\n\n" + lines,
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back to Wallet", callback_data="acct:wallet")],
                    _home_row(),
                ]))


async def _render_payment_history(query, user_id):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM payment_requests WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        lines = "No payment requests yet."
    else:
        icons = {"APPROVED": "✅", "REJECTED": "❌", "SUBMITTED": "⏳",
                 "PENDING_PAYMENT": "🕐", "CANCELLED": "🚫", "EXPIRED": "⌛"}
        lines = []
        for r in rows:
            amount = r["final_amount"] if r["final_amount"] is not None else (r["amount_inr"] or 0)
            currency = r["currency"] or "INR"
            symbol = "$" if currency == "USD" else "₹"
            icon = icons.get(r["status"], "•")
            lines.append(
                f"• {icon} <b>#{r['id']}</b> {_e(r['plan'] or 'Top-up')} "
                f"{symbol}{amount:.0f} — {_e(r['status'])}"
            )
        lines = "\n".join(lines)

    await _edit(query, "🧾 <b>Payment history</b>\n\n" + lines,
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Plan & Billing", callback_data="acct:plan")],
                    _home_row(),
                ]))


async def _render_rewards(query, user_id):
    from bot import handlers_billing
    await handlers_billing.render_earn_view(query.message, user_id)


async def _render_connections(query, user_id):
    from core import user_sessions
    phone = "Connected" if user_sessions.is_connected(user_id) else "Not connected"
    accounts = []
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT platform, account_identifier, health_status FROM platform_accounts WHERE user_id=?",
            (user_id,),
        ).fetchall()
        accounts = [dict(r) for r in rows]
    except Exception:
        accounts = []
    finally:
        conn.close()

    lines = "\n".join(
        f"• {_e(a['platform'])} — <code>{_e(a['account_identifier'])}</code> "
        f"({_e(a.get('health_status') or 'unknown')})"
        for a in accounts
    ) or "No additional platform accounts connected."

    await _edit(query,
                "🔗 <b>Connected Accounts</b>\n\n"
                f"✈️ Telegram: <b>{_e(phone)}</b>\n\n" + lines,
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌐 Manage platform accounts", callback_data="pacct:list")],
                    [InlineKeyboardButton("🔌 Disconnect Telegram session", callback_data="settings:disconnect")],
                    [InlineKeyboardButton("◀️ Back to Account", callback_data="nav:account")],
                    _home_row(),
                ]))


async def _render_platform_accounts(query, user_id):
    from services.platform_registry import get_platforms
    platforms = get_platforms()
    rows = []
    for pid, meta in platforms.items():
        if pid == "telegram":
            continue
        selectable = bool(meta.get("selectable"))
        label = f"{meta.get('icon', '•')} {meta.get('name', pid)}"
        if not selectable:
            label += " — setup required"
            rows.append([InlineKeyboardButton(label, callback_data="platform:locked")])
        else:
            rows.append([InlineKeyboardButton(label, callback_data=f"pacct:new:{pid}")])

    conn = get_connection()
    try:
        linked = conn.execute(
            "SELECT id, platform, account_identifier, health_status FROM platform_accounts WHERE user_id=?",
            (user_id,),
        ).fetchall()
    except Exception:
        linked = []
    finally:
        conn.close()

    for row in linked:
        rows.append([InlineKeyboardButton(
            f"{_e(row['platform'])}: {_e(row['account_identifier'])}",
            callback_data=f"pacct:view:{row['id']}",
        )])

    if not linked:
        rows.append([InlineKeyboardButton("No accounts linked yet", callback_data="acct:noop")])

    rows.append([InlineKeyboardButton("◀️ Back to Account", callback_data="nav:account")])
    await _edit(query,
                "🌐 <b>Platform accounts</b>\n\n"
                "A route is only offered when its full connect → verify → "
                "send path is available. Unfinished platforms stay hidden "
                "rather than pretending to work.",
                InlineKeyboardMarkup(rows))


async def _render_notifications(query, user_id):
    prefs = notification_service.get_prefs(user_id)
    rows = []
    for key, label in notification_service.NOTIFICATION_TYPES:
        rows.append([InlineKeyboardButton(
            f"{label}: {'On' if prefs.get(key, True) else 'Off'}",
            callback_data=f"notif:toggle:{key}",
        )])
    rows.append([InlineKeyboardButton("◀️ Back to Settings", callback_data="nav:settings")])
    await _edit(query,
                "🔔 <b>Notifications</b>\n\n"
                "Essential account messages (payment decisions, security) are "
                "always delivered regardless of these settings.",
                InlineKeyboardMarkup(rows))


async def _render_system_status(query):
    conn = get_connection()
    try:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM projects WHERE status=1").fetchone()[0]
    except Exception:
        users = projects = active = 0
    finally:
        conn.close()

    engine = "🟢 Online" if is_running() else "🔴 Offline"
    await _edit(query,
                "📊 <b>System status</b>\n\n"
                f"• Forwarding engine: {engine}\n"
                f"• Registered users: <b>{users}</b>\n"
                f"• Projects: <b>{projects}</b> ({active} active)\n"
                f"• Server time: <code>{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC</code>",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data="settings:systatus")],
                    [InlineKeyboardButton("◀️ Back to Settings", callback_data="nav:settings")],
                ]))


async def _render_settings(query, user_id):
    auto_renew = wallet_service.is_auto_renew_enabled(user_id)
    await _edit(query,
                "⚙️ <b>Settings</b>",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("👤 Account", callback_data="nav:account"),
                     InlineKeyboardButton("💳 Plan & Billing", callback_data="acct:plan")],
                    [InlineKeyboardButton("🔗 Connected Accounts", callback_data="acct:connections"),
                     InlineKeyboardButton("🌐 Language", callback_data="settings:language")],
                    [InlineKeyboardButton(f"🔁 Auto-Renew: {_onoff(auto_renew)}",
                                          callback_data="settings:togglerenew"),
                     InlineKeyboardButton("🔔 Notifications", callback_data="settings:notifications")],
                    [InlineKeyboardButton("📊 System Status", callback_data="settings:systatus")],
                    [InlineKeyboardButton("🛟 Support", callback_data="nav:support"),
                     InlineKeyboardButton("❓ Help", callback_data="nav:help")],
                    _home_row(),
                ]))


async def _render_help(query, user_id):
    await _edit(query,
                "❓ <b>Help & Support</b>\n\n"
                "Pick a topic below.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔎 FAQ", callback_data="help:faq"),
                     InlineKeyboardButton("📖 Guide", callback_data="help:guide")],
                    [InlineKeyboardButton("🎯 Bot Tour", callback_data="help:tour"),
                     InlineKeyboardButton("💡 Feature Request", callback_data="help:feedback")],
                    # sup:tickets / sup:new_start are the live ticket actions
                    # (bot/handlers_admin.py). The support:* family belongs to
                    # unused keyboards in bot/keyboards.py and has no handler.
                    [InlineKeyboardButton("🎫 My Tickets", callback_data="sup:tickets"),
                     InlineKeyboardButton("💬 New Ticket", callback_data="sup:new_start")],
                    [InlineKeyboardButton("🆘 Support Centre", callback_data="nav:support")],
                    _home_row(),
                ]))


async def _render_connect(query, user_id: int, context) -> bool:
    """Entry point for the "🔄 Reconnect account" button on failure screens.

    Routes into the same flow as /connect and the account screen's
    "🔌 Connect Account" button so there is only one onboarding path.
    """
    from bot import handlers_onboard
    from core import user_sessions

    # ADMIN_IDS is imported at module level - re-importing it here would
    # make it function-local and break the admin branch of handle_callbacks.
    if user_id in ADMIN_IDS or user_sessions.is_connected(user_id):
        await _edit(query, "✅ Telegram account is already connected.",
                    InlineKeyboardMarkup([_home_row()]))
        return True

    handled = await handlers_onboard.handle_callbacks(
        query, user_id, "acct", ["acct", "connect"], context)
    if not handled:
        await _send(query, "Use /connect to connect your Telegram account.")
    return True


async def _reopen_prompt(query, user_id: int, ctx: dict) -> bool:
    """Re-arms the prompt that failed, so "🔁 Try again" actually works.

    Without this the button could only say "start over", which meant
    navigating back through two or three screens to get to the same place.
    """
    from bot import handlers_projects as hp

    kind = ctx.get("kind")
    pid = ctx.get("pid")

    if kind == "source":
        hp.CURRENT_PROJECT[user_id] = pid
        hp.WAITING_SOURCE[user_id] = True
        await _send(query,
                    "📥 **Add Source Channel or Group**\n\n"
                    "Send any of the following:\n"
                    "• Public Username: `@channelusername`\n"
                    "• Private Invite Link: `https://t.me/+AbCdEf...`\n"
                    "• Channel ID: `-1001234567890`\n\n"
                    "*(Make sure your connected Telegram account has joined "
                    "this channel)*")
        return True

    if kind == "destination":
        hp.CURRENT_PROJECT[user_id] = pid
        hp.WAITING_DESTINATION[user_id] = True
        await _send(query,
                    "🎯 **Add Target Channel or Group**\n\n"
                    "Send the username, invite link or ID of the channel "
                    "that should *receive* the posts.\n\n"
                    "Your connected account must be an **admin** there with "
                    "**Post Messages** permission.")
        return True

    if kind == "template_target":
        hp.WAITING_TEMPLATE_TARGET[user_id] = ctx.get("tpl")
        await _send(query,
                    "🎯 **Send your target channel**\n\n"
                    "The template's pre-loaded sources will be added "
                    "automatically; you only provide the destination.")
        return True

    if kind == "template_source":
        hp.WAITING_TEMPLATE_SOURCE[user_id] = ctx.get("tpl")
        await _send(query,
                    "➕ **Add a source channel**\n\n"
                    "Send the public username (`@channel`), an invite link, "
                    "or a numeric ID. Send /cancel to abort.")
        return True

    if kind == "project_name":
        hp.PENDING_TEMPLATE_CHOICE[user_id] = "blank"
        hp.WAITING_PROJECT_NAME[user_id] = True
        await _send(query, "📝 **Send a name for your new project:**")
        return True

    await query.answer("That step can't be retried - please start it again.",
                       show_alert=True)
    return True


async def _render_faq(query):
    articles = knowledge_service.list_articles(kind="faq")
    if not articles:
        body = "No FAQ articles have been published yet."
    else:
        body = "\n\n".join(f"❓ <b>{_e(a['title'])}</b>\n{_e(a['body'])}" for a in articles[:8])
    await _edit(query, "🔎 <b>Frequently asked questions</b>\n\n" + body,
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back to Help", callback_data="nav:help")],
                    _home_row(),
                ]))


async def _render_guide(query):
    text = (
        "📖 <b>Quick start guide</b>\n\n"
        "1️⃣ Connect your Telegram account with <code>/connect +91XXXXXXXXXX</code>, "
        "then reply with <code>FLOW&lt;code&gt;</code>.\n"
        "2️⃣ Open 📁 Projects and create a project (manual, or a ready template).\n"
        "3️⃣ Add at least one <b>source</b> and one <b>target</b>.\n"
        "4️⃣ Press ▶️ Start. New source posts are forwarded automatically.\n\n"
        "Formatting, AI rewriting, watermark, filters and affiliate replacement "
        "only run in <b>Copy</b> mode — switch it in 🚀 Forwarding."
    )
    await _edit(query, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 Open Projects", callback_data="nav:projects")],
        [InlineKeyboardButton("◀️ Back to Help", callback_data="nav:help")],
        _home_row(),
    ]))


async def _render_tour(query):
    text = (
        "🎯 <b>Bot tour</b>\n\n"
        "📁 <b>Projects</b> — every automation pipeline lives here.\n"
        "💳 <b>Subscription</b> — plan, daily allowance and extra credits.\n"
        "🎁 <b>Rewards</b> — referral link and milestone progress.\n"
        "👤 <b>Account</b> — connection, wallet and plan details.\n"
        "🆘 <b>Support</b> — AI help, tickets and the support group.\n"
        "⚙️ <b>Settings</b> — language, notifications, auto-renew, status.\n\n"
        "Inside a project you get forwarding rules, filters, formatting, "
        "watermark, AI tools, affiliate links, auto-reactions and pacing."
    )
    await _edit(query, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Back to Help", callback_data="nav:help")],
        _home_row(),
    ]))


# ==========================================
# CALLBACK ROUTER
# ==========================================

async def handle_callbacks(query, user_id: int, action: str, parts: list, context) -> bool:
    """Handle navigation/account/settings/help callbacks. True when handled."""
    sub = parts[1] if len(parts) > 1 else ""

    # ---------------- nav ----------------
    if action == "nav":
        if sub in ("settings",):
            await _render_settings(query, user_id)
            return True
        if sub == "help":
            await _render_help(query, user_id)
            return True
        if sub == "connect":
            return await _render_connect(query, user_id, context)
        # nav:projects / nav:plans / nav:earn / nav:account / nav:support
        # are owned by bot/handlers.py's direct-navigation block.
        return False

    # ---------------- help ----------------
    if action == "help":
        if sub == "faq":
            await _render_faq(query)
            return True
        if sub == "guide":
            await _render_guide(query)
            return True
        if sub == "tour":
            await _render_tour(query)
            return True
        if sub == "feedback":
            from bot import handlers_admin
            await _send(query,
                        "💡 <b>Feature request</b>\n\n"
                        "Open a support ticket with your idea and it will "
                        "reach the team:")
            await handlers_admin.render_support_view(query.message, user_id)
            return True
        if sub == "support":
            from bot import handlers_admin
            await handlers_admin.render_support_view(query.message, user_id)
            return True
        return False

    # ---------------- universal cancel / retry ----------------
    #
    # Failure screens and every "type a value" prompt offer these. Before
    # this existed, a cancelled flow left the bot waiting for input, so the
    # user's next message was silently swallowed as the value.
    if action == "act":
        if sub == "cancel":
            from bot.error_actions import cancel_pending
            cancel_pending(user_id)
            await _edit(query, "❌ Cancelled. Nothing was changed.",
                        InlineKeyboardMarkup([_home_row()]))
            return True

        if sub == "retry":
            from bot.error_actions import take_retry
            ctx = take_retry(user_id)
            if not ctx:
                await query.answer("There's nothing to retry - start the action again.",
                                   show_alert=True)
                return True
            return await _reopen_prompt(query, user_id, ctx)

        return True

    # ---------------- language ----------------
    if action == "lang":
        from services import i18n_service
        code = (sub or "").lower()
        if code not in i18n_service.LANGUAGES:
            await query.answer("That language isn't available yet.", show_alert=True)
            return True
        i18n_service.set_user_language(user_id, code)
        name = i18n_service.LANGUAGES[code]
        await query.answer(f"Language set to {name}.")
        await _render_settings(query, user_id)
        return True

    # ---------------- settings ----------------
    if action == "settings":
        if sub == "language":
            from bot.keyboards import LANGUAGE_KEYBOARD
            await _edit(query, "🌐 <b>Choose your language</b>", LANGUAGE_KEYBOARD)
            return True
        if sub == "notifications":
            await _render_notifications(query, user_id)
            return True
        if sub == "systatus":
            await _render_system_status(query)
            return True
        if sub == "togglerenew":
            current = wallet_service.is_auto_renew_enabled(user_id)
            wallet_service.set_auto_renew(user_id, not current)
            await query.answer(f"Auto-renew {'enabled' if not current else 'disabled'}.")
            await _render_settings(query, user_id)
            return True
        # settings:disconnect is owned by handlers_onboard
        return False

    # ---------------- account ----------------
    if action == "acct":
        if sub == "plan":
            await _render_plan_billing(query, user_id)
            return True
        if sub == "wallet":
            await _render_wallet(query, user_id)
            return True
        if sub == "wallethistory":
            await _render_wallet_history(query, user_id)
            return True
        if sub == "payhistory":
            await _render_payment_history(query, user_id)
            return True
        if sub == "earn":
            await _render_rewards(query, user_id)
            return True
        if sub == "connections":
            await _render_connections(query, user_id)
            return True
        if sub == "redeem_prompt" or sub == "redeem":
            PENDING_INPUT[user_id] = {"kind": "coupon"}
            await _send(query, "🎟 Send the coupon code you want to redeem (or <code>-</code> to cancel):")
            return True
        if sub == "cancel":
            PENDING_INPUT.pop(user_id, None)
            try:
                await query.message.delete()
            except Exception:
                pass
            return True
        if sub == "noop":
            await query.answer()
            return True
        # acct:connect / confirm_disconnect / lang_picker / autorenew / notifs
        # belong to handlers_onboard.
        return False

    # ---------------- wallet ----------------
    if action == "wallet":
        if sub == "topup":
            # services/payment_service.py currently raises for every money
            # movement (payment acceptance testing is incomplete). Say so up
            # front instead of collecting an amount we cannot process.
            await _send(query,
                        "💳 <b>Wallet top-up is not enabled in this release.</b>\n\n"
                        "Payment processing is switched off until the payment "
                        "acceptance tests are complete.\n\n"
                        "You can still redeem a coupon or check your extra "
                        "forwarding units with /credits.")
            return True
        if sub == "redeem":
            PENDING_INPUT[user_id] = {"kind": "coupon"}
            await _send(query, "🎟 Send the coupon code to redeem (or <code>-</code> to cancel):")
            return True
        return True

    # ---------------- platform accounts ----------------
    if action == "pacct":
        if sub == "list":
            await _render_platform_accounts(query, user_id)
            return True
        if sub == "new":
            platform = parts[2] if len(parts) > 2 else None
            if not platform:
                await _render_platform_accounts(query, user_id)
                return True
            await _send(query,
                        f"🌐 Connecting <b>{_e(platform)}</b> is not available in this build.\n\n"
                        "Only Telegram → Telegram is fully wired. Other routes are hidden "
                        "until their connect → verify → send path is complete.")
            return True
        if sub == "view":
            await _send(query,
                        "🌐 Platform account management is not enabled in this build. "
                        "Telegram → Telegram forwarding is fully supported.")
            return True
        if sub in ("reconnect", "dconfirm"):
            await query.answer("Platform account management is not enabled in this build.",
                               show_alert=True)
            return True
        return True

    if action == "wacode":
        await query.answer("WhatsApp code pairing is not enabled in this build.", show_alert=True)
        return True

    # ---------------- platform selection ----------------
    if action == "platform":
        if sub == "locked":
            await query.answer("This route is not available yet. Telegram → Telegram is fully supported.",
                               show_alert=True)
            return True
        if sub == "telegram":
            query.data = "proj:new"
            return False
        await query.answer("This route is not available yet.", show_alert=True)
        return True

    # ---------------- payment aliases ----------------
    # bot/keyboards.py exposes a second (older) payment flow. Route it into
    # the flow handlers_billing actually implements instead of leaving the
    # buttons dead.
    if action == "pay" and sub == "method":
        query.data = "pay:select_method"
        return await handle_callbacks(query, user_id, "pay", ["pay", "select_method"], context)

    if action == "pay" and sub == "plans":
        method = parts[2] if len(parts) > 2 else "upi"
        from bot import handlers_billing
        handlers_billing.set_user_pay_method(user_id, method)
        await _render_plan_billing(query, user_id)
        return True

    if action == "pay" and sub == "durations":
        method, plan = (parts[2], parts[3]) if len(parts) > 3 else ("upi", "PRO")
        from bot import handlers_billing
        handlers_billing.set_user_pay_method(user_id, method)
        query.data = f"pay:plan:{plan}"
        return False

    if action == "pay" and sub == "create":
        method, plan = (parts[2], parts[3]) if len(parts) > 3 else ("upi", "PRO")
        months = parts[4] if len(parts) > 4 else "1"
        from bot import handlers_billing
        handlers_billing.set_user_pay_method(user_id, method)
        query.data = f"pay:dur:{plan}:{months}"
        return False

    if action == "pay" and sub == "status":
        request_id = parts[2] if len(parts) > 2 else None
        if request_id:
            query.data = f"pay:check_oxapay:{request_id}"
            return False
        await query.answer("Unknown payment request.", show_alert=True)
        return True

    if action == "pay" and sub == "cancel":
        request_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        if request_id is None:
            await query.answer("Unknown payment request.", show_alert=True)
            return True
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT user_id, status FROM payment_requests WHERE id=?", (request_id,)
            ).fetchone()
        finally:
            conn.close()
        if not row or row["user_id"] != user_id:
            await query.answer("Payment request unavailable.", show_alert=True)
            return True
        try:
            payment_service.cancel_payment_request(request_id)
            await query.answer("Payment request cancelled.")
        except RuntimeError as exc:
            # payment_service blocks all money movement in this release.
            logger.info("Cancel payment %s refused: %s", request_id, exc)
            await query.answer("Payment changes are disabled in this release.", show_alert=True)
        except Exception as exc:
            logger.warning("Cancel payment %s failed: %s", request_id, exc)
            await query.answer("Could not cancel this request.", show_alert=True)
        await _render_payment_history(query, user_id)
        return True

    # ---------------- UPI verify / cancel ----------------
    if action == "upgrade":
        request_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        if request_id is None:
            await query.answer("Unknown payment request.", show_alert=True)
            return True

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM payment_requests WHERE id=?", (request_id,)
            ).fetchone()
        finally:
            conn.close()
        if not row or row["user_id"] != user_id:
            await query.answer("Payment request unavailable.", show_alert=True)
            return True

        if sub == "cancel":
            try:
                payment_service.cancel_payment_request(request_id)
                await query.answer("Payment request cancelled.")
            except RuntimeError as exc:
                logger.info("Cancel payment %s refused: %s", request_id, exc)
                await query.answer("Payment changes are disabled in this release.",
                                   show_alert=True)
            except Exception as exc:
                logger.warning("Cancel payment %s failed: %s", request_id, exc)
                await query.answer("Could not cancel this request.", show_alert=True)
            await _render_payment_history(query, user_id)
            return True

        if sub == "verify":
            status = row["status"]
            if status in ("APPROVED",):
                await query.answer("Already approved.", show_alert=True)
                return True
            if row["method"] == "crypto":
                query.data = f"pay:check_oxapay:{request_id}"
                return False
            if row["payment_reference"]:
                await query.answer(
                    "Proof received — an admin is reviewing it.", show_alert=True)
            else:
                from bot import handlers_billing
                handlers_billing.WAITING_PAYMENT_SCREENSHOT[user_id] = request_id
                await _send(query, "📸 Send the screenshot of your payment receipt now:")
            return True
        return True

    # ---------------- legacy admin keyboard ----------------
    if action == "admin":
        if user_id not in ADMIN_IDS:
            await query.answer("Admins only.", show_alert=True)
            return True
        if sub == "refresh" or sub == "dashboard":
            from bot import handlers_admin
            await handlers_admin.render_admin_dashboard(query.message, edit=True)
            return True
        if sub == "payments":
            from bot import handlers_admin
            await handlers_admin.render_admin_payments(query.message, user_id, edit=True)
            return True
        if sub == "broadcast":
            from bot import handlers_admin
            handlers_admin.WAITING_BROADCAST_MSG[user_id] = True
            await _send(query, "📢 Send the announcement to broadcast to all users:")
            return True
        if sub == "maintenance":
            await _send(query,
                        "🛠 Maintenance mode is not implemented yet. "
                        "Use ⚙️ System in the admin console to inspect engine state.")
            return True
        return True

    if action == "admback":
        from bot import handlers_admin
        await handlers_admin.render_admin_dashboard(query.message, edit=True)
        return True

    # ---------------- promo / post / broadcast ----------------
    if action == "promo":
        from bot import admin_promo_handlers
        await admin_promo_handlers.handle_promo_callback(query, context, user_id, parts)
        return True

    return False


# ==========================================
# TEXT ROUTER (reply-keyboard menu)
# ==========================================

# Reply-keyboard labels -> inline callback they should trigger.
# The keyboard is defined in bot/keyboards.main_menu; nothing used to map
# these labels back to a screen, so the whole persistent menu was inert.
MENU_ROUTES = {
    "📁 Projects": "nav:projects",
    "💳 Subscription": "nav:plans",
    "🎁 Rewards": "nav:earn",
    "👤 Account": "nav:account",
    "🆘 Support": "nav:support",
    "⚙️ Settings": "nav:settings",
    # Pre-login keyboard
    "🚀 Connect Now": "acct:connect",
    "📖 Guide": "help:guide",
    "🧭 Tour": "help:tour",
}


async def handle_text(message, user_id: int, text: str, context) -> bool:
    pending = PENDING_INPUT.get(user_id)
    if pending:
        PENDING_INPUT.pop(user_id, None)
        kind = pending["kind"]
        value = (text or "").strip()

        if kind == "coupon":
            if value == "-":
                await message.reply_text("Cancelled.")
                return True
            try:
                ok, coupon, saved, reason = coupon_service.validate(
                    value, user_id, context="wallet"
                )
            except Exception as exc:
                logger.warning("Coupon validation error: %s", exc)
                await message.reply_text("⚠️ Could not validate that coupon. Please try again.")
                return True
            if not ok:
                await message.reply_text(f"⚠️ {_e(reason or 'That coupon cannot be used.')}")
                return True
            discount = float(coupon.get("discount_percent") or 0)
            extra = ""
            if coupon.get("max_discount_inr"):
                extra = f"\n• Maximum discount: ₹{float(coupon['max_discount_inr']):.0f}"
            await message.reply_text(
                "✅ Coupon is valid.\n\n"
                f"• Code: <code>{_e(value)}</code>\n"
                f"• Discount: <b>{discount:.0f}%</b>" + extra + "\n\n"
                "It is applied automatically at your next plan checkout.",
                parse_mode="HTML",
            )
            return True

        if kind == "topup":
            await message.reply_text(
                "💳 <b>Wallet top-up is not enabled in this release.</b>\n\n"
                "Payment processing is switched off until the payment "
                "acceptance tests are complete, so no top-up request was "
                "created.\n\n"
                "You can still:\n"
                "• redeem a coupon (🎟 Redeem Coupon)\n"
                "• check your extra forwarding units with /credits",
                parse_mode="HTML",
            )
            return True

    stripped = (text or "").strip()
    target = MENU_ROUTES.get(stripped)
    if not target:
        return False

    from telegram import InlineKeyboardMarkup as _Markup, InlineKeyboardButton as _Button

    class _Shim:
        """Reuse the inline callback router for a text-menu press.

        The menu handlers all take a callback query; a reply-keyboard press
        has none, so we synthesise the two attributes they use (``message``
        and ``answer``) and let the existing screens do the work.
        """

        def __init__(self, message):
            self.message = message
            self.data = target

        async def answer(self, *args, **kwargs):
            return None

        async def edit_message_text(self, text, reply_markup=None, **kwargs):
            kwargs.pop("parse_mode", None)
            try:
                await self.message.reply_text(
                    text, reply_markup=reply_markup, parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except BadRequest:
                await self.message.reply_text(text, reply_markup=reply_markup)

    shim = _Shim(message)
    action, *rest = target.split(":")
    handled = await handle_callbacks(shim, user_id, action, [action] + rest, context)
    if handled:
        return True

    # Fall back to the owning module for nav targets this file does not own.
    from bot import handlers_onboard, handlers_projects, handlers_billing, handlers_admin
    for module in (handlers_onboard, handlers_projects, handlers_billing, handlers_admin):
        fn = getattr(module, "handle_callbacks", None)
        if fn and await fn(shim, user_id, action, [action] + rest, context):
            return True

    await message.reply_text("ℹ️ That section is not available yet.")
    return True
