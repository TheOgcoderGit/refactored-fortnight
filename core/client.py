"""
ChannelFlow AI - Shared Telethon Client (Non-Blocking)
"""
import asyncio
import logging

from telethon import TelegramClient
from config import API_ID, API_HASH, SESSION_NAME

logger = logging.getLogger(__name__)

client = TelegramClient(
    SESSION_NAME,
    API_ID,
    API_HASH,
    connection_retries=None,
    retry_delay=5,
    auto_reconnect=True,
)

_start_lock = asyncio.Lock()
_started = False


async def ensure_started():
    global _started

    if _started and client.is_connected():
        return client

    async with _start_lock:
        if _started and client.is_connected():
            return client

        await client.connect()

        if await client.is_user_authorized():
            _started = True
            me = await client.get_me()
            logger.info("Shared Telethon client authorized as %s (id=%s)", getattr(me, "username", None) or me.first_name, me.id)
        else:
            logger.info("Shared Telethon connected. Users connect via /connect in bot chat.")
            _started = True

    return client