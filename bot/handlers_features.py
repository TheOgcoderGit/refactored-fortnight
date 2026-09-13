"""ChannelFlow AI - Project Feature Hubs
=======================================

Every per-project transformation screen that the project dashboard links to
lives here:

    🚀 Forwarding      (mode / silent / protect / albums / delay)
    🔎 Filters         (keywords, hashtags, domains, senders, regex, media, length)
    ✍️ Formatting      (prefix, suffix, replace, remove, advanced cleanup)
    🖼️ Watermark       (enable, type, text, position, size, opacity)
    🤖 AI Tools        (enable, tone, length, translate, CTA, custom prompt)
    💰 Affiliate       (per-provider toggles + affiliate IDs)
    🔄 Auto & Edits    (post edit sync, auto reactions)
    ⚡ Safe Pacing     (delay window)
    📊 Activity        (stats + logs)

Why this is a separate module
-----------------------------
``handlers_projects.py`` owns project *lifecycle* (create/list/start/delete,
sources and targets).  All of those worked, but the ten feature buttons on its
dashboard pointed at callbacks that nothing handled - they were silently
dropped by the router, which is exactly the "source/target chhodkar kuch kaam
nahi karta" symptom.  Keeping the feature screens here keeps that file small
and makes the missing wiring obvious: this module is reached from exactly two
delegation points (``handlers_projects.handle_callbacks``/``handle_text``).

Every screen here follows the PRD chain:
    UI -> callback -> handler -> service -> database -> runtime refresh
and every mutating handler calls ``force_refresh_routes()`` so the running
forwarder picks the change up without a restart.
"""

import html
import json
import logging
import re

from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest

from core.forwarder import force_refresh_routes
from config import ADMIN_IDS
from database.db import get_connection
from services import (
    affiliate_service,
    ai_service,
    content_rules_service,
    formatting_service,
    log_service,
    plan_service,
    settings_service,
    stats_service,
    watermark_service,
)
from services.project_service import get_project, rename_project
from services.source_service import get_source, delete_source, toggle_source_enabled
from services.destination_service import (
    get_destination,
    get_destinations,
    delete_destination,
    toggle_destination_enabled,
)

logger = logging.getLogger(__name__)


# ==========================================
# PENDING TEXT INPUT
# ==========================================
# user_id -> {"kind": str, "project_id": int, ...}
# Single-process bot; this is the same pattern the rest of the codebase uses
# for multi-step input (see handlers_projects.WAITING_SOURCE).
PENDING_INPUT = {}

MAX_TEXT_INPUT = 1000


def clear_pending(user_id: int):
    PENDING_INPUT.pop(user_id, None)


# ==========================================
# HELPERS
# ==========================================

def _owned(project_id: int, user_id: int):
    """Return the project row if it exists and belongs to the caller."""
    project = get_project(project_id)
    if not project:
        return None
    if project["user_id"] != user_id and user_id not in ADMIN_IDS:
        return None
    return project


def _e(value) -> str:
    """HTML-escape a dynamic value before it goes into a parse_mode=HTML body."""
    return html.escape(str(value if value is not None else ""), quote=False)


async def _edit(query, text, markup, html_mode: bool = True):
    """Edit the callback message, degrading to plain text if Telegram
    rejects the markup. Never lets a formatting nit kill a screen."""
    try:
        await query.edit_message_text(
            text,
            reply_markup=markup,
            parse_mode="HTML" if html_mode else None,
            disable_web_page_preview=True,
        )
    except BadRequest:
        try:
            await query.edit_message_text(
                text, reply_markup=markup, disable_web_page_preview=True
            )
        except BadRequest:
            pass
    except Exception:
        logger.exception("Failed to edit message")


async def _send(query, text, markup=None, html_mode: bool = True):
    try:
        await query.message.reply_text(
            text,
            reply_markup=markup,
            parse_mode="HTML" if html_mode else None,
            disable_web_page_preview=True,
        )
    except BadRequest:
        await query.message.reply_text(text, reply_markup=markup)


def _onoff(value) -> str:
    return "🟢 On" if value else "🔴 Off"


def _back_row(project_id: int):
    return [InlineKeyboardButton("◀️ Back to Project", callback_data=f"projcard:{project_id}")]


def _home_row(project_id: int):
    return [
        InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
        InlineKeyboardButton("◀️ Back to Project", callback_data=f"projcard:{project_id}"),
    ]


def _ask(user_id: int, kind: str, project_id: int, **extra):
    PENDING_INPUT[user_id] = {"kind": kind, "project_id": project_id, **extra}


# ==========================================
# 1. FORWARDING RULES
# ==========================================

def forwarding_keyboard(project_id: int, settings: dict):
    mode = settings.get("mode") or "forward"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"📨 Mode: {'Copy (editable)' if mode == 'copy' else 'Forward (native)'}",
            callback_data=f"togglemode:{project_id}")],
        [
            InlineKeyboardButton(f"🔕 Silent: {_onoff(settings.get('silent'))}",
                                 callback_data=f"togglesilent:{project_id}"),
            InlineKeyboardButton(f"🛡 Protect: {_onoff(settings.get('protect_content'))}",
                                 callback_data=f"toggleprotect:{project_id}"),
        ],
        [InlineKeyboardButton(f"🖼 Albums: {_onoff(settings.get('keep_media_groups'))}",
                              callback_data=f"togglealbum:{project_id}")],
        [InlineKeyboardButton(
            f"⏱ Delay: {settings.get('delay_min', 0)}–{settings.get('delay_max', 0)}s",
            callback_data=f"setdelay:{project_id}")],
        _home_row(project_id),
    ])


async def _render_forwarding(query, user_id, project_id):
    settings = settings_service.get_settings(project_id)
    is_copy = (settings.get("mode") or "forward") == "copy"
    mode_desc = ("Copy — text/media is re-sent so formatting, AI and "
                 "prefix/suffix apply") if is_copy else (
                "Forward — native Telegram forward, content is never edited")
    text = (
        "🚀 <b>Forwarding Rules</b>\n\n"
        f"• <b>Mode</b>: {mode_desc}\n"
        f"• <b>Silent</b>: {_onoff(settings.get('silent'))}\n"
        f"• <b>Protect content</b>: {_onoff(settings.get('protect_content'))}\n"
        f"• <b>Keep albums</b>: {_onoff(settings.get('keep_media_groups'))}\n"
        f"• <b>Delay</b>: {settings.get('delay_min', 0)}–{settings.get('delay_max', 0)} seconds\n\n"
        "⚠️ Formatting, AI, watermark, filters and affiliate replacement only run in <b>Copy</b> mode."
    )
    await _edit(query, text, forwarding_keyboard(project_id, settings))


# ==========================================
# 2. FILTERS
# ==========================================

MEDIA_FILTER_CHOICES = (
    "all", "text", "photo", "video", "audio",
    "document", "voice", "sticker", "poll", "animation", "video_note",
)


def filters_hub_keyboard(project_id: int, settings: dict, rules):
    def _count(value):
        return len([v for v in (value or "").split(",") if v.strip()])

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"🔑 Keywords (✅{_count(settings.get('keyword_whitelist'))} / 🚫{_count(settings.get('keyword_blacklist'))})",
                callback_data=f"filterkw:{project_id}"),
            InlineKeyboardButton(f"#️⃣ Hashtags ({_count(rules['hashtag_filter'])})",
                                 callback_data=f"crfield:{project_id}:hashtag_filter"),
        ],
        [
            InlineKeyboardButton(
                f"🌐 Domains (✅{_count(rules['domain_whitelist'])} / 🚫{_count(rules['domain_blacklist'])})",
                callback_data=f"filterdomains:{project_id}"),
            InlineKeyboardButton(
                f"👤 Senders (✅{_count(rules['sender_whitelist'])} / 🚫{_count(rules['sender_blacklist'])})",
                callback_data=f"filtersenders:{project_id}"),
        ],
        [
            InlineKeyboardButton(
                f"📏 Length ({rules['min_length'] if rules['min_length'] is not None else 0}"
                f"–{rules['max_length'] if rules['max_length'] is not None else '∞'})",
                callback_data=f"crfield:{project_id}:length"),
            InlineKeyboardButton(
                f"🔤 Regex {'✅' if settings.get('regex_filter') else '—'}",
                callback_data=f"setregex:{project_id}"),
        ],
        [InlineKeyboardButton(f"🎛 Media type: {settings.get('media_filter') or 'all'}",
                              callback_data=f"mediafilter:{project_id}")],
        [InlineKeyboardButton("🧹 Clear all filters", callback_data=f"clearfiltersconfirm:{project_id}")],
        _home_row(project_id),
    ])


async def _render_filters(query, user_id, project_id):
    settings = settings_service.get_settings(project_id)
    rules = content_rules_service.get_rules(project_id)
    text = (
        "🔎 <b>Content Filters</b>\n\n"
        "A message is forwarded only when every configured rule passes.\n"
        "Empty rules are ignored, so set only what you need.\n\n"
        f"• Keywords — required: <code>{_e(settings.get('keyword_whitelist') or 'none')}</code>\n"
        f"• Keywords — blocked: <code>{_e(settings.get('keyword_blacklist') or 'none')}</code>\n"
        f"• Hashtags: <code>{_e(rules['hashtag_filter'] or 'none')}</code>\n"
        f"• Domains ✅: <code>{_e(rules['domain_whitelist'] or 'none')}</code>\n"
        f"• Domains 🚫: <code>{_e(rules['domain_blacklist'] or 'none')}</code>\n"
        f"• Senders ✅: <code>{_e(rules['sender_whitelist'] or 'none')}</code>\n"
        f"• Senders 🚫: <code>{_e(rules['sender_blacklist'] or 'none')}</code>\n"
        f"• Length: {rules['min_length'] if rules['min_length'] is not None else 0}"
        f"–{rules['max_length'] if rules['max_length'] is not None else 'any'}\n"
        f"• Regex: <code>{_e(settings.get('regex_filter') or 'none')}</code>\n"
        f"• Media: <code>{_e(settings.get('media_filter') or 'all')}</code>\n"
        f"• Match mode: <code>{_e(rules['filter_logic'])}</code>"
    )
    await _edit(query, text, filters_hub_keyboard(project_id, settings, rules))


def keyword_keyboard(project_id, settings):
    def _count(value):
        return len([v for v in (value or "").split(",") if v.strip()])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Required keywords ({_count(settings.get('keyword_whitelist'))})",
                              callback_data=f"setwhitelist:{project_id}")],
        [InlineKeyboardButton(f"🚫 Blocked keywords ({_count(settings.get('keyword_blacklist'))})",
                              callback_data=f"setblacklist:{project_id}")],
        [InlineKeyboardButton("◀️ Back to Filters", callback_data=f"flt:hub:{project_id}")],
    ])


def domain_keyboard(project_id, rules):
    def _count(value):
        return len([v for v in (value or "").split(",") if v.strip()])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌐 Allowed domains ({_count(rules['domain_whitelist'])})",
                              callback_data=f"crfield:{project_id}:domain_whitelist")],
        [InlineKeyboardButton(f"⛔ Blocked domains ({_count(rules['domain_blacklist'])})",
                              callback_data=f"crfield:{project_id}:domain_blacklist")],
        [InlineKeyboardButton("◀️ Back to Filters", callback_data=f"flt:hub:{project_id}")],
    ])


def sender_keyboard(project_id, rules):
    def _count(value):
        return len([v for v in (value or "").split(",") if v.strip()])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"👤 Allowed senders ({_count(rules['sender_whitelist'])})",
                              callback_data=f"crfield:{project_id}:sender_whitelist")],
        [InlineKeyboardButton(f"🚫 Blocked senders ({_count(rules['sender_blacklist'])})",
                              callback_data=f"crfield:{project_id}:sender_blacklist")],
        [InlineKeyboardButton(f"🔁 Match mode: {str(rules['filter_logic']).upper()}",
                              callback_data=f"crlogic:{project_id}")],
        [InlineKeyboardButton("◀️ Back to Filters", callback_data=f"flt:hub:{project_id}")],
    ])


def media_keyboard(project_id):
    rows, row = [], []
    for choice in MEDIA_FILTER_CHOICES:
        row.append(InlineKeyboardButton(choice, callback_data=f"mediafilterset:{project_id}:{choice}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ Back to Filters", callback_data=f"flt:hub:{project_id}")])
    return InlineKeyboardMarkup(rows)


# ==========================================
# 3. FORMATTING
# ==========================================

def formatting_hub_keyboard(project_id: int, rules, cfg: dict):
    replace_count = len(formatting_service.get_replace_rules(project_id))
    remove_count = len(formatting_service.get_remove_patterns(project_id))
    prefix = rules["prefix"] or ""
    suffix = rules["suffix"] or ""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Advanced cleanup & preview",
                              callback_data=f"advancedformat:{project_id}")],
        [InlineKeyboardButton(f"➕ Prefix: {prefix[:18] or 'not set'}",
                              callback_data=f"fmtfield:{project_id}:prefix"),
         InlineKeyboardButton(f"➖ Suffix: {suffix[:18] or 'not set'}",
                              callback_data=f"fmtfield:{project_id}:suffix")],
        [InlineKeyboardButton(f"🔁 Replace rules ({replace_count})",
                              callback_data=f"fmtreplace:{project_id}"),
         InlineKeyboardButton(f"🧹 Remove patterns ({remove_count})",
                              callback_data=f"fmtremove:{project_id}")],
        [InlineKeyboardButton("🎯 Per-destination overrides",
                              callback_data=f"fmt:destlist:{project_id}")],
        [InlineKeyboardButton("👁 Preview result", callback_data=f"fmtpreview:{project_id}")],
        [InlineKeyboardButton("🗑 Reset all formatting", callback_data=f"fmtclearconfirm:{project_id}")],
        _home_row(project_id),
    ])


async def _render_formatting(query, user_id, project_id):
    rules = formatting_service.get_rules(project_id)
    cfg = formatting_service.get_advanced(project_id)
    mode = settings_service.get_settings(project_id).get("mode")
    warn = ""
    if mode != "copy":
        warn = ("\n\n⚠️ This project is in <b>Forward</b> mode — formatting is skipped.\n"
                "Switch to <b>Copy</b> mode in 🚀 Forwarding to use it.")
    text = (
        "✍️ <b>Formatting</b>\n\n"
        "Pipeline order (deterministic):\n"
        "<code>trim → remove usernames/links → replace/remove patterns → header → body → footer</code>\n\n"
        f"• Prefix: <code>{_e(rules['prefix'] or 'none')}</code>\n"
        f"• Suffix: <code>{_e(rules['suffix'] or 'none')}</code>\n"
        f"• Replace rules: {len(formatting_service.get_replace_rules(project_id))}\n"
        f"• Remove patterns: {len(formatting_service.get_remove_patterns(project_id))}\n"
        f"• Link preview: {_onoff(cfg.get('link_preview'))}\n"
        f"• Mono text: {_onoff(cfg.get('mono'))}\n"
        f"• Header: <code>{_e(cfg.get('header') if cfg.get('header') is not None else 'inherit prefix')}</code>\n"
        f"• Footer: <code>{_e(cfg.get('footer') if cfg.get('footer') is not None else 'inherit suffix')}</code>"
        + warn
    )
    await _edit(query, text, formatting_hub_keyboard(project_id, rules, cfg))


ADV_BOOL_KEYS = [
    ("link_preview", "🔗 Link preview"),
    ("remove_usernames", "👤 Remove @usernames"),
    ("remove_links", "🌐 Remove links"),
    ("disable_hidden_links", "🕵 Disable hidden links"),
    ("mono", "📝 Mono text"),
]


def _adv_keyboard(project_id, cfg, destination_id=None):
    scope = f"{project_id}:{destination_id}" if destination_id else str(project_id)
    rows = []
    row = []
    for key, label in ADV_BOOL_KEYS:
        row.append(InlineKeyboardButton(f"{label}: {'On' if cfg.get(key) else 'Off'}",
                                        callback_data=f"adv:{scope}:toggle:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([
        InlineKeyboardButton(
            f"✂ Words −{cfg.get('remove_first_words', 0)}/−{cfg.get('remove_last_words', 0)}"
            f" keep:{cfg.get('keep_first_words') if cfg.get('keep_first_words') is not None else '∞'}",
            callback_data=f"adv:{scope}:trim:words"),
        InlineKeyboardButton(
            f"📄 Lines −{cfg.get('remove_first_lines', 0)}/−{cfg.get('remove_last_lines', 0)}"
            f" keep:{cfg.get('keep_first_lines') if cfg.get('keep_first_lines') is not None else '∞'}",
            callback_data=f"adv:{scope}:trim:lines"),
    ])
    header = cfg.get("header")
    footer = cfg.get("footer")
    rows.append([
        InlineKeyboardButton(f"📌 Header: {_short(header, 'inherit')}",
                             callback_data=f"adv:{scope}:text:header"),
        InlineKeyboardButton(f"📍 Footer: {_short(footer, 'inherit')}",
                             callback_data=f"adv:{scope}:text:footer"),
    ])
    rows.append([
        InlineKeyboardButton("👁 Preview", callback_data=f"adv:{scope}:preview"),
        InlineKeyboardButton("🗑 Reset", callback_data=f"adv:{scope}:reset"),
    ])
    rows.append([InlineKeyboardButton(
        "◀️ Back to Formatting" if destination_id is None else "◀️ Back",
        callback_data=(f"fmt:hub:{project_id}" if destination_id is None
                       else f"fmt:destlist:{project_id}"))])
    return InlineKeyboardMarkup(rows)


def _short(value, fallback="—"):
    if value is None:
        return fallback
    text = str(value).strip()
    return (text[:12] + "…") if len(text) > 13 else (text or fallback)


def _adv_trim_keyboard(project_id, cfg, unit, destination_id=None):
    scope = f"{project_id}:{destination_id}" if destination_id else str(project_id)
    keys = [f"remove_first_{unit}", f"remove_last_{unit}", f"keep_first_{unit}"]
    rows = [
        [InlineKeyboardButton(f"{key} = {cfg.get(key) if cfg.get(key) is not None else '∞'}",
                              callback_data=f"adv:{scope}:set:{key}")]
        for key in keys
    ]
    rows.append([InlineKeyboardButton("🗑 Clear this limit", callback_data=f"adv:{scope}:none:{keys[2]}")])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"adv:{scope}:menu")])
    return InlineKeyboardMarkup(rows)


async def _render_adv(query, user_id, project_id, destination_id=None):
    cfg = formatting_service.get_advanced(project_id, destination_id)
    scope_note = ""
    if destination_id is not None:
        dest = get_destination(destination_id)
        scope_note = f"\n🎯 Override for destination: <code>{_e(dest['title'] or dest['chat_id']) if dest else destination_id}</code>\n"
    text = (
        "🧪 <b>Advanced cleanup</b>\n" + scope_note +
        "\nThese options apply in <b>Copy</b> mode. Toggle what you need, then use "
        "<b>Preview</b> to see the exact output before it goes live."
    )
    await _edit(query, text, _adv_keyboard(project_id, cfg, destination_id))


def _replace_keyboard(project_id, rules_list):
    rows = []
    for i, rule in enumerate(rules_list):
        rows.append([InlineKeyboardButton(
            f"❌ {rule['find'][:14]} → {rule['replace'][:14]}",
            callback_data=f"fmtreplacedel:{project_id}:{i}")])
    rows.append([InlineKeyboardButton("➕ Add replace rule", callback_data=f"fmtreplaceadd:{project_id}")])
    rows.append([InlineKeyboardButton("◀️ Back to Formatting", callback_data=f"fmt:hub:{project_id}")])
    return InlineKeyboardMarkup(rows)


def _remove_keyboard(project_id, patterns):
    rows = [[InlineKeyboardButton(f"❌ {p[:28]}", callback_data=f"fmtremovedel:{project_id}:{i}")]
            for i, p in enumerate(patterns)]
    rows.append([InlineKeyboardButton("➕ Add remove pattern", callback_data=f"fmtremoveadd:{project_id}")])
    rows.append([InlineKeyboardButton("◀️ Back to Formatting", callback_data=f"fmt:hub:{project_id}")])
    return InlineKeyboardMarkup(rows)


# ==========================================
# 4. WATERMARK
# ==========================================

WM_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "center")
WM_SIZES = (12, 18, 24, 32, 48, 64)
WM_OPACITIES = (0.2, 0.4, 0.6, 0.8, 1.0)


def watermark_keyboard(project_id, settings):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🖼 Watermark: {_onoff(settings.get('enabled'))}",
                              callback_data=f"wmtoggle:{project_id}")],
        [InlineKeyboardButton(f"🏷 Type: {str(settings.get('type') or 'text').capitalize()}",
                              callback_data=f"wmcycletype:{project_id}")],
        [InlineKeyboardButton(f"✍️ Text: {_short(settings.get('text'), 'not set')}",
                              callback_data=f"wmsettext:{project_id}")],
        [InlineKeyboardButton(f"📍 Position: {settings.get('position')}",
                              callback_data=f"wmcyclepos:{project_id}")],
        [InlineKeyboardButton(f"🔍 Size: {settings.get('font_size')}px",
                              callback_data=f"wmcyclesize:{project_id}")],
        [InlineKeyboardButton(f"🌫 Opacity: {int(float(settings.get('opacity', 0.7)) * 100)}%",
                              callback_data=f"wmcycleopacity:{project_id}")],
        _home_row(project_id),
    ])


async def _render_watermark(query, user_id, project_id, note=""):
    settings = watermark_service.ensure_watermark_settings(project_id)
    mode = settings_service.get_settings(project_id).get("mode")
    extra = ""
    if mode != "copy":
        extra = "\n\n⚠️ Watermark needs <b>Copy</b> mode (🚀 Forwarding)."
    if settings.get("type") == "text" and not settings.get("text"):
        extra += "\n\n⚠️ Set the watermark text, otherwise nothing is drawn."
    text = (
        "🖼️ <b>Watermark</b>\n\n"
        "Applied to photos sent in Copy mode. If processing fails the original "
        "media is forwarded untouched and the error is logged.\n\n"
        f"• Enabled: {_onoff(settings.get('enabled'))}\n"
        f"• Type: {_e(settings.get('type'))}\n"
        f"• Text: <code>{_e(settings.get('text') or 'not set')}</code>\n"
        f"• Position: {_e(settings.get('position'))}\n"
        f"• Size: {_e(settings.get('font_size'))}px\n"
        f"• Opacity: {int(float(settings.get('opacity', 0.7)) * 100)}%"
        + extra + note
    )
    await _edit(query, text, watermark_keyboard(project_id, settings))


# ==========================================
# 5. AI TOOLS
# ==========================================

AI_TONES = ("original", "casual", "professional", "promotional", "funny")
AI_LENGTHS = ("keep", "shorten", "expand")
AI_LANGS = (None, "English", "Hindi", "Bengali", "Urdu", "Spanish", "Arabic", "Indonesian")


def ai_keyboard(project_id, settings):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🤖 AI Rewriter: {_onoff(settings.get('enabled'))}",
                              callback_data=f"aitoggle:{project_id}")],
        [
            InlineKeyboardButton(f"✍️ Tone: {settings.get('tone')}",
                                 callback_data=f"aicycletone:{project_id}"),
            InlineKeyboardButton(f"📏 Length: {settings.get('length_mode')}",
                                 callback_data=f"aicyclelength:{project_id}"),
        ],
        [InlineKeyboardButton(f"🌍 Translate: {settings.get('language') or 'Off'}",
                              callback_data=f"aicyclelang:{project_id}")],
        [
            InlineKeyboardButton(f"🔗 Keep URLs: {_onoff(settings.get('preserve_urls'))}",
                                 callback_data=f"aitoggleurls:{project_id}"),
            InlineKeyboardButton(f"#️⃣ Keep hashtags: {_onoff(settings.get('preserve_hashtags'))}",
                                 callback_data=f"aitogglehashtags:{project_id}"),
        ],
        [
            InlineKeyboardButton(f"📢 Add CTA: {_onoff(settings.get('add_cta'))}",
                                 callback_data=f"aitogglecta:{project_id}"),
            InlineKeyboardButton(f"🧹 Anti-spam: {_onoff(settings.get('remove_spam'))}",
                                 callback_data=f"aitogglespam:{project_id}"),
        ],
        [InlineKeyboardButton(f"📝 Custom prompt: {_short(settings.get('custom_prompt'), 'none')}",
                              callback_data=f"aiprompt:{project_id}")],
        _home_row(project_id),
    ])


async def _render_ai(query, user_id, project_id):
    allowed = plan_service.has_feature(user_id, "ai_rewrite")
    settings = ai_service.ensure_ai_settings(project_id)
    if not allowed:
        text = (
            "🤖 <b>AI Rewriter</b>\n\n"
            "🔒 AI rewriting is included with <b>Pro</b> and <b>Creator</b> plans.\n"
            "Upgrade from 💳 Subscription to switch it on."
        )
        await _edit(query, text, InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 View plans", callback_data="pay:method")],
            _back_row(project_id),
        ]))
        return

    text = (
        "🤖 <b>AI Rewriter</b>\n\n"
        "Rewrites captions/text before sending, in <b>Copy</b> mode only. "
        "If the AI call fails the original text is used and the failure is logged.\n\n"
        f"• Enabled: {_onoff(settings.get('enabled'))}\n"
        f"• Tone: <code>{_e(settings.get('tone'))}</code>\n"
        f"• Length: <code>{_e(settings.get('length_mode'))}</code>\n"
        f"• Translate to: <code>{_e(settings.get('language') or 'Off')}</code>\n"
        f"• Keep URLs: {_onoff(settings.get('preserve_urls'))}\n"
        f"• Keep hashtags: {_onoff(settings.get('preserve_hashtags'))}\n"
        f"• Add CTA: {_onoff(settings.get('add_cta'))}\n"
        f"• Anti-spam clean: {_onoff(settings.get('remove_spam'))}\n"
        f"• Custom prompt: <code>{_e(settings.get('custom_prompt') or 'none')}</code>"
    )
    await _edit(query, text, ai_keyboard(project_id, settings))


# ==========================================
# 6. AFFILIATE
# ==========================================

AFF_PROVIDERS = (
    ("amazon", "📦 Amazon", "amazon_associate_tag"),
    ("flipkart", "🛒 Flipkart", "flipkart_publisher_id"),
    ("meesho", "🧘 Meesho", "meesho_partner_id"),
    ("wishlink", "🌈 Wishlink", "wishlink_partner_id"),
    ("earnkaro", "💰 EarnKaro", "earnkaro_publisher_id"),
)


def affiliate_keyboard(project_id, settings):
    rows = [[InlineKeyboardButton(f"💰 Affiliate replacer: {_onoff(settings.get('enabled'))}",
                                  callback_data=f"afftogglemain:{project_id}")]]
    pair = []
    for key, label, _idfield in AFF_PROVIDERS:
        pair.append(InlineKeyboardButton(f"{label} {'🟢' if settings.get(key + '_enabled') else '🔴'}",
                                         callback_data=f"afftoggle{key}:{project_id}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton("🔑 Affiliate IDs / tags",
                                      callback_data=f"aff:ids:{project_id}")])
    rows.append(_home_row(project_id))
    return InlineKeyboardMarkup(rows)


def affiliate_ids_keyboard(project_id, settings):
    rows = []
    pair = []
    for key, label, idfield in AFF_PROVIDERS:
        pair.append(InlineKeyboardButton(f"{label}: {_short(settings.get(idfield), 'not set')}",
                                         callback_data=f"affsetid:{project_id}:{key}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton("◀️ Back to Affiliate", callback_data=f"aff:hub:{project_id}")])
    return InlineKeyboardMarkup(rows)


async def _render_affiliate(query, user_id, project_id):
    settings = affiliate_service.ensure_affiliate_settings(project_id)
    text = (
        "💰 <b>Affiliate Replacer</b>\n\n"
        "Rewrites supported store links in <b>Copy</b> mode. If a provider lookup "
        "fails, the original link is kept — links are never destroyed.\n\n"
        + "\n".join(
            f"• {label}: {'🟢 on' if settings.get(key + '_enabled') else '🔴 off'}"
            f" — <code>{_e(settings.get(idfield) or 'no id set')}</code>"
            for key, label, idfield in AFF_PROVIDERS
        )
    )
    await _edit(query, text, affiliate_keyboard(project_id, settings))


# ==========================================
# 7. AUTO & EDITS (post edit sync + reactions)
# ==========================================

def _auto_reaction(project_id: int) -> dict:
    """Return (creating if needed) the auto-reaction rule row for a project."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM auto_reactions WHERE project_id=? ORDER BY id LIMIT 1", (project_id,))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO auto_reactions(project_id, emojis, trigger_mode, keywords, apply_source, apply_target)"
                " VALUES(?, '👍', 'all', '', 0, 1)",
                (project_id,),
            )
            conn.commit()
            cur.execute("SELECT * FROM auto_reactions WHERE project_id=? ORDER BY id LIMIT 1", (project_id,))
            row = cur.fetchone()
        return dict(row)
    finally:
        conn.close()


def auto_keyboard(project_id, settings, reaction):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔄 Post Edit Sync: {_onoff(settings.get('post_edit_sync'))}",
                              callback_data=f"auto:pestoggle:{project_id}")],
        [InlineKeyboardButton(f"👍 Auto Reaction: {_onoff(reaction.get('enabled'))}",
                              callback_data=f"auto:rtoggle:{project_id}")],
        [InlineKeyboardButton(f"😀 Emoji: {reaction.get('emojis') or '👍'}",
                              callback_data=f"auto:remoji:{project_id}")],
        [InlineKeyboardButton(f"🎯 Trigger: {reaction.get('trigger_mode') or 'all'}",
                              callback_data=f"auto:rmode:{project_id}")],
        [InlineKeyboardButton(f"🔑 Keywords: {_short(reaction.get('keywords'), 'all messages')}",
                              callback_data=f"auto:rkeywords:{project_id}")],
        [InlineKeyboardButton(
            f"📍 Apply: {'source' if reaction.get('apply_source') else ''}"
            f"{'+' if reaction.get('apply_source') and reaction.get('apply_target') else ''}"
            f"{'destination' if reaction.get('apply_target') else ''}",
            callback_data=f"auto:rside:{project_id}")],
        _home_row(project_id),
    ])


async def _render_auto(query, user_id, project_id):
    settings = settings_service.get_settings(project_id)
    reaction = _auto_reaction(project_id)
    text = (
        "🔄 <b>Auto & Edits</b>\n\n"
        "<b>Post Edit Sync</b> — when a source message you already forwarded is edited, "
        "ChannelFlow edits the destination copy too (where permissions allow).\n"
        f"Status: {_onoff(settings.get('post_edit_sync'))}\n\n"
        "<b>Auto Reaction</b> — add an emoji reaction to forwarded messages. "
        "Reactions are idempotent: a message is never reacted to twice, and a "
        "failed reaction never blocks forwarding.\n"
        f"Status: {_onoff(reaction.get('enabled'))}\n"
        f"Emoji: <code>{_e(reaction.get('emojis') or '👍')}</code>\n"
        f"Trigger: <code>{_e(reaction.get('trigger_mode') or 'all')}</code>\n"
        f"Keywords: <code>{_e(reaction.get('keywords') or 'all messages')}</code>"
    )
    await _edit(query, text, auto_keyboard(project_id, settings, reaction))


# ==========================================
# 8. SAFE PACING
# ==========================================

def pacing_keyboard(project_id, settings):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"⏱ Delay window: {settings.get('delay_min', 0)}–{settings.get('delay_max', 0)}s",
            callback_data=f"setdelay:{project_id}")],
        [InlineKeyboardButton("⚡ No delay (fastest)", callback_data=f"spd:set:{project_id}:0:0")],
        [InlineKeyboardButton("🐢 Gentle (2–6s)", callback_data=f"spd:set:{project_id}:2:6")],
        [InlineKeyboardButton("🚶 Steady (5–15s)", callback_data=f"spd:set:{project_id}:5:15")],
        [InlineKeyboardButton("🛡 Very safe (15–45s)", callback_data=f"spd:set:{project_id}:15:45")],
        _home_row(project_id),
    ])


async def _render_pacing(query, user_id, project_id):
    settings = settings_service.get_settings(project_id)
    text = (
        "⚡ <b>Safe Pacing</b>\n\n"
        "A random delay inside the window is applied before each send. This spreads "
        "bursts out and keeps you clear of Telegram flood limits.\n\n"
        f"Current window: <b>{settings.get('delay_min', 0)}–{settings.get('delay_max', 0)} seconds</b>\n\n"
        "Albums are always sent together after a single short debounce."
    )
    await _edit(query, text, pacing_keyboard(project_id, settings))


# ==========================================
# 9. ACTIVITY & LOGS / STATS
# ==========================================

async def _render_activity(query, user_id, project_id):
    stats = stats_service.get_stats(project_id)
    logs = log_service.get_logs(project_id, limit=10)
    log_lines = "\n".join(
        f"• <code>{_e(row['created_at'])}</code> {_e(row['level'])}: {_e(row['message'])}"
        for row in logs
    ) or "No log entries yet."
    text = (
        "📊 <b>Activity & Logs</b>\n\n"
        f"• Forwarded: <b>{stats.get('forwarded', 0)}</b>\n"
        f"• Filtered: {stats.get('filtered', 0)}\n"
        f"• Failed: {stats.get('failed', 0)}\n"
        f"• Today: {stats.get('today', 0)}\n\n"
        "<b>Last 10 events</b>\n" + log_lines
    )
    await _edit(query, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"act:view:{project_id}")],
        [InlineKeyboardButton("🗑 Clear logs", callback_data=f"act:clearconfirm:{project_id}")],
        _home_row(project_id),
    ]))


async def _render_stats(query, user_id, project_id):
    stats = stats_service.get_stats(project_id)
    project = get_project(project_id)
    text = (
        "📈 <b>Project Analytics</b>\n\n"
        f"Project: <b>{_e(project['name'])}</b>\n"
        f"Status: {'🟢 Active' if project['status'] else '⏸ Paused'}\n\n"
        f"• Forwarded (all time): <b>{stats.get('forwarded', 0)}</b>\n"
        f"• Filtered out: {stats.get('filtered', 0)}\n"
        f"• Failed: {stats.get('failed', 0)}\n"
        f"• Forwarded today: {stats.get('today', 0)}"
    )
    await _edit(query, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Activity & Logs", callback_data=f"act:view:{project_id}")],
        _home_row(project_id),
    ]))


# ==========================================
# 10. DRY RUN
# ==========================================

def _dry_run_report(project_id, sample, settings, rules, reaction_note):
    """Pure, side-effect-free simulation of the filter + formatting chain."""
    text = sample or ""
    checks = []

    media = "text"
    media_filter = settings.get("media_filter") or "all"
    if media_filter != "all" and media_filter != media:
        checks.append(f"❌ Media filter expects <code>{_e(media_filter)}</code>, sample is text")

    lowered = text.lower()
    whitelist = [w.strip().lower() for w in (settings.get("keyword_whitelist") or "").split(",") if w.strip()]
    if whitelist and not any(w in lowered for w in whitelist):
        checks.append(f"❌ None of the required keywords found ({_e(', '.join(whitelist))})")

    blacklist = [w.strip().lower() for w in (settings.get("keyword_blacklist") or "").split(",") if w.strip()]
    if blacklist and any(w in lowered for w in blacklist):
        checks.append(f"❌ Blocked keyword found ({_e(', '.join(blacklist))})")

    regex = (settings.get("regex_filter") or "").strip()
    if regex:
        try:
            if re.search(regex, text) is None:
                checks.append("❌ Regex filter did not match")
        except re.error as exc:
            checks.append(f"⚠️ Regex filter is invalid: {_e(exc)}")

    min_len, max_len = rules["min_length"], rules["max_length"]
    if min_len is not None and len(text) < min_len:
        checks.append(f"❌ Shorter than min length ({min_len})")
    if max_len is not None and len(text) > max_len:
        checks.append(f"❌ Longer than max length ({max_len})")

    outgoing = text
    if settings.get("mode") == "copy":
        outgoing = formatting_service.apply_formatting(project_id, text)
    else:
        checks.append("ℹ️ Project is in <b>Forward</b> mode — no transformations applied")

    if len(outgoing) > 4096:
        checks.append(f"❌ Output is {len(outgoing)} chars — Telegram limit is 4096")

    verdict = "✅ This message <b>would be forwarded</b>." if not any(
        c.startswith("❌") for c in checks) else "🚫 This message <b>would be skipped</b>."

    lines = "\n".join(checks) if checks else "• No filter rules rejected it."
    return (
        "🧪 <b>Dry-Run Result</b>\n\n"
        f"{verdict}\n\n"
        "<b>Filter checks</b>\n" + lines + "\n\n"
        "<b>Message that would be sent</b>\n"
        f"<pre>{_e(outgoing)}</pre>"
        + reaction_note
    )


# ==========================================
# CALLBACK ROUTER
# ==========================================

async def handle_callbacks(query, user_id: int, action: str, parts: list, context) -> bool:
    """Handle every project feature callback. Returns True when handled."""

    sub = parts[1] if len(parts) > 1 else ""

    # ---------------- new project alias ----------------
    if action == "newproj":
        query.data = "proj:new"
        return False  # let handlers_projects own the project-creation flow

    # ---------------- edit project root ----------------
    if action == "editproj":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        settings = settings_service.get_settings(project_id)
        text = (
            "⚙️ <b>Edit Project</b>\n\n"
            "Choose a section to configure:"
        )
        await _edit(query, text, InlineKeyboardMarkup([
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
                InlineKeyboardButton("🤖 AI Tools", callback_data=f"ai:hub:{project_id}"),
                InlineKeyboardButton("💰 Affiliate", callback_data=f"aff:hub:{project_id}"),
            ],
            [
                InlineKeyboardButton("🔄 Auto & Edits", callback_data=f"auto:hub:{project_id}"),
                InlineKeyboardButton("⚡ Safe Pacing", callback_data=f"spd:hub:{project_id}"),
            ],
            [
                InlineKeyboardButton("📊 Analytics", callback_data=f"stats:{project_id}"),
                InlineKeyboardButton("✏️ Rename", callback_data=f"rename:{project_id}"),
            ],
            _home_row(project_id),
        ]))
        return True

    # ---------------- hubs ----------------
    if action == "fwd" and sub == "settings":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        await _render_forwarding(query, user_id, project_id)
        return True

    if action == "projsettings":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        await _render_forwarding(query, user_id, project_id)
        return True

    if action == "flt" and sub == "hub":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        await _render_filters(query, user_id, project_id)
        return True

    if action == "projfilters":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        await _render_filters(query, user_id, project_id)
        return True

    if action == "fmt" and sub == "hub":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        await _render_formatting(query, user_id, project_id)
        return True

    if action == "fmtroot":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        await _render_formatting(query, user_id, project_id)
        return True

    if action == "wm" and sub == "hub":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        await _render_watermark(query, user_id, project_id)
        return True

    if action == "ai" and sub == "hub":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        await _render_ai(query, user_id, project_id)
        return True

    if action == "aff" and sub == "hub":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        await _render_affiliate(query, user_id, project_id)
        return True

    if action == "aff" and sub == "ids":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        settings = affiliate_service.ensure_affiliate_settings(project_id)
        await _edit(query,
                    "🔑 <b>Affiliate IDs</b>\n\n"
                    "Tap a provider to set its tag/publisher id. These are stored "
                    "per project and never shown to other users.",
                    affiliate_ids_keyboard(project_id, settings))
        return True

    if action == "affsetid":
        project_id = int(parts[1])
        provider = parts[2]
        if not _owned(project_id, user_id):
            return True
        field = next((f for k, _l, f in AFF_PROVIDERS if k == provider), None)
        if not field:
            return True
        _ask(user_id, "aff_id", project_id, field=field, provider=provider)
        await _send(query, f"🔑 Send the <b>{_e(provider)}</b> affiliate id/tag (or send <code>-</code> to clear):")
        return True

    if action == "auto" and sub == "hub":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        await _render_auto(query, user_id, project_id)
        return True

    if action == "spd" and sub == "hub":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        await _render_pacing(query, user_id, project_id)
        return True

    if action == "act" and sub == "view":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        await _render_activity(query, user_id, project_id)
        return True

    if action == "stats":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        await _render_stats(query, user_id, project_id)
        return True

    # ---------------- forwarding rules ----------------
    if action == "togglemode":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        settings_service.toggle_mode(project_id)
        await force_refresh_routes()
        await _render_forwarding(query, user_id, project_id)
        return True

    if action in ("togglesilent", "toggleprotect", "togglealbum"):
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        field = {"togglesilent": "silent",
                 "toggleprotect": "protect_content",
                 "togglealbum": "keep_media_groups"}[action]
        settings_service.toggle_flag(project_id, field)
        await force_refresh_routes()
        await _render_forwarding(query, user_id, project_id)
        return True

    if action == "setdelay":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        _ask(user_id, "delay", project_id)
        await _send(query, "⏱ Send the delay window in seconds as <code>min max</code> — e.g. <code>2 6</code>.\n"
                           "Send <code>0 0</code> to disable the delay.")
        return True

    if action == "spd" and sub == "set":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        settings_service.set_delay(project_id, float(parts[3]), float(parts[4]))
        await force_refresh_routes()
        await _render_pacing(query, user_id, project_id)
        return True

    # ---------------- filters ----------------
    if action == "filterkw":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        settings = settings_service.get_settings(project_id)
        await _edit(query,
                    "🔑 <b>Keyword filters</b>\n\n"
                    "• <b>Required</b>: message must contain at least one of these words.\n"
                    "• <b>Blocked</b>: message is skipped if it contains any of these words.\n\n"
                    "Send a comma-separated list, e.g. <code>deal, offer, discount</code>",
                    keyword_keyboard(project_id, settings))
        return True

    if action in ("setwhitelist", "setblacklist"):
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        kind = "keyword_whitelist" if action == "setwhitelist" else "keyword_blacklist"
        _ask(user_id, "keywords", project_id, field=kind)
        await _send(query, "🔑 Send a comma-separated keyword list (or <code>-</code> to clear):")
        return True

    if action == "filterdomains":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        rules = content_rules_service.get_rules(project_id)
        await _edit(query,
                    "🌐 <b>Domain filters</b>\n\n"
                    "• <b>Allowed</b>: if set, only links to these domains pass.\n"
                    "• <b>Blocked</b>: links to these domains are rejected.\n\n"
                    "Send domains without the scheme, e.g. <code>amazon.in, flipkart.com</code>",
                    domain_keyboard(project_id, rules))
        return True

    if action == "filtersenders":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        rules = content_rules_service.get_rules(project_id)
        await _edit(query,
                    "👤 <b>Sender filters</b>\n\n"
                    "Match on Telegram user/channel id or <code>@username</code>.\n"
                    "• <b>ALL</b>: every sender rule must pass.\n"
                    "• <b>ANY</b>: passing one sender rule is enough.",
                    sender_keyboard(project_id, rules))
        return True

    if action == "crlogic":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        rules = content_rules_service.get_rules(project_id)
        content_rules_service.update_rules(
            project_id, filter_logic="all" if rules["filter_logic"] != "all" else "any")
        await force_refresh_routes()
        rules = content_rules_service.get_rules(project_id)
        await _edit(query,
                    "👤 <b>Sender filters</b>\n\n"
                    f"Match mode is now <code>{_e(rules['filter_logic'])}</code>.",
                    sender_keyboard(project_id, rules))
        return True

    if action == "crfield":
        project_id = int(parts[1])
        field = parts[2]
        if not _owned(project_id, user_id):
            return True
        _ask(user_id, "crfield", project_id, field=field)
        prompts = {
            "hashtag_filter": "#️⃣ Send comma-separated hashtags, e.g. <code>#deals, #offer</code>",
            "domain_whitelist": "🌐 Send allowed domains, e.g. <code>amazon.in, flipkart.com</code>",
            "domain_blacklist": "⛔ Send blocked domains, e.g. <code>spam.com</code>",
            "sender_whitelist": "👤 Send allowed sender ids/usernames, comma separated",
            "sender_blacklist": "🚫 Send blocked sender ids/usernames, comma separated",
            "length": "📏 Send the length window as <code>min max</code>, e.g. <code>20 500</code>",
        }
        await _send(query, prompts.get(field, "Send the new value (or <code>-</code> to clear):"))
        return True

    if action == "setregex":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        _ask(user_id, "regex", project_id)
        await _send(query, "🔤 Send a Python regular expression the message must match "
                           "(or <code>-</code> to clear).\nExample: <code>(?i)loot|deal</code>")
        return True

    if action == "mediafilter":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        await _edit(query, "🎛 <b>Media type filter</b>\n\nOnly this media type will be forwarded.",
                    media_keyboard(project_id))
        return True

    if action == "mediafilterset":
        project_id, choice = int(parts[1]), parts[2]
        if not _owned(project_id, user_id):
            return True
        settings_service.set_media_filter(project_id, choice)
        await force_refresh_routes()
        await _render_filters(query, user_id, project_id)
        return True

    if action == "clearfiltersconfirm":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        await _edit(query,
                    "🧹 Clear every filter on this project (keywords, hashtags, domains, "
                    "senders, length, regex, media type)?",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("🗑 Yes, clear all", callback_data=f"clearfiltersyes:{project_id}")],
                        [InlineKeyboardButton("Cancel", callback_data=f"flt:hub:{project_id}")],
                    ]))
        return True

    if action == "clearfiltersyes":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        settings_service.update_settings(
            project_id, keyword_whitelist="", keyword_blacklist="", regex_filter="", media_filter="all")
        content_rules_service.clear_rules(project_id)
        await force_refresh_routes()
        await _render_filters(query, user_id, project_id)
        return True

    # ---------------- formatting ----------------
    if action == "advancedformat":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        await _render_adv(query, user_id, project_id)
        return True

    if action == "fmt" and sub == "destlist":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        dests = get_destinations(project_id)
        rows = [[InlineKeyboardButton(f"🎯 {d['title'] or d['chat_id']}",
                                      callback_data=f"fmt:dest:{project_id}:{d['id']}")]
                for d in dests]
        if not rows:
            rows = [[InlineKeyboardButton("No destinations yet", callback_data=f"tgt:list:{project_id}")]]
        rows.append([InlineKeyboardButton("◀️ Back to Formatting", callback_data=f"fmt:hub:{project_id}")])
        await _edit(query,
                    "🎯 <b>Per-destination formatting</b>\n\n"
                    "Pick a destination to override the project defaults for that target only.",
                    InlineKeyboardMarkup(rows))
        return True

    if action == "fmt" and sub == "dest":
        project_id, destination_id = int(parts[2]), int(parts[3])
        if not _owned(project_id, user_id):
            return True
        await _render_adv(query, user_id, project_id, destination_id)
        return True

    if action == "adv":
        # adv:<pid>[:<did>]:<verb>[:<key>]
        rest = parts[1:]
        if len(rest) >= 3 and rest[1].isdigit():
            project_id, destination_id, verb = int(rest[0]), int(rest[1]), rest[2]
            key = rest[3] if len(rest) > 3 else None
        else:
            project_id, destination_id, verb = int(rest[0]), None, rest[1]
            key = rest[2] if len(rest) > 2 else None
        if not _owned(project_id, user_id):
            return True

        cfg = formatting_service.get_advanced(project_id, destination_id)

        if verb == "toggle" and key in dict(ADV_BOOL_KEYS):
            await _adv_apply(user_id, project_id, destination_id, {key: not cfg.get(key)})
            await _render_adv(query, user_id, project_id, destination_id)
            return True

        if verb == "trim" and key in ("words", "lines"):
            await _edit(query,
                        f"✂ <b>Trim {key}</b>\n\nSet a non-negative limit, or clear the keep-limit.",
                        _adv_trim_keyboard(project_id, cfg, key, destination_id))
            return True

        if verb == "set":
            _ask(user_id, "adv_int", project_id, key=key, destination_id=destination_id)
            await _send(query, f"🔢 Send the new value for <code>{_e(key)}</code> (0–100000):")
            return True

        if verb == "none":
            await _adv_apply(user_id, project_id, destination_id, {key: None})
            await _render_adv(query, user_id, project_id, destination_id)
            return True

        if verb == "text":
            _ask(user_id, "adv_text", project_id, key=key, destination_id=destination_id)
            await _send(query, f"📝 Send the {_e(key)} text, or send <code>-</code> to inherit "
                               f"the project {'prefix' if key == 'header' else 'suffix'}.")
            return True

        if verb == "preview":
            _ask(user_id, "adv_preview", project_id, destination_id=destination_id)
            await _send(query, "👁 Send a sample message to preview the saved formatting:")
            return True

        if verb == "reset":
            await _adv_apply(user_id, project_id, destination_id,
                             dict(formatting_service.ADVANCED_DEFAULTS))
            await _render_adv(query, user_id, project_id, destination_id)
            return True

        if verb == "menu":
            await _render_adv(query, user_id, project_id, destination_id)
            return True
        return True

    if action == "fmtfield":
        project_id, field = int(parts[1]), parts[2]
        if not _owned(project_id, user_id):
            return True
        _ask(user_id, "fmt_field", project_id, field=field)
        await _send(query, f"📝 Send the new {_e(field)} text (or <code>-</code> to clear):")
        return True

    if action == "fmtprefixsuffix":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        rules = formatting_service.get_rules(project_id)
        await _edit(query,
                    "➕ <b>Prefix / Suffix</b>\n\n"
                    f"• Prefix: <code>{_e(rules['prefix'] or 'none')}</code>\n"
                    f"• Suffix: <code>{_e(rules['suffix'] or 'none')}</code>\n\n"
                    "In advanced cleanup, a null header/footer inherits these values.",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("➕ Set prefix", callback_data=f"fmtfield:{project_id}:prefix"),
                         InlineKeyboardButton("➖ Set suffix", callback_data=f"fmtfield:{project_id}:suffix")],
                        [InlineKeyboardButton("◀️ Back to Formatting", callback_data=f"fmt:hub:{project_id}")],
                    ]))
        return True

    if action == "fmtreplace":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        rules_list = formatting_service.get_replace_rules(project_id)
        await _edit(query,
                    "🔁 <b>Replace rules</b>\n\n"
                    "Applied in order. URLs are never modified by these rules.",
                    _replace_keyboard(project_id, rules_list))
        return True

    if action == "fmtreplaceadd":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        _ask(user_id, "fmt_replace_add", project_id)
        await _send(query, "🔁 Send the rule as <code>find || replace</code> — "
                           "e.g. <code>OLD || NEW</code>")
        return True

    if action == "fmtreplacedel":
        project_id, index = int(parts[1]), int(parts[2])
        if not _owned(project_id, user_id):
            return True
        formatting_service.remove_replace_rule(project_id, index)
        await force_refresh_routes()
        rules_list = formatting_service.get_replace_rules(project_id)
        await _edit(query, "🔁 <b>Replace rules</b>", _replace_keyboard(project_id, rules_list))
        return True

    if action == "fmtremove":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        patterns = formatting_service.get_remove_patterns(project_id)
        await _edit(query,
                    "🧹 <b>Remove patterns</b>\n\n"
                    "Regular expressions removed from the text. Invalid patterns are skipped safely.",
                    _remove_keyboard(project_id, patterns))
        return True

    if action == "fmtremoveadd":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        _ask(user_id, "fmt_remove_add", project_id)
        await _send(query, "🧹 Send a regular expression to remove — e.g. <code>(?i)sponsor</code>")
        return True

    if action == "fmtremovedel":
        project_id, index = int(parts[1]), int(parts[2])
        if not _owned(project_id, user_id):
            return True
        formatting_service.remove_remove_pattern(project_id, index)
        await force_refresh_routes()
        patterns = formatting_service.get_remove_patterns(project_id)
        await _edit(query, "🧹 <b>Remove patterns</b>", _remove_keyboard(project_id, patterns))
        return True

    if action == "fmtpreview":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        _ask(user_id, "fmt_preview", project_id)
        await _send(query, "👁 Send a sample message and I'll show the exact formatted output:")
        return True

    if action == "fmtclearconfirm":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        await _edit(query, "🗑 Reset prefix, suffix, replace rules, remove patterns and advanced cleanup?",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("🗑 Yes, reset", callback_data=f"fmtclear:{project_id}")],
                        [InlineKeyboardButton("Cancel", callback_data=f"fmt:hub:{project_id}")],
                    ]))
        return True

    if action == "fmtclear":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        formatting_service.clear_rules(project_id)
        formatting_service.configure(user_id, project_id, dict(formatting_service.ADVANCED_DEFAULTS))
        await force_refresh_routes()
        await _render_formatting(query, user_id, project_id)
        return True

    # ---------------- watermark ----------------
    if action == "wmtoggle":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        settings = watermark_service.ensure_watermark_settings(project_id)
        if not settings.get("enabled") and not plan_service.has_feature(user_id, "watermark"):
            await _send(query, "🔒 Watermark is included with <b>Pro</b> and <b>Creator</b> plans.")
            await _render_watermark(query, user_id, project_id)
            return True
        watermark_service.update_watermark_settings(project_id, enabled=0 if settings.get("enabled") else 1)
        await force_refresh_routes()
        await _render_watermark(query, user_id, project_id)
        return True

    if action == "wmcycletype":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        settings = watermark_service.ensure_watermark_settings(project_id)
        new_type = "logo" if (settings.get("type") or "text") == "text" else "text"
        if new_type == "logo" and not settings.get("logo_path"):
            await _send(query, "🖼 Logo mode needs a logo file first — staying on text mode for now.")
            await _render_watermark(query, user_id, project_id)
            return True
        watermark_service.update_watermark_settings(project_id, type=new_type)
        await force_refresh_routes()
        await _render_watermark(query, user_id, project_id)
        return True

    if action == "wmsettext":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        _ask(user_id, "wm_text", project_id)
        await _send(query, "✍️ Send the watermark text (or <code>-</code> to clear):")
        return True

    if action == "wmcyclepos":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        settings = watermark_service.ensure_watermark_settings(project_id)
        current = settings.get("position") or "bottom-right"
        nxt = WM_POSITIONS[(WM_POSITIONS.index(current) + 1) % len(WM_POSITIONS)] if current in WM_POSITIONS else "bottom-right"
        watermark_service.update_watermark_settings(project_id, position=nxt)
        await force_refresh_routes()
        await _render_watermark(query, user_id, project_id)
        return True

    if action == "wmcyclesize":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        settings = watermark_service.ensure_watermark_settings(project_id)
        current = int(settings.get("font_size") or 24)
        nxt = WM_SIZES[0]
        for size in WM_SIZES:
            if size > current:
                nxt = size
                break
        watermark_service.update_watermark_settings(project_id, font_size=nxt)
        await force_refresh_routes()
        await _render_watermark(query, user_id, project_id)
        return True

    if action == "wmcycleopacity":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        settings = watermark_service.ensure_watermark_settings(project_id)
        current = float(settings.get("opacity") or 0.7)
        nxt = WM_OPACITIES[0]
        for value in WM_OPACITIES:
            if value > current + 0.01:
                nxt = value
                break
        watermark_service.update_watermark_settings(project_id, opacity=nxt)
        await force_refresh_routes()
        await _render_watermark(query, user_id, project_id)
        return True

    # ---------------- AI ----------------
    if action == "aitoggle":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        if not plan_service.has_feature(user_id, "ai_rewrite"):
            await _send(query, "🔒 AI rewriting is included with <b>Pro</b> and <b>Creator</b> plans.")
            return True
        settings = ai_service.ensure_ai_settings(project_id)
        ai_service.update_ai_settings(project_id, enabled=0 if settings.get("enabled") else 1)
        await force_refresh_routes()
        await _render_ai(query, user_id, project_id)
        return True

    if action == "aicycletone":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        settings = ai_service.ensure_ai_settings(project_id)
        current = settings.get("tone") or "original"
        nxt = AI_TONES[(AI_TONES.index(current) + 1) % len(AI_TONES)] if current in AI_TONES else "original"
        ai_service.update_ai_settings(project_id, tone=nxt)
        await force_refresh_routes()
        await _render_ai(query, user_id, project_id)
        return True

    if action == "aicyclelength":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        settings = ai_service.ensure_ai_settings(project_id)
        current = settings.get("length_mode") or "keep"
        nxt = AI_LENGTHS[(AI_LENGTHS.index(current) + 1) % len(AI_LENGTHS)] if current in AI_LENGTHS else "keep"
        ai_service.update_ai_settings(project_id, length_mode=nxt)
        await force_refresh_routes()
        await _render_ai(query, user_id, project_id)
        return True

    if action == "aicyclelang":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        settings = ai_service.ensure_ai_settings(project_id)
        current = settings.get("language")
        idx = AI_LANGS.index(current) if current in AI_LANGS else -1
        nxt = AI_LANGS[(idx + 1) % len(AI_LANGS)]
        ai_service.update_ai_settings(project_id, language=nxt)
        await force_refresh_routes()
        await _render_ai(query, user_id, project_id)
        return True

    if action in ("aitoggleurls", "aitogglehashtags", "aitogglecta", "aitogglespam"):
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        field = {"aitoggleurls": "preserve_urls",
                 "aitogglehashtags": "preserve_hashtags",
                 "aitogglecta": "add_cta",
                 "aitogglespam": "remove_spam"}[action]
        settings = ai_service.ensure_ai_settings(project_id)
        ai_service.update_ai_settings(project_id, **{field: 0 if settings.get(field) else 1})
        await force_refresh_routes()
        await _render_ai(query, user_id, project_id)
        return True

    if action == "aiprompt":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        _ask(user_id, "ai_prompt", project_id)
        await _send(query, "📝 Send the custom AI instruction (or <code>-</code> to clear):")
        return True

    # ---------------- affiliate ----------------
    if action == "afftogglemain":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        settings = affiliate_service.ensure_affiliate_settings(project_id)
        affiliate_service.update_affiliate_settings(project_id, enabled=0 if settings.get("enabled") else 1)
        await force_refresh_routes()
        await _render_affiliate(query, user_id, project_id)
        return True

    for key, _label, _idfield in AFF_PROVIDERS:
        if action == f"afftoggle{key}":
            project_id = int(parts[1])
            if not _owned(project_id, user_id):
                return True
            field = key + "_enabled"
            settings = affiliate_service.ensure_affiliate_settings(project_id)
            affiliate_service.update_affiliate_settings(project_id, **{field: 0 if settings.get(field) else 1})
            await force_refresh_routes()
            await _render_affiliate(query, user_id, project_id)
            return True

    # ---------------- auto & edits ----------------
    if action == "auto" and sub == "pestoggle":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        settings = settings_service.get_settings(project_id)
        settings_service.set_post_edit_sync(project_id, 0 if settings.get("post_edit_sync") else 1)
        await force_refresh_routes()
        await _render_auto(query, user_id, project_id)
        return True

    if action == "auto" and sub == "rtoggle":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        reaction = _auto_reaction(project_id)
        _set_reaction(project_id, enabled=0 if reaction.get("enabled") else 1)
        await force_refresh_routes()
        await _render_auto(query, user_id, project_id)
        return True

    if action == "auto" and sub == "remoji":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        _ask(user_id, "auto_emoji", project_id)
        await _send(query, "😀 Send the emoji to react with (e.g. <code>👍</code>):")
        return True

    if action == "auto" and sub == "rmode":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        reaction = _auto_reaction(project_id)
        nxt = "keywords" if (reaction.get("trigger_mode") or "all") == "all" else "all"
        _set_reaction(project_id, trigger_mode=nxt)
        await force_refresh_routes()
        await _render_auto(query, user_id, project_id)
        return True

    if action == "auto" and sub == "rkeywords":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        _ask(user_id, "auto_keywords", project_id)
        await _send(query, "🔑 Send comma-separated keywords; only matching messages get a reaction "
                           "(or <code>-</code> to react to everything):")
        return True

    if action == "auto" and sub == "rside":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        reaction = _auto_reaction(project_id)
        src, tgt = bool(reaction.get("apply_source")), bool(reaction.get("apply_target"))
        src, tgt = {
            (False, True): (True, False),
            (True, False): (True, True),
            (True, True): (False, True),
        }.get((src, tgt), (False, True))
        _set_reaction(project_id, apply_source=int(src), apply_target=int(tgt))
        await force_refresh_routes()
        await _render_auto(query, user_id, project_id)
        return True

    # ---------------- activity ----------------
    if action == "act" and sub == "clearconfirm":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        await _edit(query, "🗑 Delete this project's log history?",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("🗑 Yes, clear", callback_data=f"act:clearyes:{project_id}")],
                        [InlineKeyboardButton("Cancel", callback_data=f"act:view:{project_id}")],
                    ]))
        return True

    if action == "act" and sub == "clearyes":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        log_service.clear_logs(project_id)
        await _render_activity(query, user_id, project_id)
        return True

    # ---------------- dry run ----------------
    if action == "proj" and sub == "dryrun":
        project_id = int(parts[2])
        if not _owned(project_id, user_id):
            return True
        _ask(user_id, "dryrun", project_id)
        await _send(query,
                    "🧪 <b>Dry-Run Test</b>\n\n"
                    "Send a sample message exactly like one from your source channel.\n"
                    "I'll run the real filter + formatting chain and show you what "
                    "would be sent — <b>nothing is posted anywhere</b>.")
        return True

    # ---------------- rename ----------------
    if action == "rename":
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        _ask(user_id, "rename", project_id)
        await _send(query, "✏️ Send the new project name:")
        return True

    # ---------------- legacy source/target aliases ----------------
    # bot/keyboards.py builds an older set of source/destination screens.
    # They point at the same services as src:/tgt: - wire them so no button
    # on those screens is dead.
    if action in ("listsource", "source"):
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        if action == "source":
            return False  # "add source" text flow is owned by handlers_projects
        from services.source_service import get_sources
        sources = get_sources(project_id)
        rows = [[InlineKeyboardButton(
            f"{'🟢' if s['enabled'] else '🔴'} {s['title'] or s['chat_id']}",
            callback_data=f"sourceitem:{s['id']}:{project_id}")] for s in sources]
        rows.append([InlineKeyboardButton("➕ Add Source", callback_data=f"src:add:{project_id}")])
        rows.append([InlineKeyboardButton("◀️ Back to Project", callback_data=f"projcard:{project_id}")])
        await _edit(query, "📥 <b>Sources</b>", InlineKeyboardMarkup(rows))
        return True

    if action in ("listdestination", "destination"):
        project_id = int(parts[1])
        if not _owned(project_id, user_id):
            return True
        if action == "destination":
            return False
        dests = get_destinations(project_id)
        rows = [[InlineKeyboardButton(
            f"{'🟢' if d['enabled'] else '🔴'} {d['title'] or d['chat_id']}",
            callback_data=f"destitem:{d['id']}:{project_id}")] for d in dests]
        rows.append([InlineKeyboardButton("➕ Add Target", callback_data=f"tgt:add:{project_id}")])
        rows.append([InlineKeyboardButton("◀️ Back to Project", callback_data=f"projcard:{project_id}")])
        await _edit(query, "🎯 <b>Targets</b>", InlineKeyboardMarkup(rows))
        return True

    if action == "sourceitem":
        source_id, project_id = int(parts[1]), int(parts[2])
        if not _owned(project_id, user_id):
            return True
        s = get_source(source_id)
        if not s:
            return True
        await _edit(query,
                    "📥 <b>Source</b>\n\n"
                    f"• Title: <b>{_e(s['title'] or '-')}</b>\n"
                    f"• ID: <code>{_e(s['chat_id'])}</code>\n"
                    f"• Status: {_onoff(s['enabled'])}",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("⏸ Disable" if s["enabled"] else "▶ Enable",
                                              callback_data=f"togglesource:{source_id}:{project_id}")],
                        [InlineKeyboardButton("🗑 Remove", callback_data=f"deletesourceconfirm:{source_id}:{project_id}")],
                        [InlineKeyboardButton("◀️ Back", callback_data=f"listsource:{project_id}")],
                    ]))
        return True

    if action == "destitem":
        dest_id, project_id = int(parts[1]), int(parts[2])
        if not _owned(project_id, user_id):
            return True
        d = get_destination(dest_id)
        if not d:
            return True
        await _edit(query,
                    "🎯 <b>Target</b>\n\n"
                    f"• Title: <b>{_e(d['title'] or '-')}</b>\n"
                    f"• ID: <code>{_e(d['chat_id'])}</code>\n"
                    f"• Status: {_onoff(d['enabled'])}",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("🧪 Test", callback_data=f"tgt:test:{dest_id}:{project_id}")],
                        [InlineKeyboardButton("⏸ Disable" if d["enabled"] else "▶ Enable",
                                              callback_data=f"toggledestination:{dest_id}:{project_id}")],
                        [InlineKeyboardButton("🗑 Remove", callback_data=f"deletedestinationconfirm:{dest_id}:{project_id}")],
                        [InlineKeyboardButton("◀️ Back", callback_data=f"listdestination:{project_id}")],
                    ]))
        return True

    if action == "togglesource":
        source_id, project_id = int(parts[1]), int(parts[2])
        if not _owned(project_id, user_id):
            return True
        toggle_source_enabled(source_id)
        await force_refresh_routes()
        query.data = f"sourceitem:{source_id}:{project_id}"
        return await handle_callbacks(query, user_id, "sourceitem", ["sourceitem", str(source_id), str(project_id)], context)

    if action == "toggledestination":
        dest_id, project_id = int(parts[1]), int(parts[2])
        if not _owned(project_id, user_id):
            return True
        toggle_destination_enabled(dest_id)
        await force_refresh_routes()
        query.data = f"destitem:{dest_id}:{project_id}"
        return await handle_callbacks(query, user_id, "destitem", ["destitem", str(dest_id), str(project_id)], context)

    if action in ("deletesourceconfirm", "deletedestinationconfirm"):
        item_id, project_id = int(parts[1]), int(parts[2])
        if not _owned(project_id, user_id):
            return True
        is_source = action == "deletesourceconfirm"
        await _edit(query, "🗑 Remove this " + ("source?" if is_source else "target?"),
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("🗑 Yes, remove",
                                              callback_data=f"{'deletesource' if is_source else 'deletedestination'}:{item_id}:{project_id}")],
                        [InlineKeyboardButton("Cancel",
                                              callback_data=f"{'sourceitem' if is_source else 'destitem'}:{item_id}:{project_id}")],
                    ]))
        return True

    if action == "deletesource":
        source_id, project_id = int(parts[1]), int(parts[2])
        if not _owned(project_id, user_id):
            return True
        delete_source(source_id)
        await force_refresh_routes()
        query.data = f"listsource:{project_id}"
        return await handle_callbacks(query, user_id, "listsource", ["listsource", str(project_id)], context)

    if action == "deletedestination":
        dest_id, project_id = int(parts[1]), int(parts[2])
        if not _owned(project_id, user_id):
            return True
        delete_destination(dest_id)
        await force_refresh_routes()
        query.data = f"listdestination:{project_id}"
        return await handle_callbacks(query, user_id, "listdestination", ["listdestination", str(project_id)], context)

    if action == "testdestination":
        dest_id, project_id = int(parts[1]), int(parts[2])
        if not _owned(project_id, user_id):
            return True
        return False  # handled by handlers_projects via tgt:test aliasing below

    if action == "help":
        return False

    if action == "platform" and sub == "locked":
        await query.answer("This route is not available yet.", show_alert=True)
        return True

    if action == "noop":
        await query.answer()
        return True

    return False


# ==========================================
# SMALL PERSISTENCE HELPERS
# ==========================================

async def _adv_apply(user_id, project_id, destination_id, patch: dict):
    try:
        formatting_service.configure(user_id, project_id, patch, destination_id)
    except (PermissionError, ValueError) as exc:
        logger.warning("Advanced formatting rejected for project %s: %s", project_id, exc)
        raise
    await force_refresh_routes()


def _set_reaction(project_id: int, **fields):
    _auto_reaction(project_id)
    fields = {k: v for k, v in fields.items() if k in
              ("enabled", "emojis", "trigger_mode", "keywords", "apply_source", "apply_target")}
    if not fields:
        return
    conn = get_connection()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE auto_reactions SET {sets} WHERE project_id=?",
                     list(fields.values()) + [project_id])
        conn.commit()
    finally:
        conn.close()


# ==========================================
# TEXT INPUT DISPATCHER
# ==========================================

async def handle_text(message, user_id: int, text: str, context) -> bool:
    pending = PENDING_INPUT.get(user_id)
    if not pending:
        return False

    kind = pending["kind"]
    project_id = pending["project_id"]

    if not _owned(project_id, user_id):
        clear_pending(user_id)
        return True

    value = (text or "").strip()
    if len(value) > MAX_TEXT_INPUT:
        value = value[:MAX_TEXT_INPUT]

    try:
        if kind == "fmt_field":
            field = pending["field"]
            if value == "-":
                value = ""
            if field == "prefix":
                formatting_service.set_prefix(project_id, value)
            else:
                formatting_service.set_suffix(project_id, value)
            await message.reply_text(f"✅ {field.capitalize()} saved." if value else f"✅ {field.capitalize()} cleared.")
            await force_refresh_routes()

        elif kind == "fmt_replace_add":
            if "||" not in value:
                await message.reply_text("⚠️ Use the format <code>find || replace</code>. Try again.",
                                         parse_mode="HTML")
                return True
            find, _, replace = value.partition("||")
            find, replace = find.strip(), replace.strip()
            if not find:
                await message.reply_text("⚠️ The <code>find</code> part cannot be empty.", parse_mode="HTML")
                return True
            formatting_service.add_replace_rule(project_id, find, replace)
            await message.reply_text(f"✅ Replace rule added: <code>{_e(find)}</code> → <code>{_e(replace)}</code>",
                                     parse_mode="HTML")
            await force_refresh_routes()

        elif kind == "fmt_remove_add":
            try:
                formatting_service.add_remove_pattern(project_id, value)
            except ValueError as exc:
                await message.reply_text(f"⚠️ {_e(exc)}")
                return True
            await message.reply_text(f"✅ Remove pattern added: <code>{_e(value)}</code>", parse_mode="HTML")
            await force_refresh_routes()

        elif kind == "fmt_preview":
            output = formatting_service.preview(user_id, project_id, text)
            await message.reply_text(
                "👁 <b>Formatted output</b>\n\n<pre>" + _e(output) + "</pre>", parse_mode="HTML")

        elif kind == "adv_int":
            key = pending["key"]
            if not value.isdigit():
                await message.reply_text("⚠️ Send a whole number of 0 or more.")
                return True
            await _adv_apply(user_id, project_id, pending.get("destination_id"), {key: int(value)})
            await message.reply_text(f"✅ <code>{_e(key)}</code> set to {int(value)}.", parse_mode="HTML")

        elif kind == "adv_text":
            key = pending["key"]
            new_value = None if value == "-" else text
            await _adv_apply(user_id, project_id, pending.get("destination_id"), {key: new_value})
            await message.reply_text(
                f"✅ {_e(key)} " + ("cleared (inherits project default)." if new_value is None else "saved."),
                parse_mode="HTML")

        elif kind == "adv_preview":
            output = formatting_service.preview(user_id, project_id, text, pending.get("destination_id"))
            await message.reply_text(
                "👁 <b>Formatted output</b>\n\n<pre>" + _e(output) + "</pre>", parse_mode="HTML")

        elif kind == "wm_text":
            new_value = "" if value == "-" else text
            watermark_service.update_watermark_settings(project_id, text=new_value)
            await force_refresh_routes()
            await message.reply_text("✅ Watermark text saved." if new_value else "✅ Watermark text cleared.")

        elif kind == "ai_prompt":
            new_value = "" if value == "-" else text
            ai_service.update_ai_settings(project_id, custom_prompt=new_value)
            await force_refresh_routes()
            await message.reply_text("✅ Custom AI prompt saved." if new_value else "✅ Custom AI prompt cleared.")

        elif kind == "aff_id":
            new_value = "" if value == "-" else value
            affiliate_service.update_affiliate_settings(project_id, **{pending["field"]: new_value})
            await force_refresh_routes()
            await message.reply_text(f"✅ {_e(pending['provider'])} id saved." if new_value
                                     else f"✅ {_e(pending['provider'])} id cleared.", parse_mode="HTML")

        elif kind == "keywords":
            field = pending["field"]
            new_value = "" if value == "-" else value
            if field == "keyword_whitelist":
                settings_service.set_keyword_whitelist(project_id, new_value)
            else:
                settings_service.set_keyword_blacklist(project_id, new_value)
            await force_refresh_routes()
            await message.reply_text("✅ Keyword list saved." if new_value else "✅ Keyword list cleared.")

        elif kind == "regex":
            if value != "-":
                try:
                    re.compile(value)
                except re.error as exc:
                    await message.reply_text(f"⚠️ That regex is invalid: <code>{_e(exc)}</code>\nNothing was saved.",
                                             parse_mode="HTML")
                    return True
                settings_service.set_regex_filter(project_id, value)
                await message.reply_text("✅ Regex filter saved.")
            else:
                settings_service.set_regex_filter(project_id, "")
                await message.reply_text("✅ Regex filter cleared.")
            await force_refresh_routes()

        elif kind == "crfield":
            field = pending["field"]
            if field == "length":
                if value == "-":
                    content_rules_service.update_rules(project_id, min_length=None, max_length=None)
                    await message.reply_text("✅ Length filter cleared.")
                else:
                    bits = value.replace(",", " ").split()
                    if len(bits) != 2 or not all(b.isdigit() for b in bits):
                        await message.reply_text("⚠️ Send it as <code>min max</code>, e.g. <code>20 500</code>.",
                                                 parse_mode="HTML")
                        return True
                    low, high = int(bits[0]), int(bits[1])
                    if low > high:
                        low, high = high, low
                    content_rules_service.update_rules(project_id, min_length=low, max_length=high)
                    await message.reply_text(f"✅ Length window set to {low}–{high} characters.")
            else:
                new_value = "" if value == "-" else value
                content_rules_service.update_rules(project_id, **{field: new_value})
                await message.reply_text("✅ Saved." if new_value else "✅ Cleared.")
            await force_refresh_routes()

        elif kind == "delay":
            bits = value.replace(",", " ").split()
            if len(bits) != 2 or not all(_is_number(b) for b in bits):
                await message.reply_text("⚠️ Send it as <code>min max</code>, e.g. <code>2 6</code>.",
                                         parse_mode="HTML")
                return True
            settings_service.set_delay(project_id, float(bits[0]), float(bits[1]))
            await force_refresh_routes()
            await message.reply_text(f"✅ Delay window set to {bits[0]}–{bits[1]} seconds.")

        elif kind == "rename":
            if not value:
                await message.reply_text("⚠️ The name cannot be empty. Send it again:")
                return True
            rename_project(project_id, value[:100])
            await force_refresh_routes()
            await message.reply_text(f"✅ Project renamed to <b>{_e(value[:100])}</b>.", parse_mode="HTML")

        elif kind == "auto_emoji":
            if not value:
                await message.reply_text("⚠️ Send a single emoji:")
                return True
            _set_reaction(project_id, emojis=value[:8])
            await force_refresh_routes()
            await message.reply_text(f"✅ Reaction set to {_e(value[:8])}.", parse_mode="HTML")

        elif kind == "auto_keywords":
            new_value = "" if value == "-" else value
            _set_reaction(project_id, keywords=new_value)
            await force_refresh_routes()
            await message.reply_text("✅ Reaction keywords saved." if new_value
                                     else "✅ Reacting to every forwarded message.")

        elif kind == "dryrun":
            settings = settings_service.get_settings(project_id)
            rules = content_rules_service.get_rules(project_id)
            reaction = _auto_reaction(project_id)
            note = ""
            if reaction.get("enabled"):
                note = f"\n\n👍 Auto reaction <code>{_e(reaction.get('emojis') or '👍')}</code> would also be applied."
            await message.reply_text(
                _dry_run_report(project_id, text, settings, rules, note), parse_mode="HTML")

        else:
            clear_pending(user_id)
            return False

    except (PermissionError, ValueError) as exc:
        await message.reply_text(f"⚠️ {_e(exc)}", parse_mode="HTML")
        return True
    except Exception:
        logger.exception("Feature input failed (kind=%s, project=%s)", kind, project_id)
        await message.reply_text("⚠️ Could not save that. Please try again.")
        return True
    finally:
        clear_pending(user_id)

    return True


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False
