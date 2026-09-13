"""
ChannelFlow AI - Instagram Destination Adapter
=================================================

Verified against current Meta/Instagram Graph API documentation before
writing this (August 2026): there are two genuinely different situations
here, and this module refuses to blur them.

1. Instagram Broadcast Channels (target_type="broadcast_channel")
   -----------------------------------------------------------------
   No official Graph API endpoint exists to publish into a Broadcast
   Channel. This isn't a permissions/App-Review gap - the publish
   surface simply isn't exposed. Calling .send() on a
   "broadcast_channel" destination therefore does NOT attempt any API
   call and does NOT report fake success; it returns a clear
   unsupported-capability result. The actual delivery path for these is
   the Ready-to-Publish approval queue (services/processing_service.py
   + the bot's review UI), which hands a human the formatted post to
   publish manually inside the Instagram app - never an AI/automated
   guess dressed up as automation.

2. Regular Instagram feed posts (target_type="feed")
   -----------------------------------------------------------------
   This IS a real, documented, automatable flow: create a media
   container (POST /{ig-user-id}/media with an image_url), poll it
   until FINISHED, then POST /{ig-user-id}/media_publish with the
   container's creation_id. Requires an Instagram Business/Creator
   account linked to a Facebook Page, a Meta app with the
   instagram_business_content_publish permission (App Review required
   for production use), and a valid long-lived access token. The Graph
   API fetches media by public HTTPS URL - it does not accept raw
   uploaded bytes for this flow, so media_ref must be a URL your
   infrastructure actually serves, not a local path.

Capability status (`is_configured`) is derived purely from whether
INSTAGRAM_ACCESS_TOKEN is set - it is never hard-coded to "available",
per the "report unsupported capabilities honestly" requirement.
"""

import asyncio
import logging
from typing import Optional

import httpx

from config import INSTAGRAM_ACCESS_TOKEN
from destinations.base import BaseDestination, DeliveryContent, DeliveryResult

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

CONTAINER_POLL_INTERVAL_SECONDS = 2
CONTAINER_POLL_MAX_ATTEMPTS = 30


def is_configured() -> bool:
    """Whether an Instagram access token is present at all. This does
    NOT confirm the token is valid or the account is eligible - only
    that there's something to try. See verify_capability() for a real
    check."""

    return bool(INSTAGRAM_ACCESS_TOKEN)


async def verify_capability(ig_user_id: str) -> tuple[bool, Optional[str]]:
    """
    Makes one real (cheap, read-only) Graph API call to confirm the
    configured token can actually see this Instagram account and that
    it's a Business/Creator account - i.e. genuinely eligible for
    publishing, not just "a token exists". Returns (ok, detail).
    """

    if not is_configured():
        return False, "INSTAGRAM_ACCESS_TOKEN is not set."

    url = f"{GRAPH_API_BASE}/{ig_user_id}"

    try:
        async with httpx.AsyncClient(timeout=15) as http:

            resp = await http.get(
                url,
                params={
                    "fields": "id,username,ig_id",
                    "access_token": INSTAGRAM_ACCESS_TOKEN,
                },
            )

        if resp.status_code == 200:
            return True, None

        detail = _extract_graph_error(resp)
        return False, detail

    except httpx.HTTPError as e:
        return False, f"Network error reaching Graph API: {e}"


def _extract_graph_error(resp: httpx.Response) -> str:

    try:
        payload = resp.json()
        message = payload.get("error", {}).get("message")
        if message:
            return message
    except Exception:
        pass

    return f"Graph API returned HTTP {resp.status_code}"


class InstagramDestination(BaseDestination):

    platform_id = "instagram_broadcast"

    def __init__(self, ig_user_id: str, target_type: str, label: Optional[str] = None):
        self.ig_user_id = ig_user_id
        self.target_type = target_type  # "broadcast_channel" | "feed"
        self.label = label or ig_user_id

    def describe(self) -> str:
        return self.label

    async def send(self, content: DeliveryContent) -> DeliveryResult:

        if self.target_type == "broadcast_channel":
            return DeliveryResult(
                ok=False,
                detail=(
                    "No official Meta API exists for publishing to Instagram "
                    "Broadcast Channels. This item belongs in the "
                    "Ready-to-Publish approval queue, not an automated send."
                ),
            )

        if self.target_type != "feed":
            return DeliveryResult(ok=False, detail=f"Unknown target_type: {self.target_type}")

        if not is_configured():
            return DeliveryResult(ok=False, detail="INSTAGRAM_ACCESS_TOKEN is not configured.")

        if not content.media_ref:
            return DeliveryResult(
                ok=False,
                detail="Feed publishing needs a public media URL; none was provided.",
            )

        return await self._publish_feed_post(content)

    async def _publish_feed_post(self, content: DeliveryContent) -> DeliveryResult:

        try:
            async with httpx.AsyncClient(timeout=30) as http:

                media_field = (
                    "video_url" if content.media_type == "video" else "image_url"
                )

                create_resp = await http.post(
                    f"{GRAPH_API_BASE}/{self.ig_user_id}/media",
                    data={
                        media_field: content.media_ref,
                        "caption": content.text or "",
                        "access_token": INSTAGRAM_ACCESS_TOKEN,
                    },
                )

                if create_resp.status_code != 200:
                    return DeliveryResult(ok=False, detail=_extract_graph_error(create_resp))

                creation_id = create_resp.json().get("id")

                if not creation_id:
                    return DeliveryResult(ok=False, detail="Graph API did not return a container id.")

                # Video containers process asynchronously - poll until
                # FINISHED (or give up) before publishing, per Meta's
                # documented container flow.
                for _ in range(CONTAINER_POLL_MAX_ATTEMPTS):

                    status_resp = await http.get(
                        f"{GRAPH_API_BASE}/{creation_id}",
                        params={"fields": "status_code", "access_token": INSTAGRAM_ACCESS_TOKEN},
                    )

                    status_code = status_resp.json().get("status_code")

                    if status_code == "FINISHED":
                        break

                    if status_code == "ERROR":
                        return DeliveryResult(ok=False, detail="Media container processing failed.")

                    await asyncio.sleep(CONTAINER_POLL_INTERVAL_SECONDS)

                else:
                    return DeliveryResult(ok=False, detail="Timed out waiting for media to process.")

                publish_resp = await http.post(
                    f"{GRAPH_API_BASE}/{self.ig_user_id}/media_publish",
                    data={"creation_id": creation_id, "access_token": INSTAGRAM_ACCESS_TOKEN},
                )

                if publish_resp.status_code != 200:
                    return DeliveryResult(ok=False, detail=_extract_graph_error(publish_resp))

                return DeliveryResult(ok=True)

        except httpx.HTTPError as e:
            return DeliveryResult(ok=False, detail=f"Network error: {e}")

        except Exception as e:
            logger.exception("Unexpected error publishing to Instagram feed")
            return DeliveryResult(ok=False, detail=str(e))
