"""
ChannelFlow AI - Telegram Destination Adapter
================================================

Sends one piece of content to one Telegram chat_id, on demand, via the
shared Telethon client (core.client.client) - the same account that
powers the forward engine, and the one actually joined to the channels
this bot manages. This is what /post, /broadcast, and promotional
delivery use; it is a different call site from core/forwarder.py's live
per-message routing and doesn't touch it.

Why Telethon and not the Bot API for this
-------------------------------------------
The admin composes /post and /broadcast content through the Bot API
(python-telegram-bot), but Telegram destinations are chats the
*Telethon* account is a member/admin of, not necessarily the bot
account. So content authored via the Bot API has to be re-sent through
Telethon to actually land - which means Bot-API file_ids can't be
reused directly (they're not valid Telethon file references). The
caller downloads any media to a local temp file first (see
bot/admin_handlers.py) and passes that path as media_ref; this class
uploads it fresh and never touches raw bytes itself.
"""

import logging

from telethon.errors import RPCError, FloodWaitError

from core.client import client, ensure_started
from destinations.base import BaseDestination, DeliveryContent, DeliveryResult

logger = logging.getLogger(__name__)


class TelegramDestination(BaseDestination):

    platform_id = "telegram"

    def __init__(self, chat_id, label=None):
        self.chat_id = chat_id
        self.label = label or str(chat_id)

    def describe(self) -> str:
        return self.label

    async def send(self, content: DeliveryContent) -> DeliveryResult:

        await ensure_started()

        try:
            target = int(self.chat_id)
        except (TypeError, ValueError):
            target = self.chat_id

        try:

            if content.media_ref:
                await client.send_file(
                    target,
                    content.media_ref,
                    caption=content.text or "",
                )
            else:
                await client.send_message(target, content.text or "")

            return DeliveryResult(ok=True)

        except FloodWaitError as e:
            return DeliveryResult(
                ok=False, detail=f"Telegram asked to wait {e.seconds}s"
            )

        except RPCError as e:
            return DeliveryResult(ok=False, detail=str(e))

        except Exception as e:
            logger.exception(
                "Unexpected error sending to Telegram chat %s", self.chat_id
            )
            return DeliveryResult(ok=False, detail=str(e))
