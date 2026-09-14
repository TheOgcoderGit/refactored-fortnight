# ChannelFlow AI - Master Handlers Switchboard
# ============================================

import logging
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bot import (
    commands,
    handlers_nav,
    handlers_onboard,
    handlers_projects,
    handlers_billing,
    handlers_admin,
    handlers_status,
)

logger = logging.getLogger(__name__)

def _find_fn(module, *names):
    for name in names:
        if hasattr(module, name):
            return getattr(module, name)
    async def _fallback(update, context):
        await update.message.reply_text("Action unavailable.")
    return _fallback

start = _find_fn(handlers_onboard, "start_cmd", "start_command", "start")
cancel = _find_fn(handlers_onboard, "cancel", "cancel_cmd")
connect_command = _find_fn(handlers_onboard, "connect_cmd", "connect_command", "connect")
admin_panel = _find_fn(handlers_admin, "admin_panel_cmd", "admin_panel", "admin_dashboard")
owner_panel = _find_fn(handlers_admin, "owner_panel_cmd", "owner_panel")
clones_command = _find_fn(handlers_admin, "clones_command", "render_clones_view")
status_command = _find_fn(handlers_status, "status_cmd", "status_command", "status")

owner_pannel = owner_panel
admin_pannel = admin_panel

async def payments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await commands.payments_command(update, context)

async def media_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    if hasattr(handlers_billing, "WAITING_PAYMENT_SCREENSHOT") and user.id in handlers_billing.WAITING_PAYMENT_SCREENSHOT:
        rid = handlers_billing.WAITING_PAYMENT_SCREENSHOT.pop(user.id, None)
        if msg.photo:
            fid = msg.photo[-1].file_id
            from services import payment_service
            payment_service.submit_screenshot(rid, fid)
            await msg.reply_text("✅ Payment screenshot received! An admin will review it shortly.")
        else:
            await msg.reply_text("⚠️ Please send payment proof as a photo.")
        return

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if hasattr(handlers_billing, "successful_payment_handler"):
        await handlers_billing.successful_payment_handler(update, context)

# ==========================================
# COMMANDS
# ==========================================
# These used to be stubs: /format, /preview and /ticket only printed usage
# text, and /setplan, /setprice, /setduration, /planconfig and /walletadjust
# were bare `pass`. They were registered in main.py, so Telegram listed them
# as available commands while they never reached a service or the database.
# bot/commands.py holds the real implementations.

async def format_command(update, context):
    await commands.format_command(update, context)

async def preview_command(update, context):
    await commands.preview_command(update, context)

async def credits_command(update, context):
    await commands.credits_command(update, context)

async def grantcredits_command(update, context):
    await commands.grantcredits_command(update, context)

async def tickets_command(update, context):
    await commands.tickets_command(update, context)

async def setplan_command(update, context):
    await commands.setplan_command(update, context)

async def setprice_command(update, context):
    await commands.setprice_command(update, context)

async def setduration_command(update, context):
    await commands.setduration_command(update, context)

async def planconfig_command(update, context):
    await commands.planconfig_command(update, context)

async def walletadjust_command(update, context):
    await commands.walletadjust_command(update, context)

# Master Inline Callback Router (Guaranteed Universal Home)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    user_id = query.from_user.id
    parts = data.split(":")
    action = parts[0]

    try:
        await dispatch_callback(query, user_id, action, parts, context, update=update)
    except Exception:
        # A stale or malformed callback (deleted project, hand-edited data,
        # an old keyboard still on screen) used to escape here and die, so
        # the button just looked dead. Fail loudly in the logs, gently to
        # the user.
        logger.exception("Callback failed for %r (user %s)", data, user_id)
        try:
            await query.answer("That action is no longer available.",
                               show_alert=True)
        except Exception:
            pass


async def dispatch_callback(query, user_id: int, action: str, parts: list,
                            context: ContextTypes.DEFAULT_TYPE,
                            update=None) -> bool:
    """Routes a callback to whichever handler module owns it.

    Split out of button_handler so the dispatch table can be exercised
    directly - tools_dead_buttons.py presses every callback_data the UI can
    produce through this one function, which is what catches dead buttons
    and stale-state crashes before users do.
    """

    # Universal Home Action (Works from everywhere)
    if action == "nav" and len(parts) > 1 and parts[1] == "home":
        from bot.handlers_onboard import connected_home_keyboard, disconnected_home_keyboard, returning_unconnected_keyboard, welcome_keyboard, get_user_lang
        from services import plan_service
        from config import ADMIN_IDS
        from core import user_sessions
        from database.db import get_connection

        lang = get_user_lang(user_id)
        is_conn = user_id in ADMIN_IDS or user_sessions.is_connected(user_id)

        if is_conn:
            ent = plan_service.get_entitlements(user_id)
            is_vip = ent["plan"] == "CREATOR" or user_id in ADMIN_IDS
            t_msg = "🎉 ChannelFlow Home Menu:" if lang == "hi" else "🎉 ChannelFlow Home:"
            try: await query.edit_message_text(t_msg, reply_markup=connected_home_keyboard(is_vip=is_vip, lang=lang))
            except BadRequest: pass
        else:
            conn = get_connection(); cur = conn.cursor()
            cur.execute("SELECT 1 FROM user_telegram_sessions WHERE telegram_id=?", (user_id,))
            had_session = cur.fetchone() is not None; conn.close()
            if had_session:
                t_msg = "⚠️ Aapka account session disconnected hai." if lang == "hi" else "⚠️ Your account session is disconnected."
                try: await query.edit_message_text(t_msg, reply_markup=disconnected_home_keyboard(lang=lang))
                except BadRequest: pass
            else:
                try: await query.edit_message_text("🎉 Welcome to ChannelFlow AI!", reply_markup=welcome_keyboard())
                except BadRequest: pass
        return True

    # Delegate in order
    if hasattr(handlers_onboard, "handle_callbacks") and await handlers_onboard.handle_callbacks(query, user_id, action, parts, context):
        return True
    # Navigation / Account / Settings / Help screens.
    if await handlers_nav.handle_callbacks(query, user_id, action, parts, context):
        return True
    if hasattr(handlers_projects, "handle_callbacks") and await handlers_projects.handle_callbacks(query, user_id, action, parts, context):
        return True
    if hasattr(handlers_billing, "handle_callbacks") and await handlers_billing.handle_callbacks(query, user_id, action, parts, context):
        return True
    if hasattr(handlers_admin, "handle_callbacks") and await handlers_admin.handle_callbacks(query, user_id, action, parts, context):
        return True

    # Full admin console (bot/admin_panel.py). It owns every "adm:*" screen
    # that handlers_admin does not answer itself. It was never registered
    # before, so the whole admin console - finance, coupons, giveaways,
    # support inbox, analytics, system, audit, platform toggles - was dead UI.
    if action == "adm":
        if update is None:
            # Nothing to hand to python-telegram-bot's own dispatcher.
            return True
        try:
            from bot.admin_panel import admin_callback
            await admin_callback(update, context)
        except Exception:
            logger.exception("Admin panel callback failed for %s", query.data)
            try:
                await query.answer("Admin panel is unavailable right now.", show_alert=True)
            except Exception:
                pass
        return True

    # Direct Navigation Subsections
    if action == "nav" and len(parts) > 1:
        sub = parts[1]
        if sub == "projects" and hasattr(handlers_projects, "render_projects_view"):
            await handlers_projects.render_projects_view(query.message, user_id)
            return True
        if sub == "plans" and hasattr(handlers_billing, "render_plans_view"):
            await handlers_billing.render_plans_view(query.message, user_id)
            return True
        if sub == "earn" and hasattr(handlers_billing, "render_earn_view"):
            await handlers_billing.render_earn_view(query.message, user_id)
            return True
        if sub == "account" and hasattr(handlers_onboard, "render_account_view"):
            await handlers_onboard.render_account_view(query.message, query.from_user)
            return True
        if sub == "support" and hasattr(handlers_admin, "render_support_view"):
            await handlers_admin.render_support_view(query.message, user_id)
            return True

    return False


# Master Text Message Router
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not message.text:
        return

    text = message.text.strip()
    user_id = user.id

    # The persistent reply-keyboard menu (📁 Projects / 💳 Subscription / ...)
    # arrives as plain text. handlers_nav maps those labels back to the
    # inline screens; it returns False for anything else, so onboarding
    # (phone numbers, FLOW codes) still wins.
    if await handlers_nav.handle_text(message, user_id, text, context):
        return

    if hasattr(handlers_onboard, "handle_text") and await handlers_onboard.handle_text(message, user_id, text, context):
        return
    if hasattr(handlers_projects, "handle_text") and await handlers_projects.handle_text(message, user_id, text, context):
        return
    if hasattr(handlers_admin, "handle_text") and await handlers_admin.handle_text(message, user_id, text, context):
        return

    await message.reply_text("ℹ️ Use the inline menu options above to manage your automation.")