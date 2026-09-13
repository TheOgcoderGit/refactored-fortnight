"""
ChannelFlow AI - Per-Owner Client Pool
=========================================

Once a user connects their own Telegram account via /connect
(core/user_sessions.py), their active projects should forward using
THEIR OWN account instead of the shared operator session
(core/client.py). This module is what actually makes that switch: for
every connected owner with at least one active project, it holds a
live TelegramClient built from their decrypted session, with the same
forward_message handler from core/forwarder.py registered on it, and
its own routing cache scoped to just that owner's projects
(core.forwarder.load_owner_routes).

Owners who have NOT connected are entirely unaffected - their projects
keep running on the shared client exactly as before (core/forwarder.py
excludes connected owners from its own routing query, so there's no
double-delivery).

Lifecycle
---------
* start_owner_engine(owner_id) - called right after a successful
  /connect (bot/handlers.py), and once per already-connected owner at
  process startup (core/listener.py). Safe to call again for an
  already-running owner (no-op).
* stop_owner_engine(owner_id) - called on /disconnect. Cancels the
  refresh loop and disconnects the client.
* Each pooled engine refreshes its own routes on the same
  CACHE_REFRESH_SECONDS cadence as the shared engine, and force-
  refreshes on demand the same way (force_refresh_owner_routes) so
  bot-driven edits (add source, start/stop project, etc.) show up
  immediately instead of waiting for the timer.

If an owner has zero active projects, no client is started for them at
all - there's nothing for it to do, and every live MTProto connection
has a real resource/rate-limit cost, so this only runs what's actually
needed.
"""

import asyncio
import logging

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from config import API_ID, API_HASH
from database.db import get_connection
from core.session_crypto import decrypt_session, SessionEncryptionNotConfigured
from core import forwarder

logger = logging.getLogger(__name__)

CACHE_REFRESH_SECONDS = 5

# owner_id -> {"client": TelegramClient, "routes": dict, "routes_lock": asyncio.Lock,
#              "refresh_task": asyncio.Task, "handler": callable}
_engines = {}
_engines_lock = asyncio.Lock()


def _get_encrypted_session(owner_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT encrypted_session FROM user_telegram_sessions WHERE telegram_id=? AND status='connected'",
        (owner_id,),
    )
    row = cur.fetchone()
    conn.close()

    return row["encrypted_session"] if row else None


def _owner_has_active_projects(owner_id) -> bool:

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT 1 FROM projects WHERE user_id=? AND status=1 LIMIT 1", (owner_id,))
    row = cur.fetchone()
    conn.close()

    return row is not None


async def start_owner_engine(owner_id):

    async with _engines_lock:

        if owner_id in _engines:
            return  # already running

        if not _owner_has_active_projects(owner_id):
            logger.info("Owner %s connected but has no active projects yet - not starting a client.", owner_id)
            return

        encrypted = _get_encrypted_session(owner_id)

        if not encrypted:
            logger.warning("start_owner_engine called for %s but no stored session found.", owner_id)
            return

        try:
            session_string = decrypt_session(encrypted)
        except SessionEncryptionNotConfigured:
            logger.exception("Could not decrypt session for owner %s", owner_id)
            return

        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

        try:
            await client.connect()

            if not await client.is_user_authorized():
                logger.error("Owner %s's stored session is no longer authorized - they need to /connect again.", owner_id)
                from services.telegram_ownership import require_reconnect
                require_reconnect(owner_id)
                await client.disconnect()
                return

        except Exception:
            logger.exception("Failed to connect owner %s's Telegram client", owner_id)
            try:
                await client.disconnect()
            except Exception:
                pass
            return

        routes = await forwarder.load_owner_routes(owner_id)
        routes_lock = asyncio.Lock()

        async def _handler(event):
            # Looks up the engine fresh on every event (rather than
            # closing over "routes" directly) so a route refresh is
            # picked up immediately without needing to re-register the
            # handler.
            async with _engines_lock:
                engine = _engines.get(owner_id)
            if engine is None:
                return
            await forwarder.forward_message(event, engine["client"], engine["routes"], engine["routes_lock"])

        client.add_event_handler(_handler, events.NewMessage)

        refresh_task = asyncio.create_task(_owner_refresh_loop(owner_id))

        _engines[owner_id] = {
            "client": client,
            "routes": routes,
            "routes_lock": routes_lock,
            "refresh_task": refresh_task,
            "handler": _handler,
        }

        logger.info("Started dedicated forwarding client for owner %s", owner_id)


async def stop_owner_engine(owner_id):

    async with _engines_lock:

        engine = _engines.pop(owner_id, None)

        if engine is None:
            return

        engine["refresh_task"].cancel()
        await asyncio.gather(engine["refresh_task"], return_exceptions=True)

        try:
            await engine["client"].disconnect()
        except Exception:
            pass

        logger.info("Stopped dedicated forwarding client for owner %s", owner_id)


async def force_refresh_owner_routes(owner_id):
    """Called by the bot after a create/start/stop/edit action on a
    connected owner's project, same purpose as
    core.forwarder.force_refresh_routes for the shared engine. A no-op
    if that owner doesn't have a running engine (e.g. not connected, or
    connected but no active projects yet).

    _handler (in start_owner_engine) re-fetches _engines[owner_id]
    fresh on every incoming event rather than closing over a routes
    dict at registration time, so simply rebinding "routes" here is
    enough for the next event to see it - no extra signalling needed.
    """

    async with _engines_lock:

        engine = _engines.get(owner_id)

        if engine is None:
            return

        routes = await forwarder.load_owner_routes(owner_id)

        async with engine["routes_lock"]:
            engine["routes"] = routes


async def _owner_refresh_loop(owner_id):

    while True:

        await asyncio.sleep(CACHE_REFRESH_SECONDS)

        async with _engines_lock:
            still_running = owner_id in _engines

        if not still_running:
            return

        await force_refresh_owner_routes(owner_id)


async def start_all_connected_owners():
    """Called once at process startup (core/listener.py), after the
    shared engine is already running, so every owner who connected
    before this boot gets their dedicated client back."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT telegram_id FROM user_telegram_sessions WHERE status='connected'")
    owner_ids = [row["telegram_id"] for row in cur.fetchall()]

    conn.close()

    for owner_id in owner_ids:
        try:
            await start_owner_engine(owner_id)
        except Exception:
            logger.exception("Failed to start engine for owner %s at startup", owner_id)


async def stop_all_engines():
    """Called on process shutdown (core/listener.stop_listener)."""

    async with _engines_lock:
        owner_ids = list(_engines.keys())

    for owner_id in owner_ids:
        await stop_owner_engine(owner_id)


def is_owner_engine_running(owner_id) -> bool:
    return owner_id in _engines
