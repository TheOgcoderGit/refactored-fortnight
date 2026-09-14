"""
ChannelFlow AI - Telegram Utilities
===================================
Handles chat/channel resolution (Public usernames, Numeric IDs, and Private Invite Links),
membership verification, destination posting capability checks, and live test messages.
"""

import logging
from contextlib import asynccontextmanager
from database.db import get_connection

from telethon import utils
from telethon.errors import (
    UsernameNotOccupiedError,
    UsernameInvalidError,
    UserAlreadyParticipantError,
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    RPCError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest

logger = logging.getLogger(__name__)


# ==========================================================================
# CHAT RESOLUTION ERRORS
# ==========================================================================
#
# Every failure used to collapse into one string, "Could not find or join
# chat (...). Ensure your connected Telegram account has access." That is
# true for five very different problems - no account connected, an expired
# session, a typo in the handle, not being a member, and being a member
# without post rights - and each one has a different fix. Callers now get a
# typed error and can offer the button that actually helps.


def _looks_like_unregistered(exc) -> bool:
    """Telegram rejected the session's auth key.

    Surfaces as "The key is not registered in the system" - a session
    problem, not a bad channel handle.
    """
    text = str(exc).lower()
    return "not registered in the system" in text or "auth key" in text


def _looks_like_not_found(exc) -> bool:
    text = str(exc).lower()
    return "cannot find any entity" in text or "no such" in text


class ChatResolveError(Exception):
    """Base class for chat resolution failures.

    ``kind`` drives the user-facing copy and the recovery buttons.
    """

    kind = "unknown"

    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__(detail or self.__class__.__name__)


class AccountNotConnectedError(ChatResolveError):
    """No Telegram session row for this user."""

    kind = "not_connected"


class SessionExpiredError(ChatResolveError):
    """A session exists but Telegram rejected its auth key."""

    kind = "session_expired"


class ChatNotFoundError(ChatResolveError):
    """Nothing exists at that handle / ID / invite link."""

    kind = "not_found"


class ChatNoAccessError(ChatResolveError):
    """The chat exists but the account cannot see or join it."""

    kind = "no_access"


class ChatNoPostRightsError(ChatResolveError):
    """The account is a member but may not post to the destination."""

    kind = "no_post_rights"


class ChatRateLimitedError(ChatResolveError):
    """Telegram asked us to back off."""

    kind = "rate_limited"

    def __init__(self, detail: str = "", seconds: int = 0):
        super().__init__(detail)
        self.seconds = seconds


class InvalidChatInputError(ChatResolveError):
    """The text the user sent is not a handle, ID or invite link."""

    kind = "invalid_input"


# Copy for each kind: what happened, and what to do next.
ERROR_COPY = {
    "not_connected": (
        "🔌 **Your Telegram account isn't connected.**\n\n"
        "ChannelFlow forwards using *your* account, so it can only see "
        "channels that account has joined. Connect it first, then send the "
        "channel again."
    ),
    "session_expired": (
        "⏳ **Your Telegram session expired.**\n\n"
        "Telegram rejected the stored login. Reconnect your account - it "
        "takes about 30 seconds - then send the channel again."
    ),
    "not_found": (
        "🔍 **Nothing found at that handle.**\n\n"
        "Telegram has no channel, group or user with that username/ID."
    ),
    "no_access": (
        "🔒 **Your account can't open this chat.**\n\n"
        "It's private or invite-only, and your connected account isn't a "
        "member. Join it from your Telegram app, then try again."
    ),
    "no_post_rights": (
        "✍️ **You can read this channel but not post to it.**\n\n"
        "Make your connected account an *admin* with the **Post Messages** "
        "permission, then try again."
    ),
    "rate_limited": (
        "⏱ **Telegram is rate-limiting this account.**\n\n"
        "Please wait a little and try again."
    ),
    "invalid_input": (
        "✏️ **That doesn't look like a channel.**\n\n"
        "Send one of:\n"
        "• Public username — `@mychannel`\n"
        "• Numeric ID — `-1001234567890`\n"
        "• Private invite link — `https://t.me/+AbCdEf...`"
    ),
    "unknown": (
        "⚠️ **Could not resolve this chat.**\n\n"
        "Check the handle and make sure your connected account is a member."
    ),
}


def _resolve_type_label(entity) -> str:
    """Maps a raw Telethon entity to a user-friendly label."""
    cls_name = entity.__class__.__name__

    if cls_name == "Channel":
        if getattr(entity, "megagroup", False):
            return "Supergroup"
        return "Channel"

    if cls_name == "Chat":
        return "Group"

    if cls_name == "User":
        if getattr(entity, "bot", False):
            return "Telegram Bot"
        return "User"

    return cls_name


async def _join_chat(entity, client):
    """Ensures account is a member of the channel/group so updates are received."""
    cls_name = entity.__class__.__name__

    if cls_name != "Channel":
        return True, None

    try:
        await client(JoinChannelRequest(entity))
        return True, None
    except UserAlreadyParticipantError:
        return True, None
    except ChannelPrivateError:
        return False, "This is private/invite-only. Add your connected Telegram account manually."
    except FloodWaitError as e:
        return False, f"Telegram rate limit: please wait {e.seconds}s before joining."
    except RPCError as e:
        return False, f"Could not auto-join ({e}). Please ensure account is a member manually."


async def _check_can_post(entity, client):
    """Checks if the account can send messages to this destination."""
    cls_name = entity.__class__.__name__

    if cls_name not in ("Channel", "Chat"):
        return True, None

    try:
        perms = await client.get_permissions(entity, "me")

        if getattr(entity, "broadcast", False):
            can_post = bool(getattr(perms, "is_admin", False)) and bool(
                getattr(perms, "post_messages", False)
            )
        else:
            can_post = bool(
                getattr(perms, "is_admin", False)
                or getattr(perms, "send_messages", True)
            )

        if can_post:
            return True, None
        return False, "This account may need admin rights with 'Post Messages' permission. You can tap 🧪 Test Target to confirm."

    except Exception as e:
        logger.info("Permission check inconclusive for %s: %s (allowing user to test)", entity, e)
        # Inconclusive checks should NOT block adding destinations — let 🧪 Test button verify
        return True, None


async def _send_test_message(client, chat_id, project_name="ChannelFlow AI"):
    """Sends a real, live test message to verify target delivery."""
    try:
        target = int(chat_id)
    except (TypeError, ValueError):
        target = chat_id

    try:
        await client.send_message(
            target,
            f"✅ {project_name} — Test Message\n\n"
            "If you can see this message, forwarding to this destination is working perfectly!"
        )
        return True, None

    except ChatWriteForbiddenError:
        return False, "Account cannot post here. Make sure it is an Admin with 'Post Messages' permission."
    except ChannelPrivateError:
        return False, "Chat is private/invite-only and the account is not a member. Add it manually first."
    except FloodWaitError as e:
        return False, f"Telegram asked to wait {e.seconds}s before sending again."
    except RPCError as e:
        return False, str(e)
    except Exception as e:
        logger.exception("Unexpected error sending test message to %s", chat_id)
        return False, str(e)


async def _get_chat(client, username, for_destination=False):
    """
    Resolves public usernames (@channel), raw IDs (-100...), and private invite links.
    """
    username_raw = str(username).strip()
    is_numeric = username_raw.lstrip("-").isdigit()
    try:
        username = username_raw

        # 1. Private Invite Links (e.g. t.me/+hash or t.me/joinchat/hash)
        if "t.me/+" in username or "t.me/joinchat/" in username:
            try:
                hash_val = username.split("+")[-1].split("/")[-1].strip()
                try:
                    res = await client(CheckChatInviteRequest(hash_val))
                    chat_entity = getattr(res, "chat", None)
                    if not chat_entity:
                        await client(ImportChatInviteRequest(hash_val))
                        chat_entity = await client.get_entity(username)
                except UserAlreadyParticipantError:
                    chat_entity = await client.get_entity(username)
                except Exception:
                    chat_entity = await client.get_entity(username)
                entity = chat_entity
            except Exception:
                entity = await client.get_entity(username)

        # 2. Raw Numeric Chat IDs (-100... or positive IDs)
        elif username.lstrip("-").isdigit():
            identifier = int(username)
            entity = await client.get_entity(identifier)

        # 3. Public Usernames (@channel or channelname)
        else:
            if not username.startswith("@"):
                username = "@" + username
            entity = await client.get_entity(username)

        chat_id = utils.get_peer_id(entity)
        joined, join_note = await _join_chat(entity, client)

        note = join_note
        if for_destination:
            can_post, post_note = await _check_can_post(entity, client)
            if not can_post and post_note:
                note = post_note

        return {
            "chat_id": str(chat_id),
            "username": getattr(entity, "username", "") or "",
            "title": getattr(entity, "title", "") or getattr(entity, "first_name", "") or "",
            "type": _resolve_type_label(entity),
            "joined": joined,
            "join_note": note,
        }

    except (UsernameNotOccupiedError, UsernameInvalidError):
        raise ChatNotFoundError(f"No Telegram chat exists for '{username_raw}'.")
    except ChatResolveError:
        raise
    except FloodWaitError as e:
        raise ChatRateLimitedError(str(e), seconds=getattr(e, "seconds", 0))
    except (ChannelPrivateError, ChatWriteForbiddenError) as e:
        raise ChatNoAccessError(str(e))
    except ValueError as e:
        # Telethon raises a bare ValueError("Cannot find any entity ...")
        # for handles that simply do not exist.
        raise ChatNotFoundError(str(e))
    except Exception as e:
        logger.warning("get_chat failed for %r: %s", username_raw, e)

        if is_numeric:
            # A raw ID can be perfectly valid while still being unresolvable
            # right now (the account has not joined yet). Accept it, but say
            # plainly that it was NOT verified rather than pretending it was.
            return {
                "chat_id": username_raw,
                "username": "",
                "title": f"Chat {username_raw}",
                "type": "Channel",
                "joined": False,
                "join_note": ("Not verified yet - your account could not open "
                              "this ID, so it must join before forwarding works."),
                "unverified": True,
            }

        if _looks_like_unregistered(e):
            raise SessionExpiredError(str(e))
        if _looks_like_not_found(e):
            raise ChatNotFoundError(str(e))
        raise ChatResolveError(str(e))


async def _get_account_info(client):
    """Returns metadata about the active account."""
    me = await client.get_me()
    return {
        "id": me.id,
        "username": me.username,
        "phone": me.phone,
        "first_name": me.first_name,
        "premium": bool(getattr(me, "premium", False)),
        "dc_id": getattr(client.session, "dc_id", 0),
    }


@asynccontextmanager
async def _owner_client(user_id, allow_shared=False):
    """
    Context manager providing the correct Telethon client:
    - Uses pooled engine if running in client_pool
    - Spawns decrypted temporary client if session exists
    - Optionally falls back to the shared client (admin tooling only)

    ``allow_shared`` exists because the shared client is the *bot's own*
    account. It is almost never the right identity for resolving a user's
    channel: the bot has not joined the user's channels, so resolution fails
    with a confusing "Cannot find any entity" even though the user is the
    channel owner. It used to be an automatic fallback for administrators,
    which is exactly how that happened. Callers that genuinely want the bot
    account must now opt in.
    """
    from core import client_pool
    from core.session_crypto import decrypt_session
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from config import API_ID, API_HASH, ADMIN_IDS

    if user_id is None:
        raise AccountNotConnectedError("An account owner is required.")

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM user_telegram_sessions WHERE telegram_id=? AND status='connected'",
            (user_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        if allow_shared and user_id in ADMIN_IDS:
            from core.client import ensure_started
            logger.info(
                "No connected session for admin %s; using the shared bot "
                "client on an explicit opt-in.", user_id)
            yield await ensure_started()
            return
        raise AccountNotConnectedError(
            "No connected Telegram account for this user.")

    # Check if a client engine is already running in pool
    engine = client_pool._engines.get(user_id)
    if engine:
        yield engine["client"]
        return

    # Create client from decrypted session
    try:
        session_str = decrypt_session(row["encrypted_session"])
    except Exception as exc:
        # A session saved under a different SESSION_ENCRYPTION_KEY cannot be
        # decrypted. Reconnecting is the only fix, so say that instead of
        # surfacing a crypto error.
        logger.warning("Session decrypt failed for user %s: %s", user_id, exc)
        raise SessionExpiredError("Stored session could not be decrypted.")

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise SessionExpiredError("Telegram reported the session as unauthorized.")

        # is_user_authorized() only inspects local session state. A session
        # can look authorized locally while Telegram has since dropped its
        # auth key ("The key is not registered in the system"). One cheap
        # get_me() here turns that into a clear "reconnect" prompt instead of
        # a confusing failure later during chat resolution.
        try:
            await client.get_me()
        except Exception as exc:
            logger.warning("Session probe failed for user %s: %s", user_id, exc)
            raise SessionExpiredError(str(exc))
        yield client
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_chat(username, for_destination=False, user_id=None):
    """Public wrapper to resolve chat using the owning user's Telethon client."""
    async with _owner_client(user_id) as client:
        return await _get_chat(client, username, for_destination)


async def send_test_message(chat_id, project_name="ChannelFlow AI", user_id=None):
    """Public wrapper to send live test message using the owning user's client."""
    try:
        async with _owner_client(user_id) as client:
            return await _send_test_message(client, chat_id, project_name)
    except Exception as e:
        logger.warning("send_test_message failed for chat %s: %s", chat_id, e)
        return False, f"Could not send test message ({e}). Ensure account is admin/member."


async def get_account_info(user_id=None):
    """Public wrapper to inspect active account details."""
    async with _owner_client(user_id) as client:
        return await _get_account_info(client)