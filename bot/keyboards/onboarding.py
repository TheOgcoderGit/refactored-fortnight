"""
ChannelFlow AI - Onboarding Keyboards
Implements PRD §5: Language, First Welcome, Why Connect, How It Works.
"""
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

def language_keyboard() -> InlineKeyboardMarkup:
    """PRD §5.1: Initial Language selection."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇬🇧 English", callback_data="lang:set:en"),
            InlineKeyboardButton("🇮🇳 Hinglish", callback_data="lang:set:hi")
        ]
    ])

def welcome_keyboard() -> InlineKeyboardMarkup:
    """PRD §5.2: Approved Welcome Message Primary Actions."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔌 Connect Account", callback_data="acct:connect")],
        [InlineKeyboardButton("✨ Explore Features", callback_data="explore:landing")],
        [InlineKeyboardButton("🔐 Why Connect?", callback_data="onboard:why")],
        [
            InlineKeyboardButton("💎 Plans & Credits", callback_data="nav:plans"),
            InlineKeyboardButton("❓ How It Works", callback_data="onboard:how")
        ],
        [InlineKeyboardButton("🆘 Support", callback_data="nav:support")]
    ])

def why_connect_keyboard() -> InlineKeyboardMarkup:
    """PRD §5.3: Why Connect screen buttons."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔌 Connect Now", callback_data="acct:connect")],
        [
            InlineKeyboardButton("◀️ Back", callback_data="nav:start"),
            InlineKeyboardButton("🏠 Home", callback_data="nav:home")
        ]
    ])

def how_it_works_keyboard() -> InlineKeyboardMarkup:
    """PRD §5.5: How It Works buttons."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create Project", callback_data="proj:new")],
        [InlineKeyboardButton("🔌 Connect Account", callback_data="acct:connect")],
        [
            InlineKeyboardButton("◀️ Back", callback_data="nav:start"),
            InlineKeyboardButton("🏠 Home", callback_data="nav:home")
        ]
    ])