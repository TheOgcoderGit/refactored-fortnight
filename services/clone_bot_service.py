"""
ChannelFlow AI - Dynamic Clone Bot Runner Service
Runs multiple BotFather clone instances on the same central ChannelFlow engine.
"""
import logging
from telegram import Bot, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    filters
)
from database.db import get_connection
from core.session_crypto import encrypt_session, decrypt_session

logger = logging.getLogger(__name__)

# Active clone applications registry: bot_id -> Application instance
_active_clones = {}

async def validate_bot_token(token: str):
    """Validates BotFather token and retrieves bot identity."""
    bot = Bot(token=token)
    me = await bot.get_me()
    return {
        "bot_id": me.id,
        "username": me.username,
        "first_name": me.first_name
    }

def register_bot_instance(owner_id: int, token: str, bot_id: int, username: str, display_name: str) -> int:
    encrypted_token = encrypt_session(token)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO bot_instances(bot_token_encrypted, bot_id, bot_username, display_name, status, created_by)
        VALUES(?, ?, ?, ?, 'active', ?)
        ON CONFLICT(bot_id) DO UPDATE SET
            bot_token_encrypted=excluded.bot_token_encrypted,
            bot_username=excluded.bot_username,
            display_name=excluded.display_name,
            status='active',
            updated_at=CURRENT_TIMESTAMP
    """, (encrypted_token, bot_id, username, display_name, owner_id))
    conn.commit()
    inst_id = cur.lastrowid
    conn.close()
    return inst_id

def list_bot_instances():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, bot_id, bot_username, display_name, status, created_at FROM bot_instances ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

async def start_clone_app(bot_token: str, bot_id: int):
    """Spawns an independent PTB Application on the active asyncio loop with network timeout safety."""
    if bot_id in _active_clones:
        return _active_clones[bot_id]

    from telegram.request import HTTPXRequest
    from telegram.ext import (
        Application,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        PreCheckoutQueryHandler,
        filters
    )
    from bot.handlers import (
        start, cancel, connect_command, format_command, preview_command,
        credits_command, grantcredits_command, tickets_command,
        menu_handler, button_handler, media_message_router,
        precheckout_callback, successful_payment_handler
    )

    req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)
    clone_app = Application.builder().token(bot_token).request(req).build()

    clone_app.add_handler(CommandHandler("start", start))
    clone_app.add_handler(CommandHandler("cancel", cancel))
    clone_app.add_handler(CommandHandler("connect", connect_command))
    clone_app.add_handler(CommandHandler("format", format_command))
    clone_app.add_handler(CommandHandler("preview", preview_command))
    clone_app.add_handler(CommandHandler("credits", credits_command))
    clone_app.add_handler(CommandHandler("grantcredits", grantcredits_command))
    for cmd in ("ticket", "tickets", "ticketreply", "ticketview", "ticketclose", "ticketreopen"):
        clone_app.add_handler(CommandHandler(cmd, tickets_command))

    clone_app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    clone_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    clone_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))
    clone_app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO) & ~filters.COMMAND, media_message_router))
    clone_app.add_handler(CallbackQueryHandler(button_handler))

    await clone_app.initialize()
    await clone_app.start()
    await clone_app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, bootstrap_retries=-1, timeout=30)

    _active_clones[bot_id] = clone_app
    logger.info("Clone bot @%s started live successfully!", bot_id)
    return clone_app

    # Register standard ChannelFlow handlers
    clone_app.add_handler(CommandHandler("start", start))
    clone_app.add_handler(CommandHandler("cancel", cancel))
    clone_app.add_handler(CommandHandler("connect", connect_command))
    clone_app.add_handler(CommandHandler("format", format_command))
    clone_app.add_handler(CommandHandler("preview", preview_command))
    clone_app.add_handler(CommandHandler("credits", credits_command))
    clone_app.add_handler(CommandHandler("grantcredits", grantcredits_command))
    for cmd in ("ticket", "tickets", "ticketreply", "ticketview", "ticketclose", "ticketreopen"):
        clone_app.add_handler(CommandHandler(cmd, tickets_command))

    clone_app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    clone_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    clone_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))
    clone_app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO) & ~filters.COMMAND, media_message_router))
    clone_app.add_handler(CallbackQueryHandler(button_handler))

    await clone_app.initialize()
    await clone_app.start()
    await clone_app.updater.start_polling(drop_pending_updates=True)

    _active_clones[bot_id] = clone_app
    logger.info("Clone bot @%s started live successfully!", bot_id)
    return clone_app

async def start_all_saved_clones():
    """Restores and starts all active clone bots on startup."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT bot_token_encrypted, bot_id FROM bot_instances WHERE status='active'")
    rows = cur.fetchall()
    conn.close()

    for r in rows:
        try:
            token = decrypt_session(r["bot_token_encrypted"])
            await start_clone_app(token, r["bot_id"])
        except Exception as e:
            logger.warning("Could not auto-start clone bot %s: %s", r["bot_id"], e)

async def stop_all_clones():
    for bot_id, app in list(_active_clones.items()):
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            pass
    _active_clones.clear()