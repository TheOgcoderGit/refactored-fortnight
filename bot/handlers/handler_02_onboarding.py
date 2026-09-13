"""
ChannelFlow AI - Onboarding Explanations & Feature Explorer Handlers
Implements PRD §5.3 (Why Connect), §5.4 (Feature Explorer), §5.5 (How It Works).
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from bot.keyboards.onboarding import why_connect_keyboard, how_it_works_keyboard, welcome_keyboard
from bot.keyboards.explorer import (
    explorer_landing_keyboard,
    explorer_page_keyboard,
    feature_detail_keyboard
)
from services.feature_explorer_service import get_plan_page, get_feature_by_id, PLAN_SYMBOLS

logger = logging.getLogger(__name__)

async def why_connect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PRD §5.3: Explains account connection requirement."""
    query = update.callback_query
    await query.answer()

    text = (
        "🔐 Why Connect?\n\n"
        "ChannelFlow needs your Telegram account connection "
        "for the Telegram actions your automation projects "
        "are configured to perform.\n\n"
        "After connecting, you can select eligible sources, "
        "targets and topics that your account can access.\n\n"
        "Your authentication session is protected, temporary "
        "authentication inputs are handled only for login, "
        "and you can disconnect your account later."
    )
    await query.edit_message_text(text, reply_markup=why_connect_keyboard())

async def how_it_works_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PRD §5.5: 8-step workflow explanation."""
    query = update.callback_query
    await query.answer()

    text = (
        "❓ How ChannelFlow Works\n\n"
        "1️⃣ Connect your Telegram account\n"
        "2️⃣ Create an automation Project\n"
        "3️⃣ Choose your Sources\n"
        "4️⃣ Choose your Targets\n"
        "5️⃣ Decide which messages should pass\n"
        "6️⃣ Decide how content should be changed\n"
        "7️⃣ Test the workflow\n"
        "8️⃣ Activate the Project\n\n"
        "After activation, ChannelFlow processes eligible "
        "messages according to your configured rules."
    )
    await query.edit_message_text(text, reply_markup=how_it_works_keyboard())

async def explorer_landing_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PRD §5.4.2: Feature Explorer Landing Screen."""
    query = update.callback_query
    await query.answer()

    text = (
        "✨ ChannelFlow Features\n\n"
        "Explore what you can do with ChannelFlow. "
        "Features are grouped by plan so you can "
        "see exactly what each plan gives you.\n\n"
        "Choose a plan below:"
    )
    await query.edit_message_text(text, reply_markup=explorer_landing_keyboard())

async def explorer_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PRD §5.4.3 & §5.4.4: Paginated plan feature catalog."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    plan = parts[2]
    page = int(parts[3])

    items, cur_page, total_pages = get_plan_page(plan, page)
    plan_display = PLAN_SYMBOLS.get(plan, plan)

    text = (
        f"{plan_display} Plan — Features\n\n"
        "Here's what you get with this plan:\n\n"
    )
    for it in items:
        text += f"• {it['name']}: {it['desc']}\n"
    
    text += f"\nPage {cur_page + 1} of {total_pages}"

    await query.edit_message_text(
        text,
        reply_markup=explorer_page_keyboard(plan, cur_page, total_pages, items)
    )

async def feature_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PRD §5.4.5: Feature Detail screen."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    feature_id = parts[2]
    plan = parts[3]
    page = int(parts[4])

    feat = get_feature_by_id(feature_id)

    text = (
        f"{feat['name']}\n\n"
        f"{feat['desc']}\n\n"
        f"Available on:\n"
        f"{feat['min_plan']}+ Plans\n\n"
        "What it does:\n"
        "• Processes eligible source events\n"
        "• Applies your configured rules\n"
        "• Delivers directly to selected targets\n"
        "• Records audit and status logs"
    )
    await query.edit_message_text(
        text,
        reply_markup=feature_detail_keyboard(plan, page)
    )