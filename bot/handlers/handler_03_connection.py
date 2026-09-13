"""
ChannelFlow AI - Telegram Account Connection Handler
Implements PRD §6: /connect, FLOW<OTP>, 2FA, session encryption, atomic claim, and ephemeral message cleanup.
"""
import logging
import re
from telegram import Update
from telegram.ext import ContextTypes

from core import user_sessions, client_pool
from bot.keyboards.home import auth_flow_cancel_keyboard, connected_home_keyboard
from database.db import get_connection

logger = logging.getLogger(__name__)

# Transient message cleanup registry: user_id -> list of message_ids to delete
EPHEMERAL_AUTH_MESSAGES = {}

def track_ephemeral(user_id: int, message_id: int):
    EPHEMERAL_AUTH_MESSAGES.setdefault(user_id, []).append(message_id)

async def purge_ephemeral_messages(bot, user_id: int, chat_id: int):
    """PRD §6.6: Deletes temporary auth inputs from chat history."""
    msg_ids = EPHEMERAL_AUTH_MESSAGES.pop(user_id, [])
    for mid in msg_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass  # Best-effort deletion; ignore if Telegram forbids

async def connect_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiates /connect flow."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_sessions.is_connected(user_id):
        await query.edit_message_text(
            "✅ Your Telegram account is already connected.",
            reply_markup=connected_home_keyboard()
        )
        return

    context.user_data["awaiting_phone"] = True

    sent_msg = await query.message.reply_text(
        "📱 Connect Account\n\n"
        "Please send your Telegram phone number with country code.\n\n"
        "Example:\n"
        "+919876543210\n\n"
        "Important:\n"
        "• Start with +\n"
        "• Include country code\n"
        "• No spaces",
        reply_markup=auth_flow_cancel_keyboard()
    )
    track_ephemeral(user_id, sent_msg.message_id)

async def phone_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validates phone number and requests OTP."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    track_ephemeral(user.id, message.message_id)
    phone = message.text.strip().replace(" ", "")

    if not phone.startswith("+") or not phone[1:].isdigit() or len(phone) < 8:
        sent = await message.reply_text("❌ Invalid format. Please send with country code: e.g. +919876543210")
        track_ephemeral(user.id, sent.message_id)
        return

    context.user_data.pop("awaiting_phone", None)
    wait_msg = await message.reply_text("⏳ Requesting verification code from Telegram...")
    track_ephemeral(user.id, wait_msg.message_id)

    try:
        await user_sessions.start_connect(user.id, phone)
        context.user_data["awaiting_otp"] = True

        prompt = await message.reply_text(
            "🔑 Enter OTP\n\n"
            "Telegram has sent a verification code to your Telegram app.\n\n"
            "If your code is 12345, send:\n\n"
            "FLOW12345\n\n"
            "or:\n\n"
            "FLOW 12345\n\n"
            "⚠️ The FLOW prefix is required.\n"
            "Do not send the numeric OTP alone.",
            reply_markup=auth_flow_cancel_keyboard()
        )
        track_ephemeral(user.id, prompt.message_id)

    except user_sessions.ConnectError as e:
        err = await message.reply_text(f"❌ {e}")
        track_ephemeral(user.id, err.message_id)

async def otp_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PRD §6.3: Validates FLOW<OTP> format and submits to Telethon."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    track_ephemeral(user.id, message.message_id)
    text = message.text.strip()

    ok, code_or_err = user_sessions.extract_otp_from_command(text)
    if not ok:
        err_msg = await message.reply_text(f"⚠️ {code_or_err}")
        track_ephemeral(user.id, err_msg.message_id)
        return

    try:
        await user_sessions.submit_code(user.id, code_or_err)
        context.user_data.pop("awaiting_otp", None)

        # Start dedicated client pool engine
        await client_pool.start_owner_engine(user.id)

        # Cleanup ephemeral messages
        await purge_ephemeral_messages(context.bot, user.id, message.chat_id)

        await message.reply_text(
            "🎉 Telegram connected successfully!\n\n"
            "Welcome to ChannelFlow.\n\n"
            "Your account is ready to automate content workflows.",
            reply_markup=connected_home_keyboard()
        )

    except user_sessions.NeedsPassword:
        context.user_data.pop("awaiting_otp", None)
        context.user_data["awaiting_2fa"] = True
        p_msg = await message.reply_text(
            "🔐 Two-Step Verification\n\n"
            "Your Telegram account requires a 2FA password.\n"
            "Enter it below to continue.\n\n"
            "Your password is used only for authentication.",
            reply_markup=auth_flow_cancel_keyboard()
        )
        track_ephemeral(user.id, p_msg.message_id)

    except user_sessions.ConnectError as e:
        err = await message.reply_text(f"❌ {e}")
        track_ephemeral(user.id, err.message_id)

async def password_2fa_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PRD §6.4: Submits 2FA password."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    track_ephemeral(user.id, message.message_id)
    pwd = message.text

    try:
        await user_sessions.submit_password(user.id, pwd)
        context.user_data.pop("awaiting_2fa", None)

        await client_pool.start_owner_engine(user.id)
        await purge_ephemeral_messages(context.bot, user.id, message.chat_id)

        await message.reply_text(
            "🎉 Telegram connected successfully!\n\n"
            "Welcome to ChannelFlow.\n\n"
            "Your account is ready to automate content workflows.",
            reply_markup=connected_home_keyboard()
        )

    except user_sessions.ConnectError as e:
        err = await message.reply_text(f"❌ {e}")
        track_ephemeral(user.id, err.message_id)