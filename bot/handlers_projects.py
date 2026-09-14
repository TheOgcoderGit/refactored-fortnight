# ChannelFlow AI - Projects, Sources, Targets & Template Engine
# =============================================================

import logging
from telegram import Update, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from database.db import get_connection
from core import client_pool, user_sessions
from core.forwarder import force_refresh_routes
from core.telegram_utils import get_chat, send_test_message
from bot import handlers_features, error_actions
from core import telegram_utils as tg

# Services
from services.project_service import (
    create_project, get_project, get_projects,
    delete_project, update_status, count_projects
)
from services.source_service import (
    add_source, get_sources, get_source, delete_source,
    count_sources, toggle_source_enabled
)
from services.destination_service import (
    add_destination, get_destinations, get_destination,
    delete_destination, count_destinations, toggle_destination_enabled
)
from services.settings_service import get_settings, update_settings
from services.plan_service import (
    can_create_project, can_add_source, can_add_destination, get_entitlements
)
from services.template_service import TEMPLATES_CATALOG, create_template_project
from services import (
    formatting_service, watermark_service,
    ai_service, affiliate_service, stats_service, log_service
)
from config import ADMIN_IDS

logger = logging.getLogger(__name__)

WAITING_PROJECT_NAME = {}
CURRENT_PROJECT = {}
WAITING_SOURCE = {}
WAITING_DESTINATION = {}
WAITING_TEMPLATE_TARGET = {}
PENDING_TEMPLATE_CHOICE = {}
# Per-user edits to a template's pre-configured sources.
TEMPLATE_SOURCE_DRAFT = {}
WAITING_TEMPLATE_SOURCE = {}

MAX_NAME_LENGTH = 100


def get_user_lang(user_id: int) -> str:
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT language FROM users WHERE telegram_id=?", (user_id,))
    row = cur.fetchone(); conn.close()
    return row["language"] if row and row["language"] else "en"


def project_control_keyboard(project_id: int, is_running: bool) -> InlineKeyboardMarkup:
    toggle_btn = (
        InlineKeyboardButton("⏸️ Pause", callback_data=f"stop:{project_id}")
        if is_running
        else InlineKeyboardButton("▶️ Start", callback_data=f"start:{project_id}")
    )
    return InlineKeyboardMarkup([
        [toggle_btn],
        [
            InlineKeyboardButton("📥 Sources", callback_data=f"src:list:{project_id}"),
            InlineKeyboardButton("🎯 Targets", callback_data=f"tgt:list:{project_id}"),
        ],
        [
            InlineKeyboardButton("🚀 Forwarding", callback_data=f"fwd:settings:{project_id}"),
            InlineKeyboardButton("🔎 Filters", callback_data=f"flt:hub:{project_id}"),
        ],
        [
            InlineKeyboardButton("✍️ Formatting", callback_data=f"fmt:hub:{project_id}"),
            InlineKeyboardButton("🖼️ Watermark", callback_data=f"wm:hub:{project_id}"),
        ],
        [
            InlineKeyboardButton("🔄 Auto & Edits", callback_data=f"auto:hub:{project_id}"),
            InlineKeyboardButton("🤖 AI Tools", callback_data=f"ai:hub:{project_id}"),
        ],
        [
            InlineKeyboardButton("💰 Affiliate", callback_data=f"aff:hub:{project_id}"),
            InlineKeyboardButton("⚡ Safe Pacing", callback_data=f"spd:hub:{project_id}"),
        ],
        [
            InlineKeyboardButton("🧪 Dry-Run Test", callback_data=f"proj:dryrun:{project_id}"),
            InlineKeyboardButton("📋 Clone Project", callback_data=f"proj:clone:{project_id}"),
        ],
        [
            InlineKeyboardButton("📊 Activity & Logs", callback_data=f"act:view:{project_id}"),
            InlineKeyboardButton("🧪 Test Send", callback_data=f"proj:test:{project_id}"),
        ],
        [
            InlineKeyboardButton("🗑️ Delete Project", callback_data=f"deleteconfirm:{project_id}"),
            InlineKeyboardButton("◀️ Back to Projects", callback_data="nav:projects"),
        ],
    ])


# Columns that identify the row rather than describe the configuration.
# Copying them onto another project either raises (they collide with the
# positional project_id argument) or silently re-points the row.
_IDENTITY_COLUMNS = ("id", "project_id", "user_id", "created_at", "updated_at")


def _copiable_row(row) -> dict:
    """A settings row safe to spread into another project's update call."""
    if not row:
        return {}
    return {k: v for k, v in dict(row).items() if k not in _IDENTITY_COLUMNS}


def _looks_like_chat_reference(text: str) -> bool:
    """Cheap format check, so a typo like "Hi" is caught before a round trip."""
    value = (text or "").strip()
    if not value:
        return False
    if value.lstrip("-").isdigit():
        return True
    if "t.me/" in value:
        return True
    if value.startswith("@") and len(value) > 1:
        return True
    return False


def _template_sources(user_id: int, tpl_key: str) -> list:
    """Pre-configured sources for a template, with any user edits applied."""
    draft = TEMPLATE_SOURCE_DRAFT.get(user_id)
    if draft and draft.get("tpl") == tpl_key:
        return draft["sources"]
    return list(TEMPLATES_CATALOG.get(tpl_key, {}).get("sources", []))


def _project_card_text(project: dict, lang: str = "en") -> str:
    s_count = count_sources(project["id"])
    t_count = count_destinations(project["id"])
    status = "🟢 Active" if project["status"] else "⏸️ Paused"
    stg = get_settings(project["id"])
    mode_lbl = "Copy Mode" if stg["mode"] == "copy" else "Native Forward"

    if lang == "hi":
        return (
            f"📁 **Project: {project['name']}**\n"
            f"Status: **{status}**\n\n"
            f"📥 Sources: **{s_count}**\n"
            f"🎯 Targets: **{t_count}**\n"
            f"⚙️ Mode: **{mode_lbl}**\n\n"
            f"Neeche diye gaye buttons se project manage karein:"
        )
    return (
        f"📁 **Project: {project['name']}**\n"
        f"Status: **{status}**\n\n"
        f"📥 Sources: **{s_count}**\n"
        f"🎯 Targets: **{t_count}**\n"
        f"⚙️ Mode: **{mode_lbl}**\n\n"
        f"Manage your automation using the controls below:"
    )


async def render_projects_view(message, user_id: int):
    projects = get_projects(user_id)
    lang = get_user_lang(user_id)

    if not projects:
        t_empty = (
            "📁 **No Projects Yet**\n\n"
            "Create your first automation pipeline to start forwarding content automatically."
            if lang == "en" else
            "📁 **Abhi Koi Project Nahi Hai**\n\n"
            "Channels forward karne ke liye apna pehla automation project banayein:"
        )
        buttons = [
            [InlineKeyboardButton("➕ Create Project", callback_data="proj:new")],
            [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
        ]
        await message.reply_text(t_empty, reply_markup=InlineKeyboardMarkup(buttons))
        return

    t_title = "📁 **Your Automation Projects:**" if lang == "en" else "📁 **Aapke Automation Projects:**"
    buttons = []
    for p in projects:
        icon = "🟢" if p["status"] else "⏸️"
        buttons.append([InlineKeyboardButton(f"{icon} {p['name']}", callback_data=f"projcard:{p['id']}")])

    buttons.append([InlineKeyboardButton("➕ New Project", callback_data="proj:new")])
    buttons.append([InlineKeyboardButton("🏠 Home", callback_data="nav:home")])
    await message.reply_text(t_title, reply_markup=InlineKeyboardMarkup(buttons))


# ==========================================
# CALLBACK ROUTER
# ==========================================

async def handle_callbacks(query: CallbackQuery, user_id: int, action: str, parts: list, context: ContextTypes.DEFAULT_TYPE) -> bool:
    lang = get_user_lang(user_id)

    # 1. Project Creation Gate & Flow
    if action == "proj" and parts[1] == "new":
        if not user_sessions.is_connected(user_id) and user_id not in ADMIN_IDS:
            t_gate = (
                "🔒 **Account Connection Required**\n\n"
                "To create an automation project, please connect your Telegram account first."
                if lang == "en" else
                "🔒 **Account Connect Karna Zaroori Hai**\n\n"
                "Project banane ke liye pehle apna Telegram account connect karein."
            )
            buttons = [
                [InlineKeyboardButton("🔌 Connect Account Now", callback_data="acct:connect")],
                [InlineKeyboardButton("◀️ Back", callback_data="nav:projects")]
            ]
            await query.message.reply_text(t_gate, reply_markup=InlineKeyboardMarkup(buttons))
            return True

        allowed, reason = can_create_project(user_id)
        if not allowed:
            await query.message.reply_text(f"🔒 {reason}")
            return True

        text = (
            "➕ **Create New Automation Project**\n\n"
            "Choose how you want to start:\n\n"
            "1️⃣ **Create Manually** — Enter a project name and configure rules yourself from scratch.\n"
            "2️⃣ **Use Ready Templates** — Pre-loaded with 2 active niche channels and automated rules!"
            if lang == "en" else
            "➕ **Naya Automation Project Banayein**\n\n"
            "Aap project kaise shuru karna chahte hain:\n\n"
            "1️⃣ **Create Manually** — Project ka naam dalein aur sources/targets khud add karein.\n"
            "2️⃣ **Use Ready Templates** — 2 ready-made source channels aur rules ke sath seedha shuru karein!"
        )
        buttons = [
            [InlineKeyboardButton("⚙️ Create Manually (From Scratch)", callback_data="proj:manual")],
            [InlineKeyboardButton("🚀 Use Ready Templates", callback_data="proj:show_templates")],
            [InlineKeyboardButton("◀️ Back to Projects", callback_data="nav:projects")]
        ]
        try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest: pass
        return True

    # Manual creation path
    if action == "proj" and parts[1] == "manual":
        WAITING_PROJECT_NAME[user_id] = True
        PENDING_TEMPLATE_CHOICE[user_id] = "blank"
        t_prompt = "📝 Send a name for your project (e.g. My Deals Flow):" if lang == "en" else "📝 Apne project ka naam bhejein (e.g. My Deals Flow):"
        await query.message.reply_text(t_prompt)
        return True

    # Templates List
    if action == "proj" and parts[1] == "show_templates":
        text = (
            "🚀 **Turnkey Ready-to-Use Templates:**\n\n"
            "Each template comes pre-loaded with 2 active source channels and rules.\n"
            "You only need to enter your Target channel!\n\n"
            "Choose a template:"
            if lang == "en" else
            "🚀 **Ready-to-Use Templates:**\n\n"
            "Har template me 2 active source channels aur rules pehle se set hain.\n"
            "Aapko sirf apna Target Channel enter karna hoga!\n\n"
            "Template chunein:"
        )
        buttons = [
            [InlineKeyboardButton("💰 Loot & Affiliate Deals", callback_data="proj:tpl_view:deals")],
            [InlineKeyboardButton("📰 Tech & Breaking News", callback_data="proj:tpl_view:news")],
            [InlineKeyboardButton("📈 Crypto & Web3 Signals", callback_data="proj:tpl_view:crypto")],
            [InlineKeyboardButton("🎬 Movies & Entertainment", callback_data="proj:tpl_view:movies")],
            [InlineKeyboardButton("◀️ Back", callback_data="proj:new")]
        ]
        try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest: pass
        return True

    # Template Preview & Confirmation Card
    if action == "proj" and parts[1] == "tpl_view":
        tpl_key = parts[2]
        tpl_info = TEMPLATES_CATALOG.get(tpl_key)
        if not tpl_info: return True

        sources = _template_sources(user_id, tpl_key)
        src_lines = "\n".join(f"  {idx+1}. {handle} ({desc})"
                               for idx, (handle, desc) in enumerate(sources)) or "  (none)"
        text = (
            f"**{tpl_info['title']}**\n\n"
            f"📥 **Pre-Configured Sources ({len(sources)}):**\n{src_lines}\n\n"
            f"⚙️ **Automation Pipeline:**\n{tpl_info['desc']}\n\n"
            f"Edit the sources if you want different channels, then tap "
            f"Confirm and send your target channel:"
        )
        buttons = [
            [InlineKeyboardButton("✏️ Edit Sources", callback_data=f"proj:tpl_sources:{tpl_key}")],
            [InlineKeyboardButton("✅ Confirm & Setup Target", callback_data=f"proj:tpl_confirm:{tpl_key}")],
            [
                InlineKeyboardButton("◀️ Back to Templates", callback_data="proj:show_templates"),
                InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
            ],
        ]
        try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest: pass
        return True

    # Edit the template's pre-configured sources before creating anything.
    # Previously the sources were baked in: create_template_project() added
    # them straight away, and if a handle could not be resolved it silently
    # stored the raw text, leaving a source that never worked.
    if action == "proj" and parts[1] == "tpl_sources":
        tpl_key = parts[2]
        tpl_info = TEMPLATES_CATALOG.get(tpl_key)
        if not tpl_info: return True

        sources = _template_sources(user_id, tpl_key)
        rows = []
        for idx, (handle, desc) in enumerate(sources):
            rows.append([
                InlineKeyboardButton(f"📥 {handle}", callback_data="act:noop"),
                InlineKeyboardButton("❌", callback_data=f"proj:tpl_srcdel:{tpl_key}:{idx}"),
            ])
        rows.append([InlineKeyboardButton("➕ Add Another Source",
                                          callback_data=f"proj:tpl_srcadd:{tpl_key}")])
        if len(sources) < len(tpl_info["sources"]):
            rows.append([InlineKeyboardButton("↩️ Restore Defaults",
                                              callback_data=f"proj:tpl_srcreset:{tpl_key}")])
        rows.append([InlineKeyboardButton("✅ Done", callback_data=f"proj:tpl_view:{tpl_key}")])
        rows.append([InlineKeyboardButton("🏠 Home", callback_data="nav:home")])

        text = (
            f"✏️ **Edit Pre-Configured Sources**\n\n"
            f"Template: **{tpl_info['title']}**\n\n"
            + ("\n".join(f"  {idx+1}. {handle} — {desc}"
                          for idx, (handle, desc) in enumerate(sources))
               or "  No sources yet — add at least one.")
            + "\n\nRemove one with ❌, add your own with ➕ Add Another "
              "Source, then tap ✅ Done."
        )
        try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
        except BadRequest: pass
        return True

    if action == "proj" and parts[1] == "tpl_srcdel":
        tpl_key = parts[2]
        try:
            index = int(parts[3])
        except (IndexError, ValueError):
            return True
        sources = _template_sources(user_id, tpl_key)
        if 0 <= index < len(sources):
            sources.pop(index)
            TEMPLATE_SOURCE_DRAFT[user_id] = {"tpl": tpl_key, "sources": sources}
        query.data = f"proj:tpl_sources:{tpl_key}"
        return await handle_callbacks(query, user_id, "proj",
                                      ["proj", "tpl_sources", tpl_key], context)

    if action == "proj" and parts[1] == "tpl_srcreset":
        tpl_key = parts[2]
        TEMPLATE_SOURCE_DRAFT.pop(user_id, None)
        query.data = f"proj:tpl_sources:{tpl_key}"
        return await handle_callbacks(query, user_id, "proj",
                                      ["proj", "tpl_sources", tpl_key], context)

    if action == "proj" and parts[1] == "tpl_srcadd":
        tpl_key = parts[2]
        WAITING_TEMPLATE_SOURCE[user_id] = tpl_key
        await query.message.reply_text(
            "➕ **Add a source channel**\n\n"
            "Send the public username (`@channel`), an invite link, or a "
            "numeric ID. It will be added to this template's sources.\n\n"
            "Send /cancel to abort."
        )
        return True

    # User confirms template: prompt for Target Channel
    if action == "proj" and parts[1] == "tpl_confirm":
        tpl_key = parts[2]
        WAITING_TEMPLATE_TARGET[user_id] = tpl_key
        text = (
            "🎯 **Enter Your Target Channel:**\n\n"
            "Where should posts from this template be delivered?\n\n"
            "Send your destination channel's:\n"
            "• Public Username: `@MyTargetChannel`\n"
            "• Private Invite Link: `https://t.me/+AbCdEf...`\n"
            "• Channel ID: `-1001234567890`\n\n"
            "*(Make sure your connected account is an Admin with 'Post Messages' permission)*"
        )
        await query.message.reply_text(text)
        return True

    # 2. Project Dashboard Card
    if action == "projcard":
        pid = int(parts[1])
        project = get_project(pid)
        if not project or (project["user_id"] != user_id and user_id not in ADMIN_IDS):
            return True
        try: await query.edit_message_text(_project_card_text(project, lang=lang), reply_markup=project_control_keyboard(pid, bool(project["status"])))
        except BadRequest: pass
        return True

    # 3. Start & Pause Automation
    if action == "start":
        pid = int(parts[1])
        project = get_project(pid)
        if not project or (project["user_id"] != user_id and user_id not in ADMIN_IDS):
            return True
        if count_sources(pid) == 0:
            await query.message.reply_text("⚠️ Add at least one source channel before starting.")
            return True
        if count_destinations(pid) == 0:
            await query.message.reply_text("⚠️ Add at least one target channel before starting.")
            return True

        update_status(pid, 1)
        await force_refresh_routes()
        await client_pool.start_owner_engine(user_id)
        p = get_project(pid)
        try: await query.edit_message_text(_project_card_text(p, lang=lang), reply_markup=project_control_keyboard(pid, True))
        except BadRequest: pass
        return True

    if action == "stop":
        pid = int(parts[1])
        project = get_project(pid)
        if not project or (project["user_id"] != user_id and user_id not in ADMIN_IDS):
            return True
        update_status(pid, 0)
        await force_refresh_routes()
        p = get_project(pid)
        try: await query.edit_message_text(_project_card_text(p, lang=lang), reply_markup=project_control_keyboard(pid, False))
        except BadRequest: pass
        return True

    # 4. Project Deletion
    if action == "deleteconfirm":
        pid = int(parts[1])
        buttons = [
            [InlineKeyboardButton("🗑 Yes, Delete Project", callback_data=f"delete:{pid}")],
            [InlineKeyboardButton("Cancel", callback_data=f"projcard:{pid}")]
        ]
        t_confirm = "⚠️ Are you sure you want to permanently delete this project?" if lang == "en" else "⚠️ Kya aap sach me is project ko delete karna chahte hain?"
        try: await query.edit_message_text(t_confirm, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest: pass
        return True

    if action == "delete":
        pid = int(parts[1])
        project = get_project(pid)
        if project and (project["user_id"] == user_id or user_id in ADMIN_IDS):
            delete_project(pid)
            await force_refresh_routes()
            t_del = f"🗑 Project '{project['name']}' deleted." if lang == "en" else f"🗑 Project '{project['name']}' delete kar diya gaya hai."
            await query.edit_message_text(t_del, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 Projects", callback_data="nav:projects")]]))
        return True

    # 5. One-Tap Project Clone
    if action == "proj" and parts[1] == "clone":
        pid = int(parts[2])
        orig = get_project(pid)
        if not orig or (orig["user_id"] != user_id and user_id not in ADMIN_IDS):
            return True

        allowed, reason = can_create_project(user_id)
        if not allowed:
            await query.message.reply_text(f"🔒 {reason}")
            return True

        new_name = f"{orig['name']} (Copy)"[:MAX_NAME_LENGTH]
        new_pid = create_project(user_id, new_name, platform_type=orig["platform_type"])

        # get_settings() returns the whole row, including project_id - and
        # update_settings() takes project_id positionally, so spreading the
        # row straight into it raised
        # "got multiple values for argument 'project_id'". The project was
        # created and then the clone died here, leaving a half-copied
        # project behind. Strip the identity columns before spreading.
        stg = _copiable_row(get_settings(pid))
        update_settings(new_pid, **stg)

        cfg = formatting_service.get_advanced(pid)
        formatting_service.configure(user_id, new_pid, cfg)
        wm = _copiable_row(watermark_service.ensure_watermark_settings(pid))
        watermark_service.update_watermark_settings(new_pid, **wm)
        aff = _copiable_row(affiliate_service.ensure_affiliate_settings(pid))
        affiliate_service.update_affiliate_settings(new_pid, **aff)

        await force_refresh_routes()

        # Say what was copied and where to find it. The clone itself is
        # paused on purpose, so the confirmation has to tell the user their
        # new project is NOT forwarding yet.
        await query.message.reply_text(
            f"✅ **Project cloned**\n\n"
            f"📁 **{new_name}**\n"
            f"⏸ State: **Paused** (start it when you're ready)\n\n"
            f"Copied across: forwarding rules, formatting, watermark, "
            f"affiliate settings and filters.\n\n"
            f"Sources and targets are **not** copied — add them before "
            f"starting so it forwards to the right places.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📂 Open Cloned Project",
                                      callback_data=f"projcard:{new_pid}")],
                [InlineKeyboardButton("🎯 Add Targets", callback_data=f"tgt:list:{new_pid}"),
                 InlineKeyboardButton("📥 Add Sources", callback_data=f"src:list:{new_pid}")],
                [InlineKeyboardButton("📁 My Projects", callback_data="nav:projects"),
                 InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
            ]),
        )
        return True

    # 6. Sources Complete Management
    if action == "src" and parts[1] == "list":
        pid = int(parts[2])
        p = get_project(pid)
        if not p or (p["user_id"] != user_id and user_id not in ADMIN_IDS):
            return True
        sources = get_sources(pid)
        text = f"📥 **Sources for {p['name']}**\n\nChannels & groups ChannelFlow listens to:\n"
        buttons = []
        for s in sources:
            icon = "🟢" if s["enabled"] else "🔴"
            buttons.append([InlineKeyboardButton(f"{icon} {s['title'] or s['chat_id']}", callback_data=f"src:item:{s['id']}:{pid}")])

        buttons.append([InlineKeyboardButton("➕ Add Source", callback_data=f"src:add:{pid}")])
        buttons.append([InlineKeyboardButton("◀️ Back to Project", callback_data=f"projcard:{pid}")])
        try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest: pass
        return True

    if action == "src" and parts[1] == "add":
        pid = int(parts[2])
        allowed, reason = can_add_source(user_id, pid)
        if not allowed:
            await query.message.reply_text(f"🔒 {reason}")
            return True
        CURRENT_PROJECT[user_id] = pid
        WAITING_SOURCE[user_id] = True
        text = (
            "📥 **Add Source Channel or Group**\n\n"
            "Send any of the following:\n"
            "• Public Username: `@channelusername`\n"
            "• Private Invite Link: `https://t.me/+AbCdEf...`\n"
            "• Channel ID: `-1001234567890`\n\n"
            "*(Make sure your connected Telegram account has joined this channel)*"
        )
        await query.message.reply_text(text)
        return True

    if action == "src" and parts[1] == "item":
        sid, pid = int(parts[2]), int(parts[3])
        s = get_source(sid)
        if not s: return True
        state = "🟢 Enabled" if s["enabled"] else "🔴 Disabled"
        text = (
            f"📥 **Source Details**\n\n"
            f"• Title: **{s['title'] or '-'}**\n"
            f"• Username: @{s['username'] or '-'}\n"
            f"• ID: `{s['chat_id']}`\n"
            f"• Type: {s['chat_type'] or 'Channel'}\n"
            f"• Status: {state}\n"
        )
        buttons = [
            [InlineKeyboardButton("⏸ Disable" if s["enabled"] else "▶ Enable", callback_data=f"src:toggle:{sid}:{pid}")],
            [InlineKeyboardButton("🗑 Remove Source", callback_data=f"src:del:{sid}:{pid}")],
            [InlineKeyboardButton("◀️ Back to Sources", callback_data=f"src:list:{pid}")],
        ]
        try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest: pass
        return True

    if action == "src" and parts[1] == "toggle":
        sid, pid = int(parts[2]), int(parts[3])
        p = get_project(pid)
        if not p or (p["user_id"] != user_id and user_id not in ADMIN_IDS):
            return True
        s = get_source(sid)
        if not s or s["project_id"] != pid:
            # Stale button: the source was removed after this message was
            # rendered. Say so instead of raising on None.
            await query.answer("That source no longer exists.", show_alert=True)
            query.data = f"src:list:{pid}"
            return await handle_callbacks(query, user_id, "src", ["src", "list", str(pid)], context)
        toggle_source_enabled(sid)
        await force_refresh_routes()
        s = get_source(sid)
        state = "🟢 Enabled" if s["enabled"] else "🔴 Disabled"
        text = (
            f"📥 **Source Details**\n\n"
            f"• Title: **{s['title'] or '-'}**\n"
            f"• Username: @{s['username'] or '-'}\n"
            f"• ID: `{s['chat_id']}`\n"
            f"• Type: {s['chat_type'] or 'Channel'}\n"
            f"• Status: {state}\n"
        )
        buttons = [
            [InlineKeyboardButton("⏸ Disable" if s["enabled"] else "▶ Enable", callback_data=f"src:toggle:{sid}:{pid}")],
            [InlineKeyboardButton("🗑 Remove Source", callback_data=f"src:del:{sid}:{pid}")],
            [InlineKeyboardButton("◀️ Back to Sources", callback_data=f"src:list:{pid}")],
        ]
        try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest: pass
        return True

    if action == "src" and parts[1] == "del":
        sid, pid = int(parts[2]), int(parts[3])
        delete_source(sid)
        await force_refresh_routes()
        await query.answer("Source removed successfully.")
        query.data = f"src:list:{pid}"
        return await handle_callbacks(query, user_id, "src", ["src", "list", str(pid)], context)

    # 7. Targets Complete Management & Live Testing
    if action == "tgt" and parts[1] == "list":
        pid = int(parts[2])
        p = get_project(pid)
        if not p or (p["user_id"] != user_id and user_id not in ADMIN_IDS):
            return True
        destinations = get_destinations(pid)
        text = f"🎯 **Targets for {p['name']}**\n\nDestinations where posts will be delivered:\n"
        buttons = []
        for d in destinations:
            icon = "🟢" if d["enabled"] else "🔴"
            buttons.append([InlineKeyboardButton(f"{icon} {d['title'] or d['chat_id']}", callback_data=f"tgt:item:{d['id']}:{pid}")])

        buttons.append([InlineKeyboardButton("➕ Add Target", callback_data=f"tgt:add:{pid}")])
        buttons.append([InlineKeyboardButton("🧪 Test All Targets", callback_data=f"proj:test:{pid}")])
        buttons.append([InlineKeyboardButton("◀️ Back to Project", callback_data=f"projcard:{pid}")])
        try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest: pass
        return True

    if action == "tgt" and parts[1] == "add":
        pid = int(parts[2])
        allowed, reason = can_add_destination(user_id, pid)
        if not allowed:
            await query.message.reply_text(f"🔒 {reason}")
            return True
        CURRENT_PROJECT[user_id] = pid
        WAITING_DESTINATION[user_id] = True
        text = (
            "📤 **Add Destination Target Channel**\n\n"
            "Send any of the following:\n"
            "• Public Username: `@targetchannel`\n"
            "• Private Link / Hash: `https://t.me/+AbCdEf...`\n"
            "• Numeric Chat ID: `-1001234567890`\n\n"
            "*(Make sure your connected account is an Admin with 'Post Messages' permission)*"
        )
        await query.message.reply_text(text)
        return True

    if action == "tgt" and parts[1] == "item":
        did, pid = int(parts[2]), int(parts[3])
        d = get_destination(did)
        if not d: return True
        state = "🟢 Enabled" if d["enabled"] else "🔴 Disabled"
        text = (
            f"🎯 **Target Details**\n\n"
            f"• Title: **{d['title'] or '-'}**\n"
            f"• Username: @{d['username'] or '-'}\n"
            f"• ID: `{d['chat_id']}`\n"
            f"• Type: {d['chat_type'] or 'Channel'}\n"
            f"• Status: {state}\n"
        )
        buttons = [
            [InlineKeyboardButton("🧪 Test This Target", callback_data=f"tgt:test:{did}:{pid}")],
            [InlineKeyboardButton("⏸ Disable" if d["enabled"] else "▶ Enable", callback_data=f"tgt:toggle:{did}:{pid}")],
            [InlineKeyboardButton("🗑 Remove Target", callback_data=f"tgt:del:{did}:{pid}")],
            [InlineKeyboardButton("◀️ Back to Targets", callback_data=f"tgt:list:{pid}")],
        ]
        try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest: pass
        return True

    if action == "tgt" and parts[1] == "toggle":
        did, pid = int(parts[2]), int(parts[3])
        p = get_project(pid)
        if not p or (p["user_id"] != user_id and user_id not in ADMIN_IDS):
            return True
        d = get_destination(did)
        if not d or d["project_id"] != pid:
            # Stale button: the target was removed after this message was
            # rendered. Say so instead of raising on None.
            await query.answer("That target no longer exists.", show_alert=True)
            query.data = f"tgt:list:{pid}"
            return await handle_callbacks(query, user_id, "tgt", ["tgt", "list", str(pid)], context)
        toggle_destination_enabled(did)
        await force_refresh_routes()
        d = get_destination(did)
        state = "🟢 Enabled" if d["enabled"] else "🔴 Disabled"
        text = (
            f"🎯 **Target Details**\n\n"
            f"• Title: **{d['title'] or '-'}**\n"
            f"• Username: @{d['username'] or '-'}\n"
            f"• ID: `{d['chat_id']}`\n"
            f"• Type: {d['chat_type'] or 'Channel'}\n"
            f"• Status: {state}\n"
        )
        buttons = [
            [InlineKeyboardButton("🧪 Test This Target", callback_data=f"tgt:test:{did}:{pid}")],
            [InlineKeyboardButton("⏸ Disable" if d["enabled"] else "▶ Enable", callback_data=f"tgt:toggle:{did}:{pid}")],
            [InlineKeyboardButton("🗑 Remove Target", callback_data=f"tgt:del:{did}:{pid}")],
            [InlineKeyboardButton("◀️ Back to Targets", callback_data=f"tgt:list:{pid}")],
        ]
        try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest: pass
        return True

    if action == "tgt" and parts[1] == "del":
        did, pid = int(parts[2]), int(parts[3])
        delete_destination(did)
        await force_refresh_routes()
        await query.answer("Target removed successfully.")
        query.data = f"tgt:list:{pid}"
        return await handle_callbacks(query, user_id, "tgt", ["tgt", "list", str(pid)], context)

    # 8. Test Sends
    if action == "tgt" and parts[1] == "test":
        did, pid = int(parts[2]), int(parts[3])
        d = get_destination(did)
        p = get_project(pid)
        if not d or not p: return True
        await query.message.reply_text("🧪 Sending real live test message to target channel...")
        ok, detail = await send_test_message(d["chat_id"], p["name"], user_id=user_id)
        if ok:
            await query.message.reply_text(f"✅ **Test Passed!**\n\nDelivered to: {d['title'] or d['chat_id']}")
        else:
            await query.message.reply_text(f"❌ **Test Failed!**\n\nTarget: {d['title'] or d['chat_id']}\nReason: {detail}")
        return True

    if action == "proj" and parts[1] == "test":
        pid = int(parts[2])
        p = get_project(pid)
        if not p or (p["user_id"] != user_id and user_id not in ADMIN_IDS):
            return True
        dests = get_destinations(pid)
        if not dests:
            await query.message.reply_text("⚠️ No targets configured to test.")
            return True
        await query.message.reply_text(f"🧪 Testing {len(dests)} target(s) simultaneously...")
        results = []
        for d in dests:
            ok, detail = await send_test_message(d["chat_id"], p["name"], user_id=user_id)
            label = d["title"] or d["chat_id"]
            results.append(f"{'✅' if ok else '❌'} {label}: {'Delivered' if ok else detail}")
        await query.message.reply_text("🧪 **Test Results Summary:**\n\n" + "\n".join(results))
        return True


    # 9. Feature Hubs
    # ------------------------------------------------------------
    # Everything the dashboard links to that is NOT project lifecycle
    # (formatting, filters, AI, watermark, affiliate, forwarding rules,
    # pacing, auto-reactions/post-edit-sync, activity, dry-run) lives in
    # bot/handlers_features.py. Those buttons used to fall through to here
    # and then to the router's "no handler" path, which is why they looked
    # installed but did nothing.
    if await handlers_features.handle_callbacks(query, user_id, action, parts, context):
        return True

    return False


# ==========================================
# TEXT INPUT DISPATCHER
# ==========================================

async def handle_text(message, user_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # 0. Universal cancel - MUST come before any pending-prompt branch.
    # While "send your channel" is open, that branch treats the next
    # message as the channel no matter what it says, so a /cancel typed
    # there used to be stored as a channel called "/cancel".
    if text.strip().lower() in ("/cancel", "cancel"):
        had_pending = error_actions.cancel_pending(user_id)
        await message.reply_text(
            "❌ Cancelled. Nothing was changed." if had_pending
            else "Nothing to cancel - you're all set.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Home", callback_data="nav:home")]]))
        return True

    # 1. Manual Project Creation Naming
    if PENDING_TEMPLATE_CHOICE.get(user_id) == "blank" and WAITING_PROJECT_NAME.get(user_id):
        PENDING_TEMPLATE_CHOICE.pop(user_id, None)
        WAITING_PROJECT_NAME.pop(user_id, None)

        pid = create_project(user_id, text, platform_type="telegram")
        await force_refresh_routes()

        await message.reply_text(
            f"✅ **Project '{text}' Created Successfully!**\n\nNext: Add your source and target channels below:",
            reply_markup=project_control_keyboard(pid, False)
        )
        return True

    # 1b. Adding a source to a template before it is created
    if WAITING_TEMPLATE_SOURCE.get(user_id):
        tpl_key = WAITING_TEMPLATE_SOURCE.pop(user_id)
        handle = text.strip()

        if not _looks_like_chat_reference(handle):
            WAITING_TEMPLATE_SOURCE[user_id] = tpl_key
            await message.reply_text(
                "✏️ **That doesn't look like a channel.**\n\n"
                "Send a public username (`@channel`), an invite link "
                "(`https://t.me/+AbCd...`), or a numeric ID "
                "(`-1001234567890`). Send /cancel to abort.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="act:cancel")],
                ]),
            )
            return True

        resolved = None
        try:
            resolved = await get_chat(handle, user_id=user_id)
        except Exception as e:
            if error_actions.classify(e) == "not_connected":
                # No connected account yet - accept the handle and let the
                # project creation verify it, rather than blocking here.
                resolved = None
            else:
                error_actions.remember_retry(user_id, "template_source", tpl=tpl_key)
                await error_actions.reply_resolution_failure(message, e, "source")
                return True

        sources = _template_sources(user_id, tpl_key)
        if resolved:
            sources.append((handle, resolved.get("title") or handle))
        else:
            sources.append((handle, "unverified — will be checked at setup"))
        TEMPLATE_SOURCE_DRAFT[user_id] = {"tpl": tpl_key, "sources": sources}

        note = ("✅ Verified and added." if resolved else
                "⚠️ Added, but it could not be verified yet — connect your "
                "account and it will be checked when the project is created.")
        await message.reply_text(
            f"{note}\n\nThis template now has {len(sources)} source(s).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Back to Sources",
                                      callback_data=f"proj:tpl_sources:{tpl_key}")],
                [InlineKeyboardButton("✅ Confirm & Setup Target",
                                      callback_data=f"proj:tpl_confirm:{tpl_key}")],
            ]),
        )
        return True

    # 2. Turnkey Template Project Creation (Target provided by user)
    if WAITING_TEMPLATE_TARGET.get(user_id):
        tpl_key = WAITING_TEMPLATE_TARGET.pop(user_id)
        tpl_info = TEMPLATES_CATALOG.get(tpl_key)
        if not tpl_info:
            await message.reply_text("❌ Template not found. Start over with /start.")
            return True

        status_msg = await message.reply_text("⏳ Verifying your target channel and setting up pre-loaded sources...")

        # Resolve User's Target Channel
        try:
            target_chat = await get_chat(text, for_destination=True, user_id=user_id)
        except Exception as e:
            # One message for five different problems used to be all the
            # guidance the user got. Report the actual cause and offer the
            # action that fixes it.
            logger.info("Template target resolve failed for user %s: %s", user_id, e)
            error_actions.remember_retry(
                user_id, "template_target", tpl=tpl_key)
            await error_actions.edit_resolution_failure(status_msg, e, "target channel")
            return True

        if not target_chat:
            error_actions.remember_retry(user_id, "template_target", tpl=tpl_key)
            await status_msg.edit_text(
                tg.ERROR_COPY["not_found"],
                reply_markup=error_actions.failure_keyboard(tg.ChatNotFoundError()),
                parse_mode="HTML")
            return True

        try:
            # Create Turnkey Project with 2 Pre-loaded Sources + Target + Rules
            pid = await create_template_project(
                user_id, tpl_key, target_chat,
                sources=_template_sources(user_id, tpl_key))
            TEMPLATE_SOURCE_DRAFT.pop(user_id, None)
            await force_refresh_routes()

            src_names = ", ".join(h for h, _ in tpl_info["sources"])
            await status_msg.edit_text(
                f"🎉 **{tpl_info['title']} Project Live & Ready!**\n\n"
                f"📁 Project: **{tpl_info['default_name']}**\n"
                f"📥 Pre-loaded Sources: {src_names}\n"
                f"🎯 Target Destination: **{target_chat['title'] or target_chat['chat_id']}**\n"
                f"⚙️ Rules: Pre-configured for {tpl_key.capitalize()} niche!\n\n"
                f"Tap **▶️ Start** below to activate live auto-forwarding:",
                reply_markup=project_control_keyboard(pid, False)
            )
        except Exception as ex:
            logger.exception("Error building template project: %s", ex)
            await status_msg.edit_text(f"❌ Error setting up template project: {ex}")

        return True

    # 3. Add Source Input
    if WAITING_SOURCE.get(user_id):
        pid = CURRENT_PROJECT.get(user_id)
        WAITING_SOURCE.pop(user_id, None)
        try:
            chat = await get_chat(text, user_id=user_id)
            if chat:
                add_source(pid, chat["chat_id"], chat["username"], chat["title"], chat["type"])
                await force_refresh_routes()
                await message.reply_text(
                    f"✅ **Added Source:** {chat['title'] or chat['chat_id']}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📥 View Sources", callback_data=f"src:list:{pid}")]]),
                )
        except Exception as e:
            logger.info("Add source failed for user %s: %s", user_id, e)
            error_actions.remember_retry(user_id, "source", pid=pid)
            await error_actions.reply_resolution_failure(message, e, "source")
        return True

    # 4. Add Destination Input
    if WAITING_DESTINATION.get(user_id):
        pid = CURRENT_PROJECT.get(user_id)
        WAITING_DESTINATION.pop(user_id, None)
        try:
            chat = await get_chat(text, for_destination=True, user_id=user_id)
            if chat:
                add_destination(pid, chat["chat_id"], chat["username"], chat["title"], chat["type"])
                await force_refresh_routes()
                await message.reply_text(
                    f"✅ **Added Target:** {chat['title'] or chat['chat_id']}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎯 View Targets", callback_data=f"tgt:list:{pid}")]]),
                )
        except Exception as e:
            logger.info("Add target failed for user %s: %s", user_id, e)
            error_actions.remember_retry(user_id, "destination", pid=pid)
            await error_actions.reply_resolution_failure(message, e, "target")
        return True

    # 5. Feature hub text input (prefix/suffix, filters, watermark text, ...)
    if await handlers_features.handle_text(message, user_id, text, context):
        return True

    return False