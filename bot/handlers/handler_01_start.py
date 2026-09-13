"""
ChannelFlow AI - /start & Home Entry Handler
Implements PRD §5.1, §5.2, §5.6.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from database.db import get_connection
from core import user_sessions
from bot.keyboards.onboarding import language_keyboard, welcome_keyboard
from bot.keyboards.home import connected_home_keyboard, disconnected_home_keyboard
from services.plan_service import start_trial, get_entitlements
from services.referral_service import capture_referral, parse_referral_code
from config import ADMIN_IDS

logger = logging.getLogger(__name__)

WELCOME_COPY = (
    "👋 Welcome, creator, to ChannelFlow AI!\n\n"
    "ChannelFlow watches the Telegram channels and groups "
    "you choose and automatically forwards or copies new "
    "posts to your selected destinations — Telegram today, "
    "with WhatsApp and Threads support on the way.\n\n"
    "Set filters for what gets through, reformat or replace "
    "text before it goes out, add delays, and let ChannelFlow "
    "handle retries and reliability so you don't have to "
    "babysit your automation.\n\n"
    "Everything runs through your own connected Telegram "
    "account. Your account is isolated from other users, and "
    "your login session is encrypted when stored.\n\n"
    "🎁 Your first 7 days start automatically when you register "
    "and include the Creator trial. Plan limits apply.\n\n"
    "Tap Connect below to get started."
)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /start command."""
    user = update.effective_user
    if not user:
        return

    # 1. Register or Fetch User Record
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT language, trial_started_at FROM users WHERE telegram_id=?", (user.id,))
    row = cur.fetchone()

    # Capture referral if deep link argument is present
    if context.args:
        referrer_id = parse_referral_code(context.args[0])
        if referrer_id:
            capture_referral(referrer_id, user.id)

    if row is None:
        # First-time user registration
        cur.execute("""
            INSERT INTO users(telegram_id, username, first_name, last_name, language, onboarding_step)
            VALUES(?, ?, ?, ?, NULL, 'language')
        """, (user.id, user.username, user.first_name, user.last_name))
        conn.commit()
        conn.close()

        # Start the 7-day Creator Trial
        start_trial(user.id)

        # Prompt for language first (PRD §5.1)
        await update.message.reply_text(
            "🌐 Choose your preferred language:\n\nअपनी पसंदीदा भाषा चुनें:",
            reply_markup=language_keyboard()
        )
        return

    conn.close()
    lang = row["language"]

    if not lang:
        # User exists but language not chosen yet
        await update.message.reply_text(
            "🌐 Choose your preferred language:\n\nअपनी पसंदीदा भाषा चुनें:",
            reply_markup=language_keyboard()
        )
        return

    # 2. Returning User Flow (PRD §5.6)
    is_connected = user.id in ADMIN_IDS or user_sessions.is_connected(user.id)
    if is_connected:
        ent = get_entitlements(user.id)
        is_vip = ent["plan"] == "CREATOR" or user.id in ADMIN_IDS
        await update.message.reply_text(
            "👋 Welcome back!\n\nYou're all set — your ChannelFlow account is ready.",
            reply_markup=connected_home_keyboard(is_vip_or_owner=is_vip)
        )
    else:
        await update.message.reply_text(
            "👋 Welcome back!\n\n⚠️ Your Telegram account is disconnected.\n\nReconnect to continue your automation.",
            reply_markup=disconnected_home_keyboard()
        )

async def set_language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles language selection and presents the Welcome message (PRD §5.1, §5.2)."""
    query = update.callback_query
    await query.answer()
    lang = query.data.split(":")[-1]

    user_id = query.from_user.id
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET language=?, onboarding_step='welcome' WHERE telegram_id=?", (lang, user_id))
    conn.commit()
    conn.close()

    # Render PRD §5.2 Welcome Message
    await query.edit_message_text(
        WELCOME_COPY,
        reply_markup=welcome_keyboard()
    )