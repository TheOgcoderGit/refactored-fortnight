"""
ChannelFlow AI - Feature Explorer Keyboard
Implements PRD §5.4: Plan-by-Plan Feature Explorer with pagination.
"""
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from services.feature_explorer_service import PAGE_SIZE, PLAN_SYMBOLS

def explorer_landing_keyboard() -> InlineKeyboardMarkup:
    """PRD §5.4.2: Explorer plan selector."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆓 Free", callback_data="explore:plan:FREE:0"),
            InlineKeyboardButton("🚀 Starter", callback_data="explore:plan:STARTER:0")
        ],
        [
            InlineKeyboardButton("⭐ Pro", callback_data="explore:plan:PRO:0"),
            InlineKeyboardButton("👑 Creator", callback_data="explore:plan:CREATOR:0")
        ],
        [InlineKeyboardButton("💎 Compare Plans", callback_data="nav:plans")],
        [
            InlineKeyboardButton("◀️ Back", callback_data="nav:start"),
            InlineKeyboardButton("🏠 Home", callback_data="nav:home")
        ]
    ])

def explorer_page_keyboard(plan: str, page: int, total_pages: int, items: list) -> InlineKeyboardMarkup:
    """PRD §5.4.3 & §5.4.4: Paginated feature list."""
    keyboard = []
    
    # Feature item buttons
    for item in items:
        keyboard.append([InlineKeyboardButton(item["name"], callback_data=f"feat:view:{item['id']}:{plan}:{page}")])
    
    # Pagination Row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀ Previous", callback_data=f"explore:plan:{plan}:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ▶", callback_data=f"explore:plan:{plan}:{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)
        
    # Plan Quick Jump Row
    keyboard.append([
        InlineKeyboardButton("🆓", callback_data="explore:plan:FREE:0"),
        InlineKeyboardButton("🚀", callback_data="explore:plan:STARTER:0"),
        InlineKeyboardButton("⭐", callback_data="explore:plan:PRO:0"),
        InlineKeyboardButton("👑", callback_data="explore:plan:CREATOR:0")
    ])
    
    # Footer Row
    keyboard.append([
        InlineKeyboardButton("💎 View Plan Pricing", callback_data="nav:plans"),
        InlineKeyboardButton("◀️ Back", callback_data="explore:landing")
    ])
    return InlineKeyboardMarkup(keyboard)

def feature_detail_keyboard(plan: str, page: int) -> InlineKeyboardMarkup:
    """PRD §5.4.5: Feature Detail screen buttons."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 View Plans", callback_data="nav:plans")],
        [
            InlineKeyboardButton("◀️ Back to Features", callback_data=f"explore:plan:{plan}:{page}"),
            InlineKeyboardButton("🏠 Home", callback_data="nav:home")
        ]
    ])