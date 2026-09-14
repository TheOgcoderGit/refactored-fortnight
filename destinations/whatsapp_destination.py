"""
ChannelFlow AI - WhatsApp Channel Destination Adapter
========================================================

Publishes content to a WhatsApp Channel using Meta's WhatsApp Business
Cloud API (https://developers.facebook.com/docs/whatsapp/cloud-api).

Requires (user-provided, per-deployment or per-user):
    WHATSAPP_PHONE_NUMBER_ID  - from Meta Business Manager
    WHATSAPP_ACCESS_TOKEN     - permanent or long-lived token

Media handling:
    - WhatsApp Cloud API accepts media via public HTTPS URLs only
    - The caller (admin handlers) must upload media to a public CDN first
    - Media URL is passed as media_ref in DeliveryContent

Rate limits (per Meta docs):
    - 80 messages/second per phone number (burst)
    - 1000 messages/second sustained
    - Media upload: 100MB max per file

This adapter follows the BaseDestination interface.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from config import WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN
from destinations.base import BaseDestination, DeliveryContent, DeliveryResult

logger = logging.getLogger(__name__)


@dataclass
class WhatsAppConfig:
    phone_number_id: str
    access_token: str

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/v20.0/{self.phone_number_id}"


def _get_whatsapp_config() -> Optional[WhatsAppConfig]:
    if not WHATSAPP_PHONE_NUMBER_ID or not WHATSAPP_ACCESS_TOKEN:
        return None
    return WhatsAppConfig(
        phone_number_id=WHATSAPP_PHONE_NUMBER_ID,
        access_token=WHATSAPP_ACCESS_TOKEN,
    )


class WhatsAppChannelDestination(BaseDestination):
    """Sends content to a WhatsApp Channel via Cloud API."""

    platform_id = "whatsapp_channel"

    def __init__(self, channel_id: str, label: Optional[str] = None, config: Optional[WhatsAppConfig] = None):
        """
        Args:
            channel_id: The WhatsApp Channel ID (format: "1234567890@channel")
            label: Human-readable name
            config: Optional WhatsAppConfig (uses env vars if not provided)
        """
        self.channel_id = channel_id
        self.label = label or channel_id
        self.config = config or _get_whatsapp_config()

    def describe(self) -> str:
        return f"WhatsApp Channel: {self.label}"

    async def send(self, content: DeliveryContent) -> DeliveryResult:
        config = self.config
        if not config:
            return DeliveryResult(
                ok=False,
                detail="WhatsApp not configured. Set WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN."
            )

        url = f"{config.base_url}/messages"
        headers = {
            "Authorization": f"Bearer {config.access_token}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30) as http:
                if content.media_ref and content.media_type in ("photo", "video", "document"):
                    # Media message with URL
                    media_payload = {
                        "messaging_product": "whatsapp",
                        "to": self.channel_id,
                        "type": content.media_type,
                        content.media_type: {"link": content.media_ref},
                    }
                    if content.text:
                        # Caption for media
                        if content.media_type == "photo":
                            media_payload["photo"]["caption"] = content.text
                        elif content.media_type == "video":
                            media_payload["video"]["caption"] = content.text
                        elif content.media_type == "document":
                            media_payload["document"]["caption"] = content.text

                    resp = await http.post(url, json=media_payload, headers=headers)

                else:
                    # Text message
                    text_payload = {
                        "messaging_product": "whatsapp",
                        "to": self.channel_id,
                        "type": "text",
                        "text": {"body": content.text or ""},
                    }
                    resp = await http.post(url, json=text_payload, headers=headers)

                data = resp.json()

                if resp.status_code >= 400:
                    error_msg = data.get("error", {}).get("message", str(data))
                    logger.error("WhatsApp API error: %s", error_msg)
                    return DeliveryResult(ok=False, detail=error_msg)

                # Success - response contains messages array with IDs
                return DeliveryResult(ok=True, detail=str(data.get("messages", [{}])[0].get("id", "sent")))

        except httpx.HTTPError as e:
            logger.error("WhatsApp network error: %s", e)
            return DeliveryResult(ok=False, detail=f"Network error: {e}")
        except Exception as e:
            logger.exception("Unexpected error sending to WhatsApp channel %s", self.channel_id)
            return DeliveryResult(ok=False, detail=str(e))

    async def validate_connection(self) -> DeliveryResult:
        """Test if the WhatsApp credentials are valid by calling the account info endpoint."""
        config = self.config
        if not config:
            return DeliveryResult(ok=False, detail="WhatsApp not configured")

        url = f"{config.base_url}"
        headers = {"Authorization": f"Bearer {config.access_token}"}

        try:
            async with httpx.AsyncClient(timeout=15) as http:
                resp = await http.get(url, headers=headers)
                data = resp.json()

                if resp.status_code >= 400:
                    return DeliveryResult(ok=False, detail=data.get("error", {}).get("message", "Invalid credentials"))

                return DeliveryResult(ok=True, detail="WhatsApp connection valid")

        except Exception as e:
            logger.error("WhatsApp validation error: %s", e)
            return DeliveryResult(ok=False, detail=str(e))

    async def get_channel_info(self) -> Optional[dict]:
        """Get channel metadata (name, description, etc.)."""
        config = self.config
        if not config:
            return None

        # WhatsApp Channels use the /{channel_id} endpoint for info
        url = f"https://graph.facebook.com/v20.0/{self.channel_id}"
        headers = {"Authorization": f"Bearer {config.access_token}"}

        try:
            async with httpx.AsyncClient(timeout=15) as http:
                resp = await http.get(url, headers=headers)
                if resp.status_code < 400:
                    return resp.json()
        except Exception as e:
            logger.error("WhatsApp get_channel_info error: %s", e)

        return None


# Backwards-compatible factory function used by admin handlers
def create_whatsapp_destination(channel_id: str, label: str = None) -> WhatsAppChannelDestination:
    return WhatsAppChannelDestination(channel_id, label)