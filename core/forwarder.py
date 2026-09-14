"""
ChannelFlow AI - Core Forward Engine
====================================
Handles:
- Event routing & Peer ID normalization (handles -100 IDs, raw IDs, private links)
- Message transformations: Text replacement -> Affiliate link replacer -> AI rewriting -> Formatting -> Watermarking
- Live Post Edit Synchronization (mirrors source edits to targets)
- Idempotent Auto Reactions
- Bounded retries with FloodWait backoff
"""

import asyncio
import logging
import random
import re
import time
from collections import defaultdict

from telethon import events, utils
from telethon.errors import FloodWaitError, RPCError

from core.client import client, ensure_started
from database.db import get_connection
from services import (
    affiliate_service,
    ai_service,
    auto_reaction_service,
    content_rules_service,
    dedup_service,
    formatting_service,
    forward_credit_service,
    log_service,
    plan_service,
    post_edit_sync_service,
    project_service,
    stats_service,
    text_replacement_service,
    watermark_service,
)

logger = logging.getLogger(__name__)

CACHE_REFRESH_SECONDS = 5
ALBUM_DEBOUNCE_SECONDS = 1.2
MAX_SEND_RETRIES = 3
RETRY_BACKOFF_BASE = 2

# Sentinel returned by _send_with_retry() when the outcome of a send is
# genuinely unknown (e.g. a network timeout) - as opposed to a definite
# failure. Telegram may have received and processed the message even
# though we never got the response, so this must NOT be treated the same
# as a normal failure: the forward credit and dedup claim must stay
# reserved (not released) so we never silently double-post on retry.
# See services/dedup_service.py's cleanup_expired_claims()/release() docs
# for the same "ambiguous vs definite failure" distinction.
_AMBIGUOUS_OUTCOME = object()

URL_RE = re.compile(r"https?://\S+")

_ROUTES = {}
_routes_lock = asyncio.Lock()
_project_locks = defaultdict(asyncio.Lock)
_dispatch_tasks = set()
_album_buffers = {}


def _build_route_for_project(conn, cur, project_id, owner_id):
    cur.execute("SELECT chat_id FROM sources WHERE project_id=? AND enabled=1", (project_id,))
    sources = [str(row["chat_id"]) for row in cur.fetchall()]
    if not sources:
        return None, None

    cur.execute("SELECT id, chat_id FROM destinations WHERE project_id=? AND enabled=1", (project_id,))
    dest_rows = cur.fetchall()

    destinations = []
    dest_ids = {}
    for r in dest_rows:
        cid_str = str(r["chat_id"])
        try:
            cid_int = int(cid_str)
            destinations.append(cid_int)
            dest_ids[cid_int] = r["id"]
        except ValueError:
            pass

    if not destinations:
        return None, None

    cur.execute("SELECT * FROM project_settings WHERE project_id=?", (project_id,))
    settings = cur.fetchone()
    if settings is None:
        cur.execute("INSERT INTO project_settings(project_id) VALUES(?)", (project_id,))
        conn.commit()
        cur.execute("SELECT * FROM project_settings WHERE project_id=?", (project_id,))
        settings = cur.fetchone()

    cur.execute("SELECT * FROM content_rules WHERE project_id=?", (project_id,))
    content_rules = cur.fetchone()

    route = {
        "project_id": project_id,
        "owner_id": owner_id,
        "destinations": destinations,
        "destination_ids": dest_ids,
        "settings": dict(settings),
        "content_rules": dict(content_rules) if content_rules else None,
    }
    return sources, route


def _load_routes_sync():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, user_id FROM projects WHERE status=1")
    active_projects = [(r["id"], r["user_id"]) for r in cur.fetchall()]

    routes = {}
    for pid, uid in active_projects:
        sources, route = _build_route_for_project(conn, cur, pid, uid)
        if route is None:
            continue
        for chat_id in sources:
            routes.setdefault(str(chat_id), []).append(route)
            # Normalize without -100
            clean = str(chat_id).replace("-100", "").replace("-", "")
            routes.setdefault(clean, []).append(route)
            routes.setdefault(f"-100{clean}", []).append(route)

    conn.close()
    return routes


def _load_owner_routes_sync(owner_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM projects WHERE status=1 AND user_id=?", (owner_id,))
    project_ids = [r["id"] for r in cur.fetchall()]

    routes = {}
    for pid in project_ids:
        sources, route = _build_route_for_project(conn, cur, pid, owner_id)
        if route is None:
            continue
        for chat_id in sources:
            routes.setdefault(str(chat_id), []).append(route)
            clean = str(chat_id).replace("-100", "").replace("-", "")
            routes.setdefault(clean, []).append(route)
            routes.setdefault(f"-100{clean}", []).append(route)

    conn.close()
    return routes


async def _refresh_routes():
    global _ROUTES
    try:
        routes = await asyncio.to_thread(_load_routes_sync)
        async with _routes_lock:
            _ROUTES = routes
    except Exception as e:
        logger.warning("Route refresh error: %s", e)


async def _routes_refresh_loop():
    while True:
        await _refresh_routes()
        await asyncio.sleep(CACHE_REFRESH_SECONDS)


async def force_refresh_routes():
    await _refresh_routes()


async def load_owner_routes(owner_id):
    return await asyncio.to_thread(_load_owner_routes_sync, owner_id)


def _media_type_of(message):
    if message.photo: return "photo"
    if message.voice: return "voice"
    if message.video_note: return "video_note"
    if message.gif: return "animation"
    if message.sticker: return "sticker"
    if message.video: return "video"
    if message.audio: return "audio"
    if message.document: return "document"
    if message.poll: return "poll"
    if message.text: return "text"
    return "other"


_REACTION_CACHE = {}
_REACTION_CACHE_TTL = 5.0


TELEGRAM_TEXT_LIMIT = 4096

# Entity types whose visible text hides a different destination URL
# (Telethon MessageEntityTextUrl). PRD §17.1 "Disable Hidden Links".
_HIDDEN_LINK_ENTITY_NAMES = frozenset({"MessageEntityTextUrl"})


def _is_hidden_link_entity(entity) -> bool:
    name = type(entity).__name__
    return name in _HIDDEN_LINK_ENTITY_NAMES or (
        name.startswith("MessageEntity") and "TextUrl" in name
    )


def _message_entity_pre_class():
    """Resolved lazily: the Telethon layout is not guaranteed at import time."""
    try:
        from telethon.tl.types import MessageEntityPre
        return MessageEntityPre
    except Exception:
        return None


def _text_send_kwargs(rep, outgoing, cfg):
    """Build the send-time presentation options for a plain text message.

    PRD §17.1 - link preview, mono text and hidden-link stripping are
    *send parameters*, not text mutations: the readable text must survive
    untouched. These options used to be configurable in the UI but were
    never passed to Telethon, so toggling them did nothing.

    Entity offsets are only reused when the outgoing text is byte-identical
    to the source text. Once prefix/suffix/AI/cleanup has changed the text,
    the original offsets would point at the wrong characters and Telegram
    would reject the message with an entity-bounds error, so they are
    dropped instead of silently corrupting the post.
    """
    source_text = rep.raw_text or ""
    unchanged = (outgoing or "") == source_text
    entities = list(getattr(rep, "entities", None) or []) if unchanged else []

    if cfg.get("disable_hidden_links"):
        entities = [e for e in entities if not _is_hidden_link_entity(e)]

    formatting_entities = None

    if cfg.get("mono") and outgoing:
        # Telegram counts entity offsets in UTF-16 code units.
        length = len(outgoing.encode("utf-16-le")) // 2
        pre_cls = _message_entity_pre_class()
        if pre_cls is not None:
            for builder in (
                lambda: pre_cls(offset=0, length=length, language=""),
                lambda: pre_cls(0, length, ""),
            ):
                try:
                    formatting_entities = [builder()]
                    break
                except Exception:
                    continue
        if formatting_entities is None:
            logger.warning("Mono text requested but MessageEntityPre is unavailable; sending plain text")
    elif cfg.get("disable_hidden_links"):
        formatting_entities = entities

    return {
        "link_preview": bool(cfg.get("link_preview", True)),
        "parse_mode": None,
        "formatting_entities": formatting_entities,
    }


def _reaction_for(project_id, side):
    """Return the configured reaction emoji for a project/side, or None.

    ``side`` is "source" or "destination". Returns None when auto reactions
    are disabled for the project or not configured for that side, so callers
    can simply skip. Cached briefly: this runs on every forwarded message and
    the setting only changes through the UI.
    """
    now = time.monotonic()
    cached = _REACTION_CACHE.get(project_id)
    if cached and cached[0] > now:
        row = cached[1]
    else:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT enabled, emojis, apply_source, apply_target FROM auto_reactions "
                "WHERE project_id=? ORDER BY id LIMIT 1",
                (project_id,),
            ).fetchone()
        except Exception:
            logger.exception("Auto reaction lookup failed for project %s", project_id)
            row = None
        finally:
            conn.close()
        row = dict(row) if row is not None else None
        _REACTION_CACHE[project_id] = (now + _REACTION_CACHE_TTL, row)

    if not row or not row.get("enabled"):
        return None
    if side == "source" and not row.get("apply_source"):
        return None
    if side == "destination" and not row.get("apply_target"):
        return None
    emoji = (row.get("emojis") or "").strip()
    return emoji or None


def _passes_all_filters(message, settings, content_rules=None, sender_id=None):
    media_filter = settings.get("media_filter") or "all"
    if media_filter != "all" and _media_type_of(message) != media_filter:
        return False

    text = (message.raw_text or "").lower()
    whitelist = [w.strip().lower() for w in (settings.get("keyword_whitelist") or "").split(",") if w.strip()]
    if whitelist and not any(w in text for w in whitelist):
        return False

    blacklist = [w.strip().lower() for w in (settings.get("keyword_blacklist") or "").split(",") if w.strip()]
    if blacklist and any(w in text for w in blacklist):
        return False

    regex_pat = (settings.get("regex_filter") or "").strip()
    if regex_pat:
        try:
            if re.search(regex_pat, message.raw_text or "") is None:
                return False
        except re.error:
            pass

    if content_rules:
        urls = URL_RE.findall(message.raw_text or "")
        if not content_rules_service.passes_content_rules(message.raw_text or "", urls, sender_id, content_rules):
            return False

    return True


async def _telegram_send(method, *args, **kwargs):
    """Call a Telethon send helper, dropping ``noforwards`` when the installed
    Telethon build does not accept it.

    ``inspect.signature`` only *probes* the helper's parameters, so a failure
    there must never block the send itself - probing is best-effort and the
    real API call is the source of truth about which arguments are accepted.
    """
    import inspect
    try:
        parameters = inspect.signature(method).parameters
        supported = (
            "noforwards" in parameters
            or any(p.kind is p.VAR_KEYWORD for p in parameters.values())
        )
    except (TypeError, ValueError):
        # Not introspectable (C function, wrapped object, test double).
        # Let the call itself decide.
        supported = True
    if not supported:
        kwargs.pop("noforwards", None)
    return await method(*args, **kwargs)


async def _send_with_retry(coro_factory, project_id, destination):
    for attempt in range(MAX_SEND_RETRIES + 1):
        try:
            return await coro_factory()
        except FloodWaitError as error:
            if attempt == MAX_SEND_RETRIES or error.seconds > 60:
                log_service.add_log(project_id, "error", f"Rate limit: wait {error.seconds}s")
                return None
            await asyncio.sleep(error.seconds + 1)
        except RPCError as error:
            if getattr(error, "code", 0) < 500 or attempt == MAX_SEND_RETRIES:
                log_service.add_log(project_id, "error", f"{type(error).__name__}: {error}")
                return None
            await asyncio.sleep(RETRY_BACKOFF_BASE ** (attempt + 1))
        except (OSError, asyncio.TimeoutError):
            return _AMBIGUOUS_OUTCOME
    return None


async def _dispatch(messages, route, client):
    async with _project_locks[route["project_id"]]:
        owner = project_service.get_project(route["project_id"])
        if not owner or not owner["status"]:
            return
        if any(getattr(m, "noforwards", False) for m in messages):
            stats_service.increment(route["project_id"], "filtered")
            return
        try:
            await _dispatch_impl(messages, route, client)
        except Exception as e:
            logger.warning("Dispatch error in project %s: %s", route["project_id"], e)
            stats_service.increment(route["project_id"], "failed")


async def _dispatch_impl(messages, route, client):
    project_id = route["project_id"]
    settings = route["settings"]
    rep = messages[0]
    sender_id = getattr(rep, "sender_id", None)

    if not _passes_all_filters(rep, settings, route.get("content_rules"), sender_id):
        stats_service.increment(project_id, "filtered")
        return

    owner_id = route.get("owner_id")
    delay_min = float(settings.get("delay_min") or 0)
    delay_max = float(settings.get("delay_max") or 0)
    if delay_max > 0:
        await asyncio.sleep(random.uniform(delay_min, delay_max))

    mode = settings.get("mode") or "forward"
    silent = bool(settings.get("silent"))
    protect_content = bool(settings.get("protect_content"))

    # Text Replacement -> Affiliate link replacement -> AI rewriting
    working_text = rep.raw_text or ""
    if mode == "copy" and working_text:
        working_text = text_replacement_service.apply_text_replacement(working_text, project_id)
        working_text = affiliate_service.replace_affiliate_links(working_text, project_id, owner_id)

        # AI Rewriting (if entitled and enabled)
        if plan_service.has_feature(owner_id, "ai_rewrite"):
            ai_cfg = ai_service.ensure_ai_settings(project_id)
            if ai_cfg.get("enabled"):
                res = await ai_service.rewrite_content(working_text, ai_cfg, user_id=owner_id, project_id=project_id)
                if res.success and res.text:
                    working_text = res.text

    # Telegram hard-rejects a plain text message over 4096 characters.
    # Still reserve/release the credit normally (rather than skip
    # reservation entirely) so it shows up as a wash, not silently ignored.
    too_long = (
        mode == "copy" and working_text
        and _media_type_of(rep) in ("text", "other")
        and len(working_text) > TELEGRAM_TEXT_LIMIT
    )

    # Watermark for photos in copy mode
    wm_bytes = None
    if mode == "copy" and _media_type_of(rep) == "photo":
        try:
            wm_cfg = watermark_service.ensure_watermark_settings(project_id)
            if wm_cfg.get("enabled"):
                import io as _io
                buf = _io.BytesIO()
                await client.download_media(rep, file=buf)
                data = buf.getvalue()
                if data:
                    wm_bytes, applied = watermark_service.apply_watermark(data, project_id)
                    if not applied:
                        wm_bytes = None
        except Exception:
            wm_bytes = None

    # Atomic Quota Reservation
    reservation = forward_credit_service.reserve(
        owner_id, project_id, f"tg:{owner_id}:{project_id}:{rep.chat_id}:{rep.id}"
    )
    if not reservation:
        stats_service.increment(project_id, "filtered")
        return

    published_any = False
    ambiguous_any = False

    if too_long:
        log_service.add_log(project_id, "error", f"Message too long ({len(working_text)} chars), skipped")
        stats_service.increment(project_id, "filtered")
    else:
        for dest in route["destinations"]:
            dest_row_id = route.get("destination_ids", {}).get(dest)
            cfg = formatting_service.get_advanced(project_id, dest_row_id)
            outgoing = working_text

            if mode == "copy":
                try:
                    outgoing = formatting_service.apply_formatting(project_id, working_text, dest_row_id)
                except Exception:
                    outgoing = working_text

            # Formatting can push a message past Telegram's hard limit even
            # when the source text was fine. Reject it explicitly rather than
            # letting Telethon fail the send after the unit was reserved.
            if mode == "copy" and len(outgoing or "") > TELEGRAM_TEXT_LIMIT:
                log_service.add_log(
                    project_id, "error",
                    f"Formatted output too long ({len(outgoing)} chars), skipped",
                )
                stats_service.increment(project_id, "filtered")
                continue

            dedup_claim = dedup_service.claim(
                source_platform="telegram",
                source_account_id=owner_id,
                source_channel_id=rep.chat_id,
                source_message_id=rep.id,
                project_id=project_id,
                destination_id=dest,
            )
            if not dedup_claim:
                continue

            async def _send(destination=dest):
                if mode == "forward":
                    return await _telegram_send(
                        client.forward_messages, destination, messages, silent=silent, noforwards=protect_content
                    )

                if len(messages) > 1:
                    files = [m.media for m in messages if m.media]
                    caption = outgoing or ""
                    return await _telegram_send(
                        client.send_file, destination, files, caption=caption, silent=silent, noforwards=protect_content
                    )

                if wm_bytes is not None:
                    import io as _io
                    return await _telegram_send(
                        client.send_file, destination, _io.BytesIO(wm_bytes), caption=outgoing or "", silent=silent, noforwards=protect_content
                    )

                if rep.media:
                    return await _telegram_send(
                        client.send_file, destination, rep.media, caption=outgoing or "", silent=silent, noforwards=protect_content
                    )

                return await _telegram_send(
                    client.send_message, destination, outgoing or "",
                    silent=silent, noforwards=protect_content,
                    **_text_send_kwargs(rep, outgoing, cfg),
                )

            sent = await _send_with_retry(_send, project_id, dest)
            if sent is _AMBIGUOUS_OUTCOME:
                # Unknown outcome - leave the dedup claim and credit reservation
                # exactly as they are (do NOT release/finish them) so a retry
                # is blocked until this is resolved, instead of risking a
                # duplicate post.
                ambiguous_any = True
                log_service.add_log(project_id, "warning", f"Ambiguous send to {dest}, holding reservation for manual review")
                continue
            if sent is not None:
                published_any = True
                sent_msg_id = getattr(sent, "id", None) or (sent[0].id if isinstance(sent, list) else 0)
                dedup_service.confirm(dedup_claim, str(sent_msg_id))

                # Post Edit Sync mapping (PRD §14.1). Only recorded when the
                # task opted in, so the mapping table does not grow for the
                # overwhelming majority of projects that never edit.
                if sent_msg_id and settings.get("post_edit_sync"):
                    post_edit_sync_service.record_mapping(
                        project_id, str(rep.chat_id), rep.id, str(dest), sent_msg_id
                    )

                # Auto Reaction (PRD §14.3). Respects the per-project enable
                # flag and the destination/source side selection - previously
                # any row in auto_reactions fired a reaction even when the
                # feature was switched off.
                if sent_msg_id and _reaction_for(project_id, "destination"):
                    await auto_reaction_service.apply_auto_reaction(
                        client, project_id, dest, sent_msg_id, _reaction_for(project_id, "destination")
                    )
                if rep.chat_id and _reaction_for(project_id, "source"):
                    await auto_reaction_service.apply_auto_reaction(
                        client, project_id, rep.chat_id, rep.id, _reaction_for(project_id, "source")
                    )

                stats_service.increment(project_id, "forwarded", bump_daily=False)
                log_service.add_log(project_id, "forward", f"Relayed {rep.chat_id} -> {dest}")
            else:
                dedup_service.release(dedup_claim)

    if not ambiguous_any:
        forward_credit_service.finish(reservation, published_any)
    # else: leave the reservation 'reserved' - resolved manually/by a
    # reconciliation job, never auto-retried (see _AMBIGUOUS_OUTCOME above).


# Post Edit Sync Handler
async def sync_source_edit(event, client, routes, routes_lock):
    message = event.message
    if not message:
        return

    raw_id = str(event.chat_id)
    plain_id = raw_id.replace("-100", "").replace("-", "")
    full_id = f"-100{plain_id}"

    matched_routes = []
    async with routes_lock:
        for candidate in (raw_id, full_id, f"-{plain_id}", plain_id):
            if candidate in routes:
                matched_routes = routes[candidate]
                break

    if not matched_routes:
        return

    for route in matched_routes:
        pid = route["project_id"]
        owner_id = route.get("owner_id")
        # PRD §14.1 - opt-in only. Editing an already-published post is a
        # surprising side effect, so a task without the flag never touches
        # destination messages.
        if not route["settings"].get("post_edit_sync"):
            continue

        new_text = message.raw_text or ""
        if route["settings"].get("mode") == "copy":
            new_text = affiliate_service.replace_affiliate_links(new_text, pid, owner_id)
            new_text = formatting_service.apply_formatting(pid, new_text)

        await post_edit_sync_service.sync_message_edit(client, pid, raw_id, message.id, new_text)


# Message Event Handler
async def forward_message(event, client, routes, routes_lock):
    message = event.message
    if not message:
        return

    raw_id = str(event.chat_id)
    plain_id = raw_id.replace("-100", "").replace("-", "")
    full_id = f"-100{plain_id}"

    matched_routes = []
    async with routes_lock:
        for candidate in (raw_id, full_id, f"-{plain_id}", plain_id):
            if candidate in routes:
                matched_routes = routes[candidate]
                break

    if not matched_routes:
        return

    for route in matched_routes:
        keep_albums = bool(route["settings"].get("keep_media_groups"))
        if message.grouped_id and keep_albums:
            key = (message.chat_id, message.grouped_id, route["project_id"])
            if key not in _album_buffers:
                _album_buffers[key] = {
                    "messages": [message],
                    "route": route,
                    "client": client,
                    "task": asyncio.create_task(_flush_album(key)),
                }
            else:
                _album_buffers[key]["messages"].append(message)
        else:
            task = asyncio.create_task(_dispatch([message], route, client))
            _dispatch_tasks.add(task)
            task.add_done_callback(_dispatch_tasks.discard)


async def _flush_album(key):
    await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
    buf = _album_buffers.pop(key, None)
    if not buf:
        return
    msgs = sorted(buf["messages"], key=lambda m: m.id)
    await _dispatch(msgs, buf["route"], buf["client"])


async def start_forwarder():
    await ensure_started()
    await _refresh_routes()
    asyncio.create_task(_routes_refresh_loop())

    async def _on_new(event):
        await forward_message(event, client, _ROUTES, _routes_lock)

    async def _on_edit(event):
        await sync_source_edit(event, client, _ROUTES, _routes_lock)

    # Listen to both incoming and outgoing (in case user posts directly in source)
    client.add_event_handler(_on_new, events.NewMessage(incoming=None))
    client.add_event_handler(_on_edit, events.MessageEdited(incoming=None))
    logger.info("Forward Engine running live.")
    await client.run_until_disconnected()


async def stop_dispatches():
    tasks = list(_dispatch_tasks) + [b["task"] for b in _album_buffers.values()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _dispatch_tasks.clear()
    _album_buffers.clear()