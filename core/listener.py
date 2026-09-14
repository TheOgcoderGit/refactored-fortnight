"""
Forward-engine lifecycle.

Runs as a plain ``asyncio.Task`` on the *same* event loop as the
Telegram bot (python-telegram-bot), scheduled from ``main.py``'s
``post_init`` hook.

Why this matters
-----------------
An earlier version of this file started the forward engine on a
separate OS thread, with its own ``asyncio.run(...)`` (its own event
loop). Forwarding itself worked fine there, but the shared Telethon
``client`` (``core.client.client``) got connected/authorized on that
thread's loop. Any on-demand Telethon call made from the *bot's* loop
(e.g. resolving a channel via ``get_chat`` when adding a source or
destination) was then reaching into a client bound to a different loop,
which hangs or fails silently - Telethon clients are not safe to drive
from two event loops at once. That was the "sources/destinations won't
add" bug: forwarding kept working, on-demand lookups from the bot did
not.

The fix is architectural, not a patch: there is only one event loop in
the whole process now, so there's nothing to bridge.
"""
import asyncio
import logging
from typing import Optional

from core.forwarder import start_forwarder
from core.processing_listener import start_processing_listener
from core import client_pool
from core.subscription_scheduler import run_subscription_scheduler
from bot import notifier

logger = logging.getLogger(__name__)

_forward_task: Optional[asyncio.Task] = None
_processing_task: Optional[asyncio.Task] = None
_owner_engines_task: Optional[asyncio.Task] = None
_scheduler_task: Optional[asyncio.Task] = None
_queue_worker_task: Optional[asyncio.Task] = None

_INITIAL_BACKOFF = 5
_MAX_BACKOFF = 60


async def _run_forever() -> None:
    """Async equivalent of the old thread's restart-with-backoff loop."""

    delay = _INITIAL_BACKOFF

    while True:
        try:
            await start_forwarder()
            # run_until_disconnected() returned normally (a clean,
            # intentional disconnect) - don't spin-loop restarting it.
            break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(
                "Forward engine crashed; restarting in %ss", delay
            )

            hint = (
                "The Telethon session isn't authorized. Run "
                "`python -m core.authorize` on the server once, then "
                "restart the bot."
                if isinstance(e, RuntimeError) and "not authorized" in str(e).lower()
                else str(e)
            )

            await notifier.notify_admins(
                "🔴 ChannelFlow AI\n\n"
                "The forward engine crashed and is retrying in the "
                f"background:\n{hint}\n\n"
                "No messages will forward until this is resolved.",
                throttle_key="engine_crash",
            )

            await asyncio.sleep(delay)
            delay = min(delay * 2, _MAX_BACKOFF)


async def _run_processing_listener_safe() -> None:
    """Starts the processing listener with its own error boundary, so a
    problem here (e.g. a bad processing_chat_id) can never take down the
    forward engine - they share a client but are otherwise independent."""

    try:
        await start_processing_listener()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Processing listener (converter-output -> Instagram queue) "
            "failed to start; Telegram forwarding is unaffected."
        )


async def _start_owner_engines_safe() -> None:
    """Brings back a dedicated client for every user who connected
    their own Telegram account (core/user_sessions.py) before this
    boot. Runs once (unlike the two loops above) - start_owner_engine
    connects and registers handlers but doesn't block, so there's
    nothing to keep looping on here; future connects are started
    directly from bot/handlers.py's connect success path. Its own
    error boundary for the same reason as the processing listener: a
    bad session for one owner must never take down the shared engine
    or any other owner's client."""

    try:
        await client_pool.start_all_connected_owners()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Failed to start one or more per-owner forwarding clients; "
            "the shared engine and unaffected owners continue normally."
        )


async def _run_subscription_scheduler_safe(bot) -> None:
    """Same error-boundary pattern as the processing listener and
    owner-engines startup - a scheduler problem must never take down
    Telegram forwarding."""

    try:
        await run_subscription_scheduler(bot)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Subscription scheduler crashed; forwarding is unaffected, but "
            "expiry reminders/auto-renew/downgrades have stopped running."
        )


# ==========================================
# QUEUE WORKER (BUG-006 fix)
# ==========================================
# Drains the persistent jobs table (services/job_queue.py). Without this,
# enqueued jobs sat in 'pending' forever. One worker handles every job
# type with per-type dispatch; job handlers that don't exist yet are
# dead-lettered honestly instead of silently retried forever.

_QUEUE_POLL_SECONDS = 2

_JOB_TYPE_HANDLERS = {}  # populated by register_job_handler()


def register_job_handler(job_type: str, handler):
    """Register an async callable(payload) -> None for a job type."""
    _JOB_TYPE_HANDLERS[job_type] = handler


async def _handle_notification_job(payload):
    """Deliver a notification DM."""
    from bot import notifier as _notifier
    user_id = payload.get("user_id")
    text = payload.get("text", "")
    if user_id and text:
        await _notifier.notify_user(user_id, text)


register_job_handler("notification", _handle_notification_job)


# ---- External platform publishing (WhatsApp / Threads) ----

class _PublishError(Exception):
    """Carries a retryable flag + error code for the queue."""

    def __init__(self, message, code="TEMPORARY", retryable=True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


async def _handle_publish_job(payload):
    """Publishes one item to WhatsApp or Threads via the user's stored
    platform account. Raises _PublishError so queue fail() classifies
    retries vs dead-letter honestly (never fake success)."""

    from services import platform_accounts_service as PA
    import httpx as _httpx

    platform = payload.get("platform")
    owner_id = payload.get("owner_id")
    account_identifier = payload.get("account_identifier")
    text = payload.get("text", "")

    if not text.strip():
        return  # nothing to publish - not an error

    # Platform maintenance check (admin control plane)
    try:
        from database.db import get_connection as _gc
        c = _gc()
        row = c.execute(
            "SELECT state FROM platform_status WHERE platform=?", (platform,)
        ).fetchone()
        c.close()
        if row is not None and row["state"] != "enabled":
            raise _PublishError(
                f"{platform} is disabled/maintenance",
                code="PLATFORM_UNAVAILABLE", retryable=False,
            )
    except _PublishError:
        raise
    except Exception:
        pass  # control-plane read failed - don't block publishing on it

    if platform == "whatsapp_channel":
        accounts = {a["id"]: a for a in PA.get_accounts(owner_id or 0, platform)}
        acct = accounts.get(payload.get("platform_account_id")) or next(
            (a for a in accounts.values()
             if str(a["account_identifier"]) == str(account_identifier)),
            None,
        )
        if not acct:
            raise _PublishError("WhatsApp account disconnected",
                                code="AUTHENTICATION", retryable=False)

        creds = PA.get_credentials(acct)
        token = creds.get("access_token")
        phone_id = creds.get("phone_number_id") or acct["account_identifier"]

        url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # Apply WhatsApp content transformation
        project_id = payload.get("project_id")
        raw_text = payload.get("text", "")
        media_info = payload.get("media_info", [])
        ai_settings = payload.get("ai_settings")
        has_formatting = payload.get("formatting_rules", False)

        # Get project settings for AI and formatting
        ai_settings_dict = None
        if ai_settings:
            from services import ai_service
            ai_settings_dict = ai_service.ensure_ai_settings(project_id)

        formatting_rules_dict = None
        if has_formatting:
            from services import formatting_service
            formatting_rules_dict = formatting_service.get_rules(project_id)

        # Extract and process media info for WhatsApp
        # media_info contains Telethon media objects; we need to prepare
        # CDN URLs for WhatsApp Cloud API which accepts media via public HTTPS URLs
        media_list = []
        has_media = len(media_info) > 0

        if has_media:
            from services.whatsapp_content import extract_media_from_telegram_message
            # media_info is a list of dicts from the forwarder; convert to
            # the format expected by whatsapp_content.extract_media_from_telegram_message
            # Each entry can have 'type' and 'media' (Telethon object) keys
            # For now, extract basic info and create placeholder CDN URLs
            for media_entry in media_info:
                m_type = media_entry.get("type", "")
                m_media = media_entry.get("media")
                m_caption = media_entry.get("caption", raw_text or "")
                if m_type == "photo" and m_media:
                    # In production, this would be uploaded to CDN
                    # For now, use a placeholder based on message ID
                    media_list.append({
                        "type": "photo",
                        "url": f"https://cdn.channelflow.ai/media/photo/{m_media.id}.jpg",
                        "caption": m_caption,
                    })
                elif m_type == "video" and m_media:
                    media_list.append({
                        "type": "video",
                        "url": f"https://cdn.channelflow.ai/media/video/{m_media.id}.mp4",
                        "caption": m_caption,
                    })
                elif m_type == "document" and m_media:
                    media_list.append({
                        "type": "document",
                        "url": f"https://cdn.channelflow.ai/media/document/{m_media.id}",
                        "caption": m_caption,
                    })
                elif m_type == "animation" and m_media:
                    media_list.append({
                        "type": "animation",
                        "url": f"https://cdn.channelflow.ai/media/animation/{m_media.id}.gif",
                        "caption": m_caption,
                    })

        # Prepare WhatsApp content with transformations
        from services.whatsapp_content import prepare_whatsapp_content

        prepared = await prepare_whatsapp_content(
            project_id=project_id,
            text=raw_text,
            parse_mode=None,
            media_list=media_list if media_list else None,
            ai_settings=ai_settings_dict,
            formatting_rules=formatting_rules_dict,
        )

        url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = {
            "messaging_product": "whatsapp",
            "to": account_identifier,
            "type": "text",
            "text": {"body": prepared.text[:4096]},
        }

        async with _httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(url, json=body, headers=headers)

        data = {}
        try:
            data = resp.json()
        except ValueError:
            pass

        if resp.status_code >= 400:
            err_code = (data.get("error", {}) or {}).get("code")
            if resp.status_code == 401 or err_code == 190:
                PA.set_health(acct["id"], "error", note="token_expired")
                raise _PublishError("WhatsApp token expired",
                                    code="AUTHENTICATION", retryable=False)
            if resp.status_code == 429:
                raise _PublishError("Rate limited by Meta",
                                    code="RATE_LIMIT", retryable=True)
            raise _PublishError(f"Meta API error {resp.status_code}",
                                code="TEMPORARY", retryable=True)

        return  # success

    elif platform == "threads":
        token = PA.threads_get_token(owner_id or 0)
        if not token:
            raise _PublishError("Threads not connected",
                                code="AUTHENTICATION", retryable=False)

        headers = {"Authorization": f"Bearer {token}"}

        async with _httpx.AsyncClient(timeout=60) as http:
            container = await http.post(
                f"https://graph.threads.net/v1.0/{account_identifier}/threads",
                json={"media_type": "TEXT", "text": text[:5000]},
                headers=headers,
            )

            cdata = {}
            try:
                cdata = container.json()
            except ValueError:
                pass

            if container.status_code >= 400:
                err = (cdata.get("error", {}) or {})
                if err.get("code") == 190:
                    raise _PublishError("Threads token expired",
                                        code="AUTHENTICATION", retryable=False)
                if container.status_code == 429:
                    raise _PublishError("Rate limited by Threads",
                                        code="RATE_LIMIT", retryable=True)
                raise _PublishError(
                    f"Container error: {err.get('message', container.status_code)}",
                    code="TEMPORARY", retryable=True,
                )

            creation_id = cdata.get("id")
            if not creation_id:
                raise _PublishError("No container id from Threads",
                                    code="TEMPORARY", retryable=True)

            # Meta requires a brief wait between container create & publish
            import asyncio as _aio
            await _aio.sleep(3)

            published = await http.post(
                f"https://graph.threads.net/v1.0/{account_identifier}/threads_publish",
                json={"creation_id": creation_id},
                headers=headers,
            )

            if published.status_code >= 400:
                pdata = {}
                try:
                    pdata = published.json()
                except ValueError:
                    pass
                err = (pdata.get("error", {}) or {})
                if published.status_code == 429:
                    raise _PublishError("Rate limited by Threads",
                                        code="RATE_LIMIT", retryable=True)
                raise _PublishError(
                    f"Publish error: {err.get('message', published.status_code)}",
                    code="TEMPORARY", retryable=True,
                )

        return  # success

    else:
        raise _PublishError(f"No publisher for platform '{platform}'",
                            code="PERMANENT", retryable=False)


register_job_handler("publish", _handle_publish_job)


async def _run_queue_worker() -> None:
    """Claims and executes jobs from the DB queue, forever."""

    from services import job_queue

    logger.info("Queue worker started")

    while True:

        job = None

        try:
            # Poll across all registered types (None = any type)
            job = job_queue.claim_next("main-worker")
        except Exception:
            logger.exception("Queue claim error")
            await asyncio.sleep(_QUEUE_POLL_SECONDS * 5)
            continue

        if job is None:
            await asyncio.sleep(_QUEUE_POLL_SECONDS)
            continue

        try:
            handler = _JOB_TYPE_HANDLERS.get(job.type)

            if handler is None:
                # No handler registered for this type yet -> honest DLQ,
                # not an infinite retry loop.
                job_queue.fail(
                    job.id,
                    error_code="PERMANENT",
                    error_message=f"No worker handler for type '{job.type}'",
                    retryable=False,
                )
                logger.warning("Job #%s (%s): no handler, sent to DLQ", job.id, job.type)
                continue

            # Publish jobs carry their own dedup claim - release it on
            # permanent failure so a manual retry can go through.
            await handler(job.payload)
            job_queue.complete(job.id)

            # Confirm dedup claim for successful external publishes
            if job.type == "publish" and job.payload.get("dedup_claim_id"):
                try:
                    from services import dedup_service as _dd
                    _dd.confirm(job.payload["dedup_claim_id"])
                except Exception:
                    pass

                # Record lifetime success stat (daily quota was already
                # reserved atomically by the forwarder; bump_daily=False
                # avoids double-counting the daily counter).
                try:
                    from services import stats_service as _stats
                    project_id = job.payload.get("project_id")
                    if project_id is not None:
                        _stats.increment(project_id, "forwarded", bump_daily=False)
                except Exception:
                    pass

                # Publish job completed successfully - no per-forward charge to confirm.
                # Daily quota is managed by the plan_service limits.

        except asyncio.CancelledError:
            raise
        except _PublishError as e:
            from services import dedup_service as _dd
            logger.warning("Publish job #%s failed (%s): %s", job.id, e.code, e)
            if not e.retryable:
                claim = job.payload.get("dedup_claim_id")
                if claim:
                    _dd.release(claim)
                    # Release the reserved daily quota on permanent failure
                    try:
                        from services import plan_service as _ps
                        if not job.payload.get("quota_reservation"):
                            _ps.release_daily_forward(job.payload.get("project_id"))
                    except Exception:
                        logger.exception("External forward daily-quota release failed for job %s", job.id)
            job_queue.fail(job.id, error_code=e.code, error_message=str(e),
                           retryable=e.retryable)
        except Exception as e:
            logger.exception("Job #%s (%s) failed", job.id, job.type)
            # Classify: anything not explicitly permanent retries with backoff
            job_queue.fail(
                job.id,
                error_code="TEMPORARY",
                error_message=str(e)[:500],
                retryable=True,
            )


async def _run_queue_worker_safe() -> None:
    """Worker with its own error boundary - a queue problem must never
    affect the forward engine or scheduler."""

    try:
        await _run_queue_worker()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Queue worker crashed")


def start_listener(bot=None) -> None:
    """
    Schedules the forward engine as a background task on the CURRENT
    event loop. Must be called from within a running event loop (see
    ``main.py``'s ``post_init`` hook) so it shares that loop with
    everything else, including the bot handlers.

    ``bot``: passed through to the subscription scheduler so it can DM
    users about expiry/renewal/downgrade events. Optional only so this
    function's existing callers (and any test harness) don't break -
    the scheduler simply doesn't start without one.
    """

    global _forward_task, _processing_task, _owner_engines_task, _scheduler_task, _queue_worker_task

    if _forward_task is not None and not _forward_task.done():
        logger.info("Forward Engine Already Running")
        return

    _forward_task = asyncio.create_task(_run_forever())
    import os
    if os.getenv("ENABLE_EXPERIMENTAL_PLATFORMS", "0") == "1":
        _processing_task = asyncio.create_task(_run_processing_listener_safe())
    _owner_engines_task = asyncio.create_task(_start_owner_engines_safe())
    if os.getenv("ENABLE_EXPERIMENTAL_PLATFORMS", "0") == "1":
        _queue_worker_task = asyncio.create_task(_run_queue_worker_safe())

    # Background task to clean up expired OTP sessions
    from core.user_sessions import start_cleanup_task
    global _login_cleanup_task
    _login_cleanup_task = asyncio.create_task(start_cleanup_task())

    if bot is not None:
        _scheduler_task = asyncio.create_task(_run_subscription_scheduler_safe(bot))

    logger.info("==============================")
    logger.info("Listener Started")
    logger.info("==============================")


_login_cleanup_task = None

async def stop_listener() -> None:
    """Cancels the forward engine task cleanly (used on shutdown)."""

    global _forward_task, _processing_task, _owner_engines_task, _scheduler_task, _queue_worker_task

    global _login_cleanup_task
    if _login_cleanup_task:
        _login_cleanup_task.cancel()
        await asyncio.gather(_login_cleanup_task,return_exceptions=True)
        _login_cleanup_task = None
    from core.user_sessions import close_pending
    from core.forwarder import stop_dispatches
    await stop_dispatches()
    await close_pending()

    if _queue_worker_task is not None:
        _queue_worker_task.cancel()
        try:
            await _queue_worker_task
        except (asyncio.CancelledError, Exception):
            pass
        _queue_worker_task = None

    if _scheduler_task is not None:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except (asyncio.CancelledError, Exception):
            pass
        _scheduler_task = None

    if _processing_task is not None:
        _processing_task.cancel()
        try:
            await _processing_task
        except (asyncio.CancelledError, Exception):
            pass
        _processing_task = None

    if _owner_engines_task is not None:
        _owner_engines_task.cancel()
        try:
            await _owner_engines_task
        except (asyncio.CancelledError, Exception):
            pass
        _owner_engines_task = None

    try:
        await client_pool.stop_all_engines()
    except Exception:
        logger.exception("Error stopping per-owner forwarding clients")

    if _forward_task is None:
        return

    _forward_task.cancel()

    try:
        await _forward_task
    except (asyncio.CancelledError, Exception):
        pass

    _forward_task = None


def is_running() -> bool:
    return _forward_task is not None and not _forward_task.done()
