"""
ChannelFlow AI - Processing Listener (Converter Output -> Instagram Queue)
=============================================================================

Watches each active instagram_broadcast/both project's
``processing_chat_id`` - the Telegram channel the *external* affiliate-
converter bot posts its converted output into - and turns each new
message there into a processing_job (services/processing_service.py),
landing it in the Ready-to-Publish approval queue.

This is a second, independent ``@client.on(events.NewMessage)`` handler
on the same shared Telethon client used by core/forwarder.py. Telethon
dispatches an incoming update to every registered handler, so this is
purely additive: it has its own routing cache (_PROCESSING_ROUTES,
never touching forwarder.py's ``_ROUTES``), its own refresh loop, and
an early-exit on any chat that isn't a configured processing channel.
Nothing here can slow down or break the existing Telegram forwarding
path - see core/forwarder.py and Section 35 ("do not modify working
Telegram logic unnecessarily") of the build spec this was written
against.

What this handler does NOT do, on purpose:
    * It never calls the affiliate converter again - the message
      arriving here is already-converted output, taken as-is.
    * It never edits, re-derives, or "cleans up" any URL in the
      message text. See services/processing_service.extract_urls().
    * It never auto-publishes to Instagram Broadcast Channels - it only
      ever creates a READY job for a human to review (see
      destinations/instagram_destination.py for why).
"""

import asyncio
import logging

from telethon import events

from core.client import client, ensure_started
from database.db import get_connection
from services import processing_service

logger = logging.getLogger(__name__)

CACHE_REFRESH_SECONDS = 5

# chat_id (str) -> list of project rows (id, processing_chat_id, ...)
_PROCESSING_ROUTES = {}
_routes_lock = asyncio.Lock()


def _load_routes_sync():

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, processing_chat_id
        FROM projects
        WHERE status=1
          AND platform_type IN ('instagram_broadcast', 'both')
          AND processing_chat_id IS NOT NULL
          AND processing_chat_id != ''
        """
    )

    rows = cur.fetchall()
    conn.close()

    routes = {}

    for row in rows:
        routes.setdefault(str(row["processing_chat_id"]), []).append(row["id"])

    return routes


async def _refresh_routes():

    global _PROCESSING_ROUTES

    routes = await asyncio.to_thread(_load_routes_sync)

    async with _routes_lock:
        _PROCESSING_ROUTES = routes


async def _routes_refresh_loop():

    while True:
        await asyncio.sleep(CACHE_REFRESH_SECONDS)
        try:
            await _refresh_routes()
        except Exception:
            logger.exception("Failed to refresh processing-channel routes")


async def force_refresh_processing_routes():
    """Called right after a project's processing channel is set/changed
    in the bot UI, same pattern as forwarder.force_refresh_routes()."""

    await _refresh_routes()


def _media_info(message):

    if not message.photo and not message.video and not message.document:
        return None, 0

    if message.grouped_id:
        # Album membership isn't resolvable from a single event without
        # extra lookups; record it as a photo/video album by type and
        # let the review step pull the live message when an admin
        # actually looks at it, rather than guessing a count here.
        media_type = "video" if message.video else "photo"
        return f"{media_type}_album", 1

    if message.video:
        return "video", 1

    if message.photo:
        return "photo", 1

    return "document", 1


@client.on(events.NewMessage)
async def on_processing_channel_message(event):

    chat_key = str(event.chat_id)

    async with _routes_lock:
        project_ids = _PROCESSING_ROUTES.get(chat_key)

    if not project_ids:
        return

    message = event.message
    text = message.raw_text or ""
    media_type, media_count = _media_info(message)

    for project_id in project_ids:

        job_id, created = processing_service.create_job(
            project_id=project_id,
            converter_chat_id=chat_key,
            converter_message_id=message.id,
            media_type=media_type,
            media_count=media_count,
            text_content=text,
        )

        if not created:
            # Already processed this exact converter message - this is
            # the duplicate-protection path firing (restart, Telegram
            # sending the update twice, etc.), not an error.
            logger.debug(
                "Processing job for project %s / converter msg %s already exists (job %s)",
                project_id, message.id, job_id,
            )
            continue

        # Nothing left to derive - the converter already did its job,
        # and this handler doesn't touch URLs or re-run any conversion.
        # Straight to the Ready-to-Publish approval queue.
        processing_service.set_status(job_id, "READY")

        logger.info(
            "New processing job %s ready for review (project %s, converter msg %s)",
            job_id, project_id, message.id,
        )


async def start_processing_listener():
    """Called once from core/listener.py alongside start_forwarder(), on
    the same event loop / same client. Does not call
    client.run_until_disconnected() itself - core/forwarder.py already
    owns that; this only needs its cache populated and kept fresh."""

    await ensure_started()
    await _refresh_routes()

    asyncio.create_task(_routes_refresh_loop())

    logger.info("Processing listener running")
