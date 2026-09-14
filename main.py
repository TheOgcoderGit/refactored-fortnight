import socket

# ==============================================================================
# FORCE IPv4 FOR ANDROID / INDIAN MOBILE NETWORKS (Airtel/Jio IPv6 Timeout Fix)
# ==============================================================================
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_getaddrinfo(*args, **kwargs):
    res = _orig_getaddrinfo(*args, **kwargs)
    ipv4 = [r for r in res if r[0] == socket.AF_INET]
    return ipv4 if ipv4 else res
socket.getaddrinfo = _ipv4_getaddrinfo

import logging
import os
import asyncio

from telegram import Update
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
    ApplicationHandlerStop
)

from config import BOT_TOKEN
from database.db import init_db
from utils.startup_check import log_startup_checks

# Clean imports from bot.handlers
from bot.handlers import (
    start,
    cancel,
    connect_command,
    admin_panel,
    owner_panel,
    setplan_command,
    setprice_command,
    setduration_command,
    planconfig_command,
    payments_command,
    walletadjust_command,
    format_command,
    status_command,
    owner_auth_router,
    preview_command,
    credits_command,
    grantcredits_command,
    tickets_command,
    clones_command,
    menu_handler,
    button_handler,
    media_message_router,
    precheckout_callback,
    successful_payment_handler
)

from core.listener import start_listener, stop_listener
from core.client import client as telethon_client
from bot import notifier

# ==========================================
# 1. LOGGING
# ==========================================
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("channelflow")

# ==========================================
# 2. DATABASE INITIALIZATION
# ==========================================
init_db()
log_startup_checks()
logger.info("Database initialized successfully.")

# ==========================================
# 3. GLOBAL ERROR HANDLER
# ==========================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled update error: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            # Show actual error so issues can be fixed immediately
            await update.effective_message.reply_text(f"⚠️ Notice: {context.error}")
        except Exception:
            pass

# ==========================================
# 4. ENGINE & CLONES LIFECYCLE
# ==========================================
async def _post_init(app: Application) -> None:
    notifier.set_bot(app.bot)
    start_listener(app.bot)
    # Start clone bots in background so they don't delay the main bot startup
    async def _bg_clones():
        await asyncio.sleep(2)
        try:
            from services.clone_bot_service import start_all_saved_clones
            await start_all_saved_clones()
        except Exception as e:
            logger.warning("Error auto-starting clone bots: %s", e)
    asyncio.create_task(_bg_clones())

async def _post_shutdown(app: Application) -> None:
    try:
        from services.clone_bot_service import stop_all_clones
        await stop_all_clones()
    except Exception:
        pass
    await stop_listener()
    if telethon_client.is_connected():
        await telethon_client.disconnect()

# ==========================================
# 5. APPLICATION BUILDER WITH IPv4 FAST TIMEOUTS
# ==========================================
request_client = HTTPXRequest(
    connect_timeout=15.0,
    read_timeout=15.0,
    write_timeout=15.0,
    pool_timeout=15.0
)

app = (
    Application.builder()
    .token(BOT_TOKEN)
    .request(request_client)
    .post_init(_post_init)
    .post_shutdown(_post_shutdown)
    .build()
)

# Private chat guard
async def private_chat_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.effective_chat.type != "private":
        if update.callback_query:
            await update.callback_query.answer("Open the bot in a private chat.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("Open the bot in a private chat to manage your account.")
        raise ApplicationHandlerStop

app.add_handler(MessageHandler(filters.ALL, private_chat_guard), group=-1)
app.add_handler(CallbackQueryHandler(private_chat_guard), group=-1)

# ==========================================
# 6. COMMAND HANDLERS
# ==========================================
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("cancel", cancel))
app.add_handler(CommandHandler("connect", connect_command))
app.add_handler(CommandHandler("format", format_command))
app.add_handler(CommandHandler("status", status_command))
app.add_handler(CommandHandler("preview", preview_command))
app.add_handler(CommandHandler("credits", credits_command))
app.add_handler(CommandHandler("grantcredits", grantcredits_command))

for cmd in ("ticket", "tickets", "ticketreply", "ticketview", "ticketclose", "ticketreopen"):
    app.add_handler(CommandHandler(cmd, tickets_command))

app.add_handler(CommandHandler("admin", admin_panel))
app.add_handler(CommandHandler("owner", owner_panel))
app.add_handler(CommandHandler("clones", clones_command))
app.add_handler(CommandHandler("setplan", setplan_command))
app.add_handler(CommandHandler("setprice", setprice_command))
app.add_handler(CommandHandler("setduration", setduration_command))
app.add_handler(CommandHandler("planconfig", planconfig_command))
app.add_handler(CommandHandler("payments", payments_command))
app.add_handler(CommandHandler("walletadjust", walletadjust_command))

# ==========================================
# 7. TELEGRAM STARS PAYMENT HANDLERS
# ==========================================
app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

# ==========================================
# 8. TEXT & MEDIA ROUTERS
# ==========================================
# Owner challenge answers have to be read before the menu router, which
# would otherwise treat the password as a menu selection. It only acts
# when the user is mid-challenge and passes everything else through.
app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        owner_auth_router
    )
)

app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        menu_handler
    )
)

app.add_handler(
    MessageHandler(
        (filters.PHOTO | filters.VIDEO) & ~filters.COMMAND,
        media_message_router
    )
)

# Inline Callbacks
app.add_handler(CallbackQueryHandler(button_handler))

app.add_error_handler(error_handler)

# ==========================================
# 9. RUNTIME ENTRY (SAFE POLLING)
# ==========================================
if __name__ == "__main__":
    logger.info("ChannelFlow AI bot starting with IPv4 Fast Connect...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=5,
        timeout=20
    )