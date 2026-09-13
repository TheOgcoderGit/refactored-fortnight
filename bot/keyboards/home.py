"""
ChannelFlow AI - Connected and Disconnected Home Keyboards
Implements PRD §7: Main Navigation.
"""
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

def connected_home_keyboard(is_vip_or_owner: bool = False) -> InlineKeyboardMarkup:
    """PRD §5.6 & F031: Connected User Main Home."""
    keyboard = [
        [InlineKeyboardButton("📁 Projects", callback_data="nav:projects")],
        [InlineKeyboardButton("✨ Explore Features", callback_data="explore:landing")],
        [
            InlineKeyboardButton("💎 Plans & Credits", callback_data="nav:plans"),
            InlineKeyboardButton("💰 Earn / Affiliate", callback_data="nav:earn")
        ],
        [
            InlineKeyboardButton("👤 My Account", callback_data="nav:account"),
            InlineKeyboardButton("🆘 Support", callback_data="nav:support")
        ]
    ]
    if is_vip_or_owner:
        keyboard.append([InlineKeyboardButton("👑 Contact Owner", callback_data="nav:owner_contact")])
    return InlineKeyboardMarkup(keyboard)

def disconnected_home_keyboard() -> InlineKeyboardMarkup:
    """PRD §5.6: Disconnected User Returning Home."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔌 Reconnect Account", callback_data="acct:connect")],
        [InlineKeyboardButton("📁 Projects", callback_data="nav:projects")],
        [
            InlineKeyboardButton("👤 My Account", callback_data="nav:account"),
            InlineKeyboardButton("🏠 Home", callback_data="nav:home")
        ]
    ])

def auth_flow_cancel_keyboard() -> InlineKeyboardMarkup:
    """PRD §6.1: Connection Cancel Keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 Why is this needed?", callback_data="onboard:why")],
        [
            InlineKeyboardButton("◀️ Back", callback_data="nav:home"),
            InlineKeyboardButton("✖️ Cancel", callback_data="acct:cancel")
        ]
    ])