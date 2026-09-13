"""
ChannelFlow AI - Admin Promotional Post / Broadcast
======================================================

/post       - admin sends one promotional item to ONE chosen target
              (a specific project's destinations, or every eligible
              destination).
/broadcast  - admin sends one promotional item to MANY targets at once,
              scoped by platform (Telegram / Instagram / both / all).

Both share the same flow: content -> target -> confirm -> send -> report.
Nothing sends without an explicit confirmation tap (spec section 21:
"Never immediately blast content without confirmation"). Every attempted
target gets a promotional_deliveries row via services/promo_service.py,
so a partial failure is a query, not a guess.

This is a SEPARATE conversation namespace from the existing admin-panel
"📢 Broadcast" button (callback_data "admin:broadcast", handled in
bot/handlers.py via WAITING_BROADCAST) - that feature DMs every bot
*user* a text announcement and is untouched by this file. This module's
targets are project *destinations* (Telegram channels/Instagram), a
different concept the spec calls out as a new feature, not a
replacement for the existing one.

"Eligible" project, everywhere below, means promo_enabled=1 (mandatory
participation per spec section 22) AND status=1 (active) - a paused
project's channels don't get promotional content pushed into them.
"""

import logging
import os
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import ADMIN_IDS
from database.db import get_connection
from services import destination_service, instagram_service, project_service, promo_service
from destinations.base import DeliveryContent
from destinations.telegram_destination import TelegramDestination
from destinations.instagram_destination import InstagramDestination

logger = logging.getLogger(__name__)

# admin_id -> in-progress draft dict:
#   {"kind": "post"|"broadcast", "stage": "content"|"target"|"confirm",
#    "text": str|None, "media_file_id": str|None, "media_type": str|None,
#    "scope": str|None, "project_id": int|None}
WAITING_PROMO = {}


def _is_admin(user_id) -> bool:
    return user_id in ADMIN_IDS


def _eligible_projects():

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM projects WHERE promo_enabled=1 AND status=1 ORDER BY id"
    )

    rows = cur.fetchall()
    conn.close()

    return rows


# ==========================================
# ENTRY POINTS (/post, /broadcast)
# ==========================================

async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if not _is_admin(user_id):
        await update.message.reply_text("❌ Admins only.")
        return

    WAITING_PROMO[user_id] = {"kind": "post", "stage": "content"}

    await update.message.reply_text(
        "📤 New Post\n\n"
        "Send the text and/or a single photo/video for this post now.\n"
        "Send /cancel to abort."
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    from services import audit_service as RBAC
    if not RBAC.can(user_id, "broadcast.send"):
        await update.message.reply_text("⛔ You don't have broadcast permission.")
        return

    WAITING_PROMO[user_id] = {"kind": "broadcast", "stage": "content"}

    await update.message.reply_text(
        "📣 New Broadcast\n\n"
        "Send the text and/or a single photo/video to broadcast now.\n"
        "Send /cancel to abort."
    )


def cancel_promo(user_id):
    """Called from bot/handlers.py's existing /cancel handler so
    /post and /broadcast drafts are abandoned the same way every other
    in-progress input in this bot already is."""

    WAITING_PROMO.pop(user_id, None)


# ==========================================
# CONTENT CAPTURE
# ==========================================

async def handle_promo_text(message, context, user_id, text):
    """Called from bot/handlers.py's menu_handler when user_id has a
    WAITING_PROMO draft in the 'content' stage."""

    draft = WAITING_PROMO.get(user_id)

    if not draft or draft["stage"] != "content":
        return

    draft["text"] = text
    draft["media_file_id"] = None
    draft["media_type"] = None

    await _show_target_menu(message, user_id)


async def handle_promo_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Registered as its own MessageHandler in main.py (photo/video),
    since the existing menu_handler only ever sees text messages."""

    user_id = update.effective_user.id
    draft = WAITING_PROMO.get(user_id)

    if not draft or draft["stage"] != "content":
        return

    message = update.message

    if message.photo:
        draft["media_file_id"] = message.photo[-1].file_id
        draft["media_type"] = "photo"
    elif message.video:
        draft["media_file_id"] = message.video.file_id
        draft["media_type"] = "video"
    else:
        await message.reply_text("❌ Unsupported media type. Send a photo or video.")
        return

    draft["text"] = message.caption or ""

    await _show_target_menu(message, user_id)


# ==========================================
# TARGET SELECTION
# ==========================================

async def _show_target_menu(message, user_id):

    draft = WAITING_PROMO[user_id]
    draft["stage"] = "target"

    if draft["kind"] == "post":

        buttons = [
            [InlineKeyboardButton("🌐 All Eligible Destinations", callback_data="promo:scope:all")],
        ]

        for project in _eligible_projects():
            buttons.append([
                InlineKeyboardButton(
                    f"📁 {project['name']}", callback_data=f"promo:project:{project['id']}"
                )
            ])

    else:

        buttons = [
            [InlineKeyboardButton("🌐 All Eligible Projects", callback_data="promo:scope:all")],
            [InlineKeyboardButton("📡 Telegram Only", callback_data="promo:scope:telegram")],
            [InlineKeyboardButton("📸 Instagram Only", callback_data="promo:scope:instagram")],
            [InlineKeyboardButton("🔀 Both", callback_data="promo:scope:both")],
        ]

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="promo:abort")])

    await message.reply_text(
        "🎯 Choose a target:", reply_markup=InlineKeyboardMarkup(buttons)
    )


def _resolve_targets(scope=None, project_id=None):
    """Returns a list of (project_id, destination) tuples for the
    chosen scope. `destination` is a TelegramDestination or
    InstagramDestination instance, ready to .send()."""

    targets = []

    projects = (
        [project_service.get_project(project_id)] if project_id else _eligible_projects()
    )

    for project in projects:

        if project is None:
            continue

        platform = project["platform_type"]
        include_telegram = scope in (None, "all", "telegram", "both") and platform in ("telegram", "both")
        include_instagram = scope in (None, "all", "instagram", "both") and platform in ("instagram_broadcast", "both")

        if include_telegram:
            for dest in destination_service.get_destinations(project["id"]):
                if dest["enabled"]:
                    targets.append((
                        project["id"],
                        TelegramDestination(dest["chat_id"], label=dest["title"] or dest["username"] or dest["chat_id"]),
                    ))

        if include_instagram:
            for dest in instagram_service.get_instagram_destinations(project["id"]):
                if dest["enabled"]:
                    targets.append((
                        project["id"],
                        InstagramDestination(dest["ig_user_id"], dest["target_type"], label=dest["username"] or dest["ig_user_id"]),
                    ))

    return targets


# ==========================================
# CALLBACK ROUTING (called from bot/handlers.py's button_handler
# when action == "promo")
# ==========================================

async def handle_promo_callback(query, context, user_id, parts):

    if not _is_admin(user_id):
        await query.message.reply_text("❌ Admins only.")
        return

    draft = WAITING_PROMO.get(user_id)

    if not draft:
        await query.message.reply_text("⚠️ No draft in progress. Start again with /post or /broadcast.")
        return

    sub = parts[1] if len(parts) > 1 else None

    if sub == "abort":
        WAITING_PROMO.pop(user_id, None)
        await query.message.reply_text("❌ Cancelled.")
        return

    if sub == "scope":
        draft["scope"] = parts[2]
        draft["project_id"] = None
        await _show_confirmation(query.message, user_id)
        return

    if sub == "project" and draft["kind"] == "post":
        draft["project_id"] = int(parts[2])
        draft["scope"] = "all"
        await _show_confirmation(query.message, user_id)
        return

    if sub == "confirm":
        await _execute_send(query.message, context, user_id)
        return


async def _show_confirmation(message, user_id):

    draft = WAITING_PROMO[user_id]
    draft["stage"] = "confirm"

    targets = _resolve_targets(scope=draft.get("scope"), project_id=draft.get("project_id"))

    preview = (draft.get("text") or "").strip() or "(no text)"
    media_note = f"\n📎 Media: {draft['media_type']}" if draft.get("media_file_id") else ""

    buttons = [
        [InlineKeyboardButton("✅ Confirm & Send", callback_data="promo:confirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="promo:abort")],
    ]

    await message.reply_text(
        f"👀 Preview\n\n{preview}{media_note}\n\n"
        f"🎯 Targets: {len(targets)}\n\n"
        "Nothing is sent until you confirm.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _execute_send(message, context, user_id):

    draft = WAITING_PROMO.pop(user_id, None)

    if not draft:
        return

    targets = _resolve_targets(scope=draft.get("scope"), project_id=draft.get("project_id"))

    post_id = promo_service.create_promo_post(
        admin_id=user_id,
        kind=draft["kind"],
        text_content=draft.get("text"),
        media_file_id=draft.get("media_file_id"),
        media_type=draft.get("media_type"),
        target_scope=draft.get("scope") or "project",
    )

    local_media_path = None

    try:

        if draft.get("media_file_id"):
            local_media_path = await _download_telegram_media(context, draft["media_file_id"])

        for project_id, destination in targets:

            content = DeliveryContent(
                text=draft.get("text"),
                media_type=draft.get("media_type"),
                media_ref=(
                    local_media_path
                    if isinstance(destination, TelegramDestination)
                    else None  # Instagram feed needs a public URL, which
                               # a locally-downloaded temp file is not -
                               # see destinations/instagram_destination.py
                ),
            )

            result = await destination.send(content)

            promo_service.record_delivery(
                post_id, project_id, destination.platform_id, destination.describe(),
                result.ok, error=result.detail,
            )

    finally:
        if local_media_path and os.path.exists(local_media_path):
            os.remove(local_media_path)

    promo_service.set_post_status(post_id, "COMPLETED")

    report = promo_service.get_delivery_report(post_id)
    await message.reply_text(f"✅ Delivery Report\n\n{_format_report(report)}")


async def _download_telegram_media(context, file_id) -> str:

    tg_file = await context.bot.get_file(file_id)

    fd, path = tempfile.mkstemp(prefix="channelflow_promo_", suffix=os.path.splitext(tg_file.file_path or "")[1])
    os.close(fd)

    await tg_file.download_to_drive(path)

    return path


def _format_report(report: dict) -> str:

    if not report:
        return "No targets were eligible."

    lines = []

    for platform, counts in report.items():
        lines.append(f"{platform.title()}: {counts['sent']} sent, {counts['failed']} failed")

    return "\n".join(lines)
