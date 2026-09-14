"""
ChannelFlow AI - Destination Abstraction
==========================================

Common interface every publish target implements, so admin /post,
/broadcast, and promotional delivery can treat "send this content to N
targets" the same way regardless of platform.

This is deliberately NOT wired into core/forwarder.py's live per-message
routing. That engine has its own routing cache and dispatch loop tuned
for one job (project sources -> project's own Telegram destinations,
continuously, in real time) and it already works - see
core/forwarder.py's module docstring. This abstraction exists for the
on-demand, admin-driven sends (/post, /broadcast, promotional delivery)
and for the Instagram processing pipeline, which are new call sites, not
a replacement for the existing one.

Only TelegramDestination and InstagramDestination (destinations/*.py)
actually send anything. WhatsAppChannelDestination, PinterestDestination
and FacebookPageDestination are Phase-2-roadmap metadata placeholders
registered in services/platform_registry.py with status="coming_soon" -
nothing in this codebase constructs or calls them, and if something ever
did, NotImplementedError is what it would get, not a fake success.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class DeliveryContent:
    """
    Platform-agnostic content to deliver.

    media_ref meaning is destination-specific and documented on each
    concrete class:
        - TelegramDestination expects a local file path (or None).
        - InstagramDestination (feed publish) expects a public HTTPS
          image/video URL (or None) - the Graph API fetches media by
          URL server-side, it doesn't accept raw uploads.
    """

    text: Optional[str] = None
    media_type: Optional[str] = None  # "photo" | "video" | "document" | None
    media_ref: Optional[str] = None


@dataclass
class DeliveryResult:
    ok: bool
    detail: Optional[str] = None


class BaseDestination:
    """Every concrete destination implements `send`. `describe()` feeds
    the human-readable label stored in promotional_deliveries /
    publish_logs, so a delivery report is readable without joining back
    to five other tables."""

    platform_id: str = "base"

    async def send(self, content: DeliveryContent) -> DeliveryResult:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError
