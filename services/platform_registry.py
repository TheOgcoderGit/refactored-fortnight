"""
ChannelFlow AI - Platform Registry
=====================================

Single source of truth for every platform the project-creation UI,
admin platform-status view, and "Coming Soon" roadmap screen show.
Nothing else should hard-code a platform id, label, or status string -
look it up here so there's exactly one place to change when a
platform's status changes.

Status values:
    "available"       - real, working, can be selected right now.
    "setup_required"  - code path exists, but this deployment hasn't
                         configured the credentials it needs.
    "coming_soon"      - roadmap metadata only. No adapter, no
                         credential prompts, no API calls - see
                         destinations/base.py's module docstring.
    "deprecated"       - was selectable before, no longer is. Existing
                         projects on this platform_type keep working
                         (or are shown a clear notice) rather than
                         crashing; new projects can't pick it.

Instagram Broadcast and Facebook Page were removed from the roadmap
(platform priority migration): Instagram Broadcast never had an
official publish API to begin with (see destinations/
instagram_destination.py's docstring) and Facebook Page was dropped
from product scope. Both stay as "deprecated" entries - not deleted
outright - purely so any project a user created while they were
selectable doesn't crash on a KeyError; get_platform() still resolves
them.
"""

import os
from typing import Optional

from config import (
    INSTAGRAM_ACCESS_TOKEN,
    WHATSAPP_PHONE_NUMBER_ID,
    WHATSAPP_WABA_ID,
    WHATSAPP_ACCESS_TOKEN,
    THREADS_APP_ID,
    THREADS_APP_SECRET,
)


def _instagram_status() -> str:
    # Deprecated regardless of config - INSTAGRAM_ACCESS_TOKEN presence
    # no longer makes this selectable, it's just still checked so any
    # pre-existing "available"/"setup_required" wording that reads this
    # doesn't break.
    return "deprecated"


def _whatsapp_status() -> str:
    """WhatsApp Channel status depends on having WABA credentials configured."""
    if os.getenv("ENABLE_EXPERIMENTAL_PLATFORMS", "0") != "1":
        return "setup_required"
    if WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN:
        return "available"
    return "setup_required"


def _threads_status() -> str:
    """Threads status depends on having OAuth credentials configured."""
    if os.getenv("ENABLE_EXPERIMENTAL_PLATFORMS", "0") != "1":
        return "setup_required"
    if THREADS_APP_ID and THREADS_APP_SECRET:
        return "available"
    return "setup_required"


def get_platforms() -> dict:
    """Returns the full registry, computed fresh each call so status
    reflects the current environment config where relevant."""

    return {
        "telegram": {
            "id": "telegram",
            "name": "Telegram",
            "status": "available",
            "icon": "📡",
            "description": "Forward or copy messages between Telegram chats.",
        },
        "whatsapp_channel": {
            "id": "whatsapp_channel",
            "name": "WhatsApp Channel",
            "status": _whatsapp_status(),
            "icon": "🟢",
            "description": (
                "Cross-post a Telegram channel to a WhatsApp Channel. "
                "Uses Meta WhatsApp Business Cloud API."
            ),
        },
        "threads": {
            "id": "threads",
            "name": "Threads",
            "status": _threads_status(),
            "icon": "🧵",
            "description": "Publish posts to Threads via Meta Threads API.",
        },
        "pinterest": {
            "id": "pinterest",
            "name": "Pinterest",
            "status": "coming_soon",
            "icon": "📌",
            "description": "Publish Pins to Pinterest.",
        },
        "instagram_broadcast": {
            "id": "instagram_broadcast",
            "name": "Instagram Broadcast",
            "status": _instagram_status(),
            "icon": "📸",
            "description": "Removed from the platform roadmap.",
        },
        "facebook_page": {
            "id": "facebook_page",
            "name": "Facebook Page",
            "status": "deprecated",
            "icon": "📘",
            "description": "Removed from the platform roadmap.",
        },
    }


def get_platform(platform_id: str) -> Optional[dict]:
    return get_platforms().get(platform_id)


def is_selectable(platform_id: str) -> bool:
    """A platform can be picked for a new project only if it's actually
    available. "setup_required", "coming_soon", and "deprecated" all
    block creation, for different reasons the caller should explain."""

    platform = get_platform(platform_id)
    return bool(platform) and platform["status"] == "available"


# Destination-platform ids valid for a project's `platform_type` column.
# "instagram_broadcast" and "both" (its Telegram+Instagram combo) stay
# valid as STORED values only, for backward compatibility with any
# project created before the removal - they are not offered at
# creation time (see is_selectable above / bot/keyboards.py's
# platform_selection_keyboard, which reads status from this registry).
PROJECT_PLATFORM_CHOICES = ("telegram", "instagram_broadcast", "both", "whatsapp_channel", "threads")
