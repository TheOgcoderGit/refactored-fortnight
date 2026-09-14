# ChannelFlow AI - Turnkey Niche Project Templates Service
# ========================================================
# Pre-loads 2 verified source channels and auto-configures pipeline rules.

import logging
from database.db import get_connection
from core.telegram_utils import get_chat
from services.project_service import create_project
from services.source_service import add_source
from services.destination_service import add_destination
from services.settings_service import update_settings
from services.formatting_service import configure as configure_formatting
from services.affiliate_service import update_affiliate_settings

logger = logging.getLogger(__name__)

# Niche Templates Catalog with 2 Pre-loaded Sources each
TEMPLATES_CATALOG = {
    "deals": {
        "title": "💰 Loot & Affiliate Deals",
        "default_name": "Loot Deals Automation",
        "sources": [
            ("@Deals99ai", "Live Shopping Deals"),
            ("stylewithakhil", "Top Discount Alerts")
        ],
        "desc": (
            "• Delivery Mode: Copy Mode\n"
            "• Link Preview: ON\n"
            "• Affiliate Replacer: ON (Amazon, Flipkart, Meesho)\n"
            "• Pre-loaded Sources: @Deals99ai, @stylewithakhil"
        ),
        "settings": {"mode": "copy"},
        "formatting": {"link_preview": True},
        "affiliate": {"enabled": True, "amazon_enabled": True, "flipkart_enabled": True, "meesho_enabled": True}
    },
    "news": {
        "title": "📰 Tech & Breaking News",
        "default_name": "News Distribution",
        "sources": [
            ("@TechNewsDailyLive", "Tech News Live"),
            ("@BreakingNewsGlobal", "Global News Desk")
        ],
        "desc": (
            "• Delivery Mode: Copy Mode\n"
            "• Strip Handles: ON (Removes @competing handles)\n"
            "• Branding Header: 📰 Breaking News\n"
            "• Pre-loaded Sources: @TechNewsDailyLive, @BreakingNewsGlobal"
        ),
        "settings": {"mode": "copy"},
        "formatting": {"remove_usernames": True, "header": "📰 Breaking News", "link_preview": True},
        "affiliate": {"enabled": False}
    },
    "crypto": {
        "title": "📈 Crypto & Web3 Signals",
        "default_name": "Crypto Alerts",
        "sources": [
            ("@CryptoSignalsAlert", "Crypto Market Feed"),
            ("@BitcoinAlertsHub", "Bitcoin Updates")
        ],
        "desc": (
            "• Delivery Mode: Copy Mode\n"
            "• Safe Pacing: 2-5s Jitter Delay\n"
            "• Branding Header: ⚡ Crypto Alert\n"
            "• Pre-loaded Sources: @CryptoSignalsAlert, @BitcoinAlertsHub"
        ),
        "settings": {"mode": "copy", "delay_min": 2.0, "delay_max": 5.0},
        "formatting": {"header": "⚡ Crypto Alert", "link_preview": True},
        "affiliate": {"enabled": False}
    },
    "movies": {
        "title": "🎬 Movies & Entertainment",
        "default_name": "Entertainment Feed",
        "sources": [
            ("@CinemaUpdatesDaily", "Cinema Hub"),
            ("@MovieTrailersHub", "Movie Trailers & Posters")
        ],
        "desc": (
            "• Delivery Mode: Copy Mode\n"
            "• Group Albums: ON (Keeps posters & media grouped)\n"
            "• Link Preview: ON\n"
            "• Pre-loaded Sources: @CinemaUpdatesDaily, @MovieTrailersHub"
        ),
        "settings": {"mode": "copy", "keep_media_groups": 1},
        "formatting": {"link_preview": True},
        "affiliate": {"enabled": False}
    }
}


async def create_template_project(user_id: int, template_key: str, target_chat: dict,
                                  sources=None) -> int:
    """Creates a project, pre-adds the niche source channels, adds target, and applies rules.

    ``sources`` lets the caller pass an edited list (the user can add or
    remove pre-configured channels before confirming). It defaults to the
    template's own list.
    """
    tpl = TEMPLATES_CATALOG.get(template_key)
    if not tpl:
        raise ValueError(f"Unknown template key: {template_key}")

    if sources is None:
        sources = tpl["sources"]

    # 1. Create Project
    pid = create_project(user_id, tpl["default_name"], platform_type="telegram")

    # 2. Add Pre-loaded Niche Sources (2 Channels)
    for src_handle, src_desc in sources:
        try:
            src_chat = await get_chat(src_handle, user_id=user_id)
            if src_chat:
                add_source(pid, src_chat["chat_id"], src_chat["username"], src_chat["title"], src_chat["type"])
            else:
                add_source(pid, src_handle, src_handle.lstrip("@"), src_desc, "Channel")
        except Exception:
            add_source(pid, src_handle, src_handle.lstrip("@"), src_desc, "Channel")

    # 3. Add Destination Target
    add_destination(pid, target_chat["chat_id"], target_chat["username"], target_chat["title"], target_chat["type"])

    # 4. Apply Pre-configured Niche Settings
    update_settings(pid, **tpl["settings"])
    configure_formatting(user_id, pid, tpl["formatting"])
    if tpl.get("affiliate"):
        update_affiliate_settings(pid, **tpl["affiliate"])

    return pid