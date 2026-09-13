"""
ChannelFlow AI - Keyboards
============================

Navigation hierarchy (Prompt 3):

    MAIN MENU (reply keyboard)
    ├── 📁 Projects      -> project list -> project actions -> Edit submenus
    ├── 👤 Account       -> Plan & Billing / Wallet / Referrals / Connections
    ├── ⚙️ Settings      -> Language / Auto-Renew / Disconnect / System Status
    └── ❓ Help          -> Guide / Tour / Support / Feedback

Button-count rule: main menu 4-6 buttons, sections 3-8, project 4-8.
Contextual buttons only - a paused project shows Resume, an active one
shows Pause; a disconnected platform shows Reconnect.
"""

from telegram import (
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

import json

# ==========================================
# MAIN MENU (reply keyboard) - minimal
# Prompt 4 final requirement: exactly 4 primary actions
# ==========================================

main_menu = ReplyKeyboardMarkup(
    [
        ["📁 Projects", "💳 Subscription"],
        ["🎁 Rewards", "👤 Account"],
        ["🆘 Support", "⚙️ Settings"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


pre_login_menu = ReplyKeyboardMarkup(
    [
        ["🚀 Connect Now"],
        ["📖 Guide", "🧭 Tour"]
    ],
    resize_keyboard=True,
    is_persistent=True
)


# ==========================================
# PROJECTS SECTION
# ==========================================

def projects_section_keyboard(projects, can_create=True):
    """One tap on 📁 Projects opens this: every project as a row plus a
    single ➕ New Project action."""

    rows = []

    for p in projects:
        status_icon = "🟢" if p["status"] else "⚪"
        rows.append([
            InlineKeyboardButton(
                f"{status_icon} {p['name']}",
                callback_data=f"projcard:{p['id']}"
            )
        ])

    if can_create:
        rows.append([InlineKeyboardButton("➕ New Project", callback_data="newproj")])

    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="nav:home")])

    return InlineKeyboardMarkup(rows)


def project_actions_keyboard(project_id, running=None, platform_type=None):
    """Compact per-project view (Prompt 3 section 5): edit/status/
    start-pause/delete + back. Contextual start/pause via ``running``."""

    if running is True:
        toggle_row = [InlineKeyboardButton("⏸ Pause", callback_data=f"stop:{project_id}")]
    elif running is False:
        toggle_row = [InlineKeyboardButton("▶️ Start", callback_data=f"start:{project_id}")]
    else:
        toggle_row = [
            InlineKeyboardButton("▶️ Start", callback_data=f"start:{project_id}"),
            InlineKeyboardButton("⏸ Pause", callback_data=f"stop:{project_id}"),
        ]

    instagram_row = (
        [InlineKeyboardButton("📸 Instagram", callback_data=f"instagram:{project_id}")]
        if platform_type in ("instagram_broadcast", "both")
        else None
    )

    rows = [
        [
            InlineKeyboardButton("⚙️ Edit Project", callback_data=f"editproj:{project_id}"),
            InlineKeyboardButton("📊 Status", callback_data=f"stats:{project_id}"),
        ],
        toggle_row,
        [
            InlineKeyboardButton("🗑 Delete", callback_data=f"deleteconfirm:{project_id}"),
        ],
    ]

    if instagram_row:
        rows.append(instagram_row)

    rows.append([
        InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
        InlineKeyboardButton("⬅ Back to Projects", callback_data="nav:projects"),
    ])

    return InlineKeyboardMarkup(rows)


def edit_project_keyboard(project_id, platform_type=None):
    """Categorized project management (Prompt 3 section 6). Each row is
    its own submenu; nothing else leaks onto this screen."""

    rows = [
        [
            InlineKeyboardButton("📥 Sources", callback_data=f"listsource:{project_id}"),
            InlineKeyboardButton("📤 Destinations", callback_data=f"listdestination:{project_id}"),
        ],
        [
            InlineKeyboardButton("🔍 Content Filters", callback_data=f"projfilters:{project_id}"),
            InlineKeyboardButton("🔁 Forwarding Rules", callback_data=f"projsettings:{project_id}"),
        ],
        [
            InlineKeyboardButton("📝 Formatting", callback_data=f"fmtroot:{project_id}"),
            InlineKeyboardButton("⏱ Delay", callback_data=f"setdelay:{project_id}"),
        ],
        [
            InlineKeyboardButton("📊 Analytics", callback_data=f"stats:{project_id}"),
        ],
    ]

    if platform_type in ("instagram_broadcast", "both"):
        rows.append([InlineKeyboardButton("📸 Instagram Pipeline", callback_data=f"instagram:{project_id}")])

    rows += [
        [
            InlineKeyboardButton("✏ Rename", callback_data=f"rename:{project_id}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"deleteconfirm:{project_id}"),
        ],
        [
            InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
            InlineKeyboardButton("⬅ Back to Project", callback_data=f"projcard:{project_id}"),
        ],
    ]

    return InlineKeyboardMarkup(rows)


# ==========================================
# AI SETTINGS KEYBOARDS
# ==========================================

def ai_settings_keyboard(project_id, settings):
    """🤖 AI & Rewriting hub (Prompt 3 section 12)."""

    enabled = settings.get("enabled")
    status_icon = "🟢 ON" if enabled else "🔴 OFF"

    tone_labels = {
        "original": "Original", "casual": "Casual", "professional": "Professional",
        "promotional": "Promotional", "funny": "Funny",
    }
    length_labels = {"keep": "Keep", "shorten": "Shorten", "expand": "Expand"}

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"AI Status: {status_icon}", callback_data=f"aitoggle:{project_id}")],
        [InlineKeyboardButton(f"✍️ Tone: {tone_labels.get(settings.get('tone', 'original'), 'Original')}", callback_data=f"aicycletone:{project_id}")],
        [InlineKeyboardButton(f"📏 Length: {length_labels.get(settings.get('length_mode', 'keep'), 'Keep')}", callback_data=f"aicyclelength:{project_id}")],
        [InlineKeyboardButton(f"🌍 Translate: {settings.get('language') or 'Off'}", callback_data=f"aicyclelang:{project_id}")],
        [InlineKeyboardButton(f"🔗 Preserve URLs: {'On' if settings.get('preserve_urls') else 'Off'}", callback_data=f"aitoggleurls:{project_id}")],
        [InlineKeyboardButton(f"#️⃣ Preserve Hashtags: {'On' if settings.get('preserve_hashtags') else 'Off'}", callback_data=f"aitogglehashtags:{project_id}")],
        [InlineKeyboardButton(f"📢 Add CTA: {'On' if settings.get('add_cta') else 'Off'}", callback_data=f"aitogglecta:{project_id}")],
        [InlineKeyboardButton(f"🧹 Anti-Spam Clean: {'On' if settings.get('remove_spam') else 'Off'}", callback_data=f"aitogglespam:{project_id}")],
        [InlineKeyboardButton("📝 Custom Prompt", callback_data=f"aiprompt:{project_id}")],
        [InlineKeyboardButton("⬅ Back to Edit", callback_data=f"editproj:{project_id}")],
    ])


TONES = ("original", "casual", "professional", "promotional", "funny")
LENGTH_MODES = ("keep", "shorten", "expand")
LANGUAGES_AI = (None, "English", "Hindi", "Bengali", "Urdu", "Spanish", "Arabic", "Indonesian")


# ==========================================
# WATERMARK KEYBOARDS
# ==========================================

def watermark_settings_keyboard(project_id, settings):
    """🖼 Watermark hub (Prompt 3 section 13)."""

    enabled = settings.get("enabled")
    status_icon = "🟢 ON" if enabled else "🔴 OFF"
    wm_type = settings.get("type", "text")

    positions = ("top-left", "top-right", "bottom-left", "bottom-right", "center")

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Watermark: {status_icon}", callback_data=f"wmtoggle:{project_id}")],
        [InlineKeyboardButton(f"🏷 Type: {wm_type.capitalize()}", callback_data=f"wmcycletype:{project_id}")],
        [InlineKeyboardButton("✍️ Set Text", callback_data=f"wmsettext:{project_id}")],
        [InlineKeyboardButton(f"📍 Position: {settings.get('position', 'bottom-right')}", callback_data=f"wmcyclepos:{project_id}")],
        [InlineKeyboardButton(f"🔍 Size: {settings.get('font_size', 24)}px", callback_data=f"wmcyclesize:{project_id}")],
        [InlineKeyboardButton(f"🌫 Opacity: {int(float(settings.get('opacity', 0.7)) * 100)}%", callback_data=f"wmcycleopacity:{project_id}")],
        [InlineKeyboardButton("⬅ Back to Edit", callback_data=f"editproj:{project_id}")],
    ])


# ==========================================
# AFFILIATE SETTINGS KEYBOARD
# ==========================================

def affiliate_settings_keyboard(project_id, settings):
    """🛍 Affiliate Links hub - per-project affiliate configuration."""

    enabled = settings.get("enabled")
    status_icon = "🟢 ON" if enabled else "🔴 OFF"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Affiliate Tools: {status_icon}", callback_data=f"afftogglemain:{project_id}")],
        [InlineKeyboardButton(f"📦 Amazon {'🟡 On' if settings.get('amazon_enabled') else '🔴 Off'}", callback_data=f"afftoggleamazon:{project_id}")],
        [InlineKeyboardButton(f"🛒 Flipkart {'🟡 On' if settings.get('flipkart_enabled') else '🔴 Off'}", callback_data=f"afftoggleflipkart:{project_id}")],
        [InlineKeyboardButton(f"🧘 Meesho {'🟡 On' if settings.get('meesho_enabled') else '🔴 Off'}", callback_data=f"afftogglemmeesho:{project_id}")],
        [InlineKeyboardButton(f"🌈 Wishlink {'🟡 On' if settings.get('wishlink_enabled') else '🔴 Off'}", callback_data=f"afftogglewishlink:{project_id}")],
        [InlineKeyboardButton(f"💰 EarnKaro {'🟡 On' if settings.get('earnkaro_enabled') else '🔴 Off'}", callback_data=f"afftogglevarearnkaro:{project_id}")],
        [InlineKeyboardButton("⬅ Back to Edit", callback_data=f"editproj:{project_id}")],
    ])


# ==========================================
# SOURCES / DESTINATIONS
# ==========================================


# ==========================================
# SOURCES / DESTINATIONS
# ==========================================

def sources_list_keyboard(project_id, sources, can_add=True):
    rows = []

    for s in sources:
        icon = "🟢" if s["enabled"] else "🔴"
        label = f"{icon} {s['username'] or s['title'] or s['chat_id']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"sourceitem:{s['id']}:{project_id}")])

    if can_add:
        rows.append([InlineKeyboardButton("➕ Add Source", callback_data=f"source:{project_id}")])

    rows.append([
        InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
        InlineKeyboardButton("⬅ Back to Edit", callback_data=f"editproj:{project_id}"),
    ])

    return InlineKeyboardMarkup(rows)


def destinations_list_keyboard(project_id, destinations, can_add=True):
    rows = []

    for d in destinations:
        icon = "🟢" if d["enabled"] else "🔴"
        label = f"{icon} {d['username'] or d['title'] or d['chat_id']}"
        # Show pairing/verification status for WhatsApp/Threads destinations
        if d["chat_type"] in ("whatsapp_channel", "threads"):
            status = d.get("pairing_status") or "pending"
            status_icon = {
                "pending": "🟡",
                "verified": "🟢",
                "expired": "⚠️",
            }.get(status, "🟡")
            code = d.get("pairing_code") or ""
            label += f" {status_icon}{(' ' + code) if code else ''}"
        rows.append([InlineKeyboardButton(label, callback_data=f"destitem:{d['id']}:{project_id}")])

    if can_add:
        rows.append([InlineKeyboardButton("➕ Add Destination", callback_data=f"destination:{project_id}")])

    rows.append([
        InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
        InlineKeyboardButton("⬅ Back to Edit", callback_data=f"editproj:{project_id}"),
    ])

    return InlineKeyboardMarkup(rows)


def source_item_keyboard(source_id, project_id, enabled=True):

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏸ Disable" if enabled else "▶ Enable",
                    callback_data=f"togglesource:{source_id}:{project_id}"
                ),
                InlineKeyboardButton(
                    "🔌 Disconnect",
                    callback_data=f"deletesourceconfirm:{source_id}:{project_id}"
                )
            ],
            [
                InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                InlineKeyboardButton("⬅ Back to Sources", callback_data=f"listsource:{project_id}")
            ]
        ]
    )


def destination_item_keyboard(destination_id, project_id, enabled=True):

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏸ Disable" if enabled else "▶ Enable",
                    callback_data=f"toggledestination:{destination_id}:{project_id}"
                ),
                InlineKeyboardButton(
                    "🔌 Disconnect",
                    callback_data=f"deletedestinationconfirm:{destination_id}:{project_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🧪 Test",
                    callback_data=f"testdestination:{destination_id}:{project_id}"
                )
            ],
            [
                InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                InlineKeyboardButton("⬅ Back to Destinations", callback_data=f"listdestination:{project_id}")
            ]
        ]
    )


def delete_confirm_keyboard(confirm_action, back_action):
    """Generic destructive-action confirmation (Prompt 3 section 46)."""

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Yes, delete", callback_data=confirm_action)],
        [InlineKeyboardButton("Cancel", callback_data=back_action)],
    ])


# ==========================================
# FORWARD SETTINGS / FILTERS / FORMATTING
# ==========================================

def project_settings_keyboard(project_id, settings):

    mode = settings["mode"]
    silent = bool(settings["silent"])
    protect = bool(settings["protect_content"])
    albums = bool(settings["keep_media_groups"])

    return InlineKeyboardMarkup(

        [

            [
                InlineKeyboardButton(
                    f"Mode: {'📨 Forward' if mode == 'forward' else '📝 Copy'}",
                    callback_data=f"togglemode:{project_id}"
                )
            ],

            [
                InlineKeyboardButton(
                    f"🔕 Silent: {'On' if silent else 'Off'}",
                    callback_data=f"togglesilent:{project_id}"
                ),
                InlineKeyboardButton(
                    f"🛡 Protect: {'On' if protect else 'Off'}",
                    callback_data=f"toggleprotect:{project_id}"
                )
            ],

            [
                InlineKeyboardButton(
                    f"🖼 Albums: {'On' if albums else 'Off'}",
                    callback_data=f"togglealbum:{project_id}"
                )
            ],

            [
                InlineKeyboardButton(
                    "⬅ Back to Edit",
                    callback_data=f"editproj:{project_id}"
                )
            ]

        ]

    )


MEDIA_FILTER_CHOICES = (
    "all", "text", "photo", "video", "audio",
    "document", "voice", "sticker", "poll", "animation",
)


def project_filters_keyboard(project_id, settings, content_rules=None):
    """Content Filters submenu (Prompt 3 section 11): categories first,
    each opening its own screen - never one wall of filter inputs."""

    rows = [
        [
            InlineKeyboardButton("🔑 Keywords", callback_data=f"filterkw:{project_id}"),
            InlineKeyboardButton("#️⃣ Hashtags", callback_data=f"crfield:{project_id}:hashtag_filter"),
        ],
        [
            InlineKeyboardButton("🌐 Domains", callback_data=f"filterdomains:{project_id}"),
            InlineKeyboardButton("👤 Senders", callback_data=f"filtersenders:{project_id}"),
        ],
        [
            InlineKeyboardButton("📏 Length", callback_data=f"crfield:{project_id}:length"),
            InlineKeyboardButton("🔤 Regex", callback_data=f"setregex:{project_id}"),
        ],
        [
            InlineKeyboardButton(
                f"🎛 Media Type: {settings['media_filter']}",
                callback_data=f"mediafilter:{project_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🧹 Clear All Filters",
                callback_data=f"clearfiltersconfirm:{project_id}"
            )
        ],
        [
            InlineKeyboardButton("⬅ Back to Edit", callback_data=f"editproj:{project_id}")
        ],
    ]

    return InlineKeyboardMarkup(rows)


def keyword_filter_keyboard(project_id, settings):
    wl = settings["keyword_whitelist"] or ""
    bl = settings["keyword_blacklist"] or ""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Required ({len(wl.split(',')) if wl else 0})", callback_data=f"setwhitelist:{project_id}")],
        [InlineKeyboardButton(f"🚫 Blocked ({len(bl.split(',')) if bl else 0})", callback_data=f"setblacklist:{project_id}")],
        [InlineKeyboardButton("⬅ Back to Filters", callback_data=f"projfilters:{project_id}")],
    ])


def domain_filter_keyboard(project_id, content_rules):
    dwl = content_rules["domain_whitelist"] or ""
    dbl = content_rules["domain_blacklist"] or ""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌐 Whitelist ({len(dwl.split(',')) if dwl else 0})", callback_data=f"crfield:{project_id}:domain_whitelist")],
        [InlineKeyboardButton(f"⛔ Blacklist ({len(dbl.split(',')) if dbl else 0})", callback_data=f"crfield:{project_id}:domain_blacklist")],
        [InlineKeyboardButton("⬅ Back to Filters", callback_data=f"projfilters:{project_id}")],
    ])


def sender_filter_keyboard(project_id, content_rules):
    swl = content_rules["sender_whitelist"] or ""
    sbl = content_rules["sender_blacklist"] or ""
    logic = content_rules["filter_logic"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"👤 Allow ({len(swl.split(',')) if swl else 0})", callback_data=f"crfield:{project_id}:sender_whitelist")],
        [InlineKeyboardButton(f"🚫 Block ({len(sbl.split(',')) if sbl else 0})", callback_data=f"crfield:{project_id}:sender_blacklist")],
        [InlineKeyboardButton(f"🔁 Mode: {logic.upper()}", callback_data=f"crlogic:{project_id}")],
        [InlineKeyboardButton("⬅ Back to Filters", callback_data=f"projfilters:{project_id}")],
    ])


def media_filter_choice_keyboard(project_id):

    rows = []
    row = []

    for choice in MEDIA_FILTER_CHOICES:

        row.append(
            InlineKeyboardButton(
                choice,
                callback_data=f"mediafilterset:{project_id}:{choice}"
            )
        )

        if len(row) == 3:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append([
        InlineKeyboardButton(
            "⬅ Back",
            callback_data=f"projfilters:{project_id}"
        )
    ])

    return InlineKeyboardMarkup(rows)


def formatting_root_keyboard(project_id):
    """Formatting hub (Prompt 3 section 6 style): text transforms and
    cleanup live here, not on the edit root."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Prefix / ➖ Suffix", callback_data=f"fmtprefixsuffix:{project_id}")],
        [InlineKeyboardButton("🔁 Replace Rules", callback_data=f"fmtreplace:{project_id}")],
        [InlineKeyboardButton("🧹 Remove Patterns", callback_data=f"fmtremove:{project_id}")],
        [InlineKeyboardButton("⬅ Back to Edit", callback_data=f"editproj:{project_id}")],
    ])


def formatting_keyboard(project_id, rules):

    prefix_label = f"➕ Prefix: {rules['prefix'][:20]}" if rules["prefix"] else "➕ Set Prefix"
    suffix_label = f"➖ Suffix: {rules['suffix'][:20]}" if rules["suffix"] else "➖ Set Suffix"

    replace_count = len(json.loads(rules["replace_rules"] or "[]"))
    remove_count = len(json.loads(rules["remove_patterns"] or "[]"))

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Advanced cleanup / preview",callback_data=f"advancedformat:{project_id}")],

        [InlineKeyboardButton(prefix_label, callback_data=f"fmtfield:{project_id}:prefix")],
        [InlineKeyboardButton(suffix_label, callback_data=f"fmtfield:{project_id}:suffix")],

        [InlineKeyboardButton(
            f"🔁 Replace Rules ({replace_count})", callback_data=f"fmtreplace:{project_id}"
        )],
        [InlineKeyboardButton(
            f"🧹 Remove Patterns ({remove_count})", callback_data=f"fmtremove:{project_id}"
        )],
        [InlineKeyboardButton("⬅ Back to Formatting", callback_data=f"editproj:{project_id}")],
    ])


def formatting_replace_list_keyboard(project_id, rules_list):

    rows = []

    for i, rule in enumerate(rules_list):
        label = f"❌ {rule['find'][:15]} → {rule['replace'][:15]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"fmtreplacedel:{project_id}:{i}")])

    rows.append([InlineKeyboardButton("➕ Add Replace Rule", callback_data=f"fmtreplaceadd:{project_id}")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"fmtroot:{project_id}")])

    return InlineKeyboardMarkup(rows)


def formatting_remove_list_keyboard(project_id, patterns):

    rows = []

    for i, pattern in enumerate(patterns):
        rows.append([InlineKeyboardButton(f"❌ {pattern[:30]}", callback_data=f"fmtremovedel:{project_id}:{i}")])

    rows.append([InlineKeyboardButton("➕ Add Remove Pattern", callback_data=f"fmtremoveadd:{project_id}")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"fmtroot:{project_id}")])

    return InlineKeyboardMarkup(rows)


# ==========================================
# ACCOUNT SECTION
# ==========================================

def account_section_keyboard(connected=True):
    """👤 Account hub (Prompt 3 section 37). Only features that exist."""

    rows = [
        [InlineKeyboardButton("💳 Plan & Billing", callback_data="acct:plan")],
        [InlineKeyboardButton("💰 Wallet", callback_data="acct:wallet")],
        [InlineKeyboardButton("👥 Referrals", callback_data="acct:earn")],
    ]

    if connected:
        rows.append([InlineKeyboardButton("🔗 Connected Accounts", callback_data="acct:connections")])

    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="nav:home")])

    return InlineKeyboardMarkup(rows)


def plan_billing_keyboard(current_plan, auto_renew_enabled=False):
    """💳 Plan & Billing card actions."""

    renew_label = "🔁 Auto-Renew: ON" if auto_renew_enabled else "🔁 Auto-Renew: OFF"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆️ Upgrade Plan", callback_data="pay:method")],
        [
            InlineKeyboardButton("💰 Wallet", callback_data="acct:wallet"),
            InlineKeyboardButton("🧾 Payments", callback_data="acct:payhistory"),
        ],
        [InlineKeyboardButton(renew_label, callback_data="settings:togglerenew")],
        [InlineKeyboardButton("⬅ Back to Account", callback_data="nav:account")],
    ])


def wallet_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Balance", callback_data="wallet:topup")],
        [InlineKeyboardButton("🎟 Redeem Coupon", callback_data="wallet:redeem")],
        [InlineKeyboardButton("🧾 Transactions", callback_data="acct:wallethistory")],
        [InlineKeyboardButton("⬅ Back to Account", callback_data="nav:account")],
    ])


def connections_keyboard(connected_phone):
    phone = connected_phone or "None"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✈️ Telegram: {phone}", callback_data="acct:noop")],
        [InlineKeyboardButton("🔌 Disconnect Telegram Session", callback_data="settings:disconnect")],
        [InlineKeyboardButton("🔗 Manage Platform Accounts", callback_data="pacct:list")],
        [InlineKeyboardButton("⬅ Back to Account", callback_data="nav:account")],
    ])


def platform_accounts_keyboard(accounts):
    """Unified Connected Accounts screen (Prompt 2 §25 Connection Center)."""

    rows = []
    by_platform = {}
    for a in accounts:
        by_platform.setdefault(a["platform"], []).append(a)

    icons = {"telegram": "✈️", "whatsapp_channel": "🟢", "threads": "🧵"}
    health_icons = {"healthy": "🟢", "warning": "🟠", "error": "🔴", None: "⚪"}

    for platform in ("whatsapp_channel", "threads"):
        plist = by_platform.get(platform, [])
        icon = icons.get(platform, "•")

        if not plist:
            label = f"{icon} {'WhatsApp' if 'whatsapp' in platform else platform.capitalize()}: Not connected"
            cb = "pacct:wa_start" if platform == "whatsapp_channel" else "pacct:th_start"
            rows.append([InlineKeyboardButton(label, callback_data=cb)])
        else:
            # Show pairing code option for already-linked WhatsApp accounts
            rows.append(
                [InlineKeyboardButton(
                    f"🔗 Pair via Code ({platform})",
                    callback_data=f"wacode:list:{platform}",
                )]
            )
            for a in plist:
                h = health_icons.get(a["health_status"], "⚪")
                ident = str(a["account_identifier"])[:18]
                rows.append([InlineKeyboardButton(
                    f"{icon} {ident} {h}",
                    callback_data=f"pacct:view:{a['id']}"
                )])

    rows.append([InlineKeyboardButton("➕ Connect New Account", callback_data="pacct:new")])
    rows.append([InlineKeyboardButton("⬅ Back to Account", callback_data="nav:account")])

    return InlineKeyboardMarkup(rows)


def platform_account_detail_keyboard(account_id, health_status):
    rows = []
    if health_status in ("error", "warning"):
        rows.append([InlineKeyboardButton("🔄 Reconnect / Refresh", callback_data=f"pacct:reconnect:{account_id}")])
    rows.append([InlineKeyboardButton("🔌 Disconnect", callback_data=f"pacct:dconfirm:{account_id}")])
    rows.append([InlineKeyboardButton("⬅ My Accounts", callback_data="pacct:list")])
    return InlineKeyboardMarkup(rows)


def payment_history_keyboard(payments):
    """Last few payment requests as read-only rows."""
    rows = []
    for r in payments:
        amount = r["final_amount"] if r["final_amount"] is not None else (r["amount_inr"] or 0)
        currency = r["currency"] or "INR"
        symbol = "$" if currency == "USD" else "₹"
        plan_label = r["plan"] or "Wallet top-up"
        icon = {"APPROVED": "✅", "REJECTED": "❌", "SUBMITTED": "⏳", "PENDING_PAYMENT": "🕐"}.get(r["status"], "•")
        rows.append([InlineKeyboardButton(f"{icon} {plan_label} {symbol}{amount:.0f}", callback_data="noop")])
    rows.append([InlineKeyboardButton("⬅ Back to Billing", callback_data="acct:plan")])
    return InlineKeyboardMarkup(rows)


def referral_keyboard(bot_username, stats):
    link = f"https://t.me/{bot_username}?start=ref_placeholder"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share Link", url=link)],
        [InlineKeyboardButton("⬅ Back to Account", callback_data="nav:account")],
    ])


# ==========================================
# SETTINGS SECTION
# ==========================================

def settings_section_keyboard(auto_renew_enabled=False):
    """⚙️ Settings hub (Prompt 3 section 17/49)."""

    renew_label = "🔁 Auto-Renew: ON" if auto_renew_enabled else "🔁 Auto-Renew: OFF"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Account", callback_data="nav:account")],
        [InlineKeyboardButton("💳 Plan & Billing", callback_data="acct:plan")],
        [InlineKeyboardButton("🔗 Connected Accounts", callback_data="acct:connections")],
        [InlineKeyboardButton("🌐 Language", callback_data="settings:language")],
        [InlineKeyboardButton(renew_label, callback_data="settings:togglerenew")],
        [InlineKeyboardButton("🔔 Notifications", callback_data="settings:notifications")],
        [InlineKeyboardButton("📊 System Status", callback_data="settings:systatus")],
        [InlineKeyboardButton("🛟 Support", callback_data="nav:help")],
        [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
    ])


def settings_keyboard(auto_renew_enabled=False):
    """Canonical Settings keyboard. BUG-001 fix: this function was
    dropped during the Prompt-3 rewrite while handlers.py still imported
    it, which crashed the entire handlers module and killed /start.
    Now delegates to the section hub - ONE canonical settings screen."""

    return settings_section_keyboard(auto_renew_enabled)


LANGUAGE_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("English 🇬🇧", callback_data="lang:en")],
    [InlineKeyboardButton("हिंदी 🇮🇳", callback_data="lang:hi")],
    [InlineKeyboardButton("বাংলা 🇧🇩", callback_data="lang:bn")],
    [InlineKeyboardButton("اردو 🇵🇰", callback_data="lang:ur")],
    [InlineKeyboardButton("Español 🇪🇸", callback_data="lang:es")],
    [InlineKeyboardButton("العربية 🇸🇦", callback_data="lang:ar")],
    [InlineKeyboardButton("Bahasa Indonesia 🇮🇩", callback_data="lang:id")],
    [InlineKeyboardButton("⬅ Back to Settings", callback_data="nav:settings")],
])


# ==========================================
# HELP SECTION
# ==========================================

def support_section_keyboard(user_id: int = None, is_creator: bool = False):
    """Dynamic Support section with AI Chatbot, Support Group, and Creator-only Owner contact."""
    rows = [
        [InlineKeyboardButton("🤖 AI Chatbot", callback_data="support:ai")],
        [InlineKeyboardButton("Support Team", callback_data="support:group")],
    ]
    
    if is_creator:
        rows.append([InlineKeyboardButton("👑 Contact Owner", callback_data="support:owner")])
    
    rows.extend([
        [InlineKeyboardButton("🔎 FAQ", callback_data="help:faq")],
        [InlineKeyboardButton("📖 Guide", callback_data="help:guide")],
        [InlineKeyboardButton("🎯 Bot Tour", callback_data="help:tour")],
        [InlineKeyboardButton("🎫 My Support Tickets", callback_data="support:list")],
        [InlineKeyboardButton("💬 New Support Ticket", callback_data="support:new")],
        [InlineKeyboardButton("💡 Feature Request", callback_data="help:feedback")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="nav:home")],
    ])
    return InlineKeyboardMarkup(rows)

# Legacy static keyboard for backward compatibility
help_section_keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("🤖 AI Chatbot", callback_data="support:ai")],
    [InlineKeyboardButton("Support Team", callback_data="support:group")],
    [InlineKeyboardButton("🔎 FAQ", callback_data="help:faq")],
    [InlineKeyboardButton("📖 Guide", callback_data="help:guide")],
    [InlineKeyboardButton("🎯 Bot Tour", callback_data="help:tour")],
    [InlineKeyboardButton("🎫 My Support Tickets", callback_data="support:list")],
    [InlineKeyboardButton("💬 New Support Ticket", callback_data="support:new")],
    [InlineKeyboardButton("💡 Feature Request", callback_data="help:feedback")],
    [InlineKeyboardButton("🏠 Main Menu", callback_data="nav:home")],
])


# ==========================================
# SUPPORT TICKETS (user side)
# ==========================================

SUPPORT_CATEGORIES = (
    ("faq", "❓ FAQ"),
    ("tickets", "🎫 My Tickets"),
    ("new_ticket", "➕ New Ticket"),
    ("updates", "📣 Updates"),
    ("feature", "💡 Feature Request"),
)


def support_category_keyboard():
    rows = []
    for cat, label in SUPPORT_CATEGORIES:
        rows.append([InlineKeyboardButton(label, callback_data=f"support:cat:{cat}")])
    rows.append([InlineKeyboardButton("⬅ Back to Help", callback_data="nav:help")])
    return InlineKeyboardMarkup(rows)


def support_ticket_keyboard(ticket_id, status):
    rows = []
    if status not in ("closed",):
        rows.append([InlineKeyboardButton("✉️ Reply", callback_data=f"support:reply:{ticket_id}")])
        rows.append([InlineKeyboardButton("✅ Close Ticket", callback_data=f"support:close:{ticket_id}")])
    else:
        rows.append([InlineKeyboardButton("🔄 Reopen", callback_data=f"support:reopen:{ticket_id}")])
    rows.append([InlineKeyboardButton("⬅ My Tickets", callback_data="support:list")])
    return InlineKeyboardMarkup(rows)


# ==========================================
# PAYMENT FLOW (method-first)
# ==========================================

def payment_method_keyboard(crypto_available=True, upi_available=True):
    """⬆️ Upgrade starts HERE - method selection before any pricing
    (Prompt 3 section 20)."""

    rows = []
    if upi_available:
        rows.append([InlineKeyboardButton("🇮🇳 UPI", callback_data="pay:plans:upi")])
    if crypto_available:
        rows.append([InlineKeyboardButton("₿ Crypto", callback_data="pay:plans:crypto")])
    rows.append([InlineKeyboardButton("⭐ Telegram Stars", callback_data="pay:plans:stars")])

    if not rows:
        rows.append([InlineKeyboardButton("⚠ No payment methods configured", callback_data="noop")])

    rows.append([InlineKeyboardButton("⬅ Back", callback_data="acct:plan")])
    return InlineKeyboardMarkup(rows)


def plan_choice_keyboard(method):
    """Plan picker for the chosen method - INR labels for UPI, USD for
    crypto (separate price books, never converted), Stars ⭐ for
    Telegram Stars payments."""

    from services import plan_service

    rows = []

    icons = {"BEGINNER": "🌱", "PRO": "🚀", "CREATOR": "👑"}

    for plan in ("BEGINNER", "PRO", "CREATOR"):
        icon = icons[plan]

        if method == "crypto":
            usd = plan_service.get_plan_crypto_price_usd(plan)
            label = f"{icon} {plan.capitalize()} — ${usd:.2f}/mo"
        elif method == "stars":
            stars_price = plan_service.get_plan_stars_price(plan)
            label = f"{icon} {plan.capitalize()} — ⭐{stars_price}"
        else:
            inr = plan_service.get_plan_monthly_price_inr(plan)
            label = f"{icon} {plan.capitalize()} — ₹{inr:.0f}/mo"

        rows.append([InlineKeyboardButton(label, callback_data=f"pay:durations:{method}:{plan}")])

    rows.append([InlineKeyboardButton("⬅ Back", callback_data="pay:method")])
    return InlineKeyboardMarkup(rows)


def duration_choice_keyboard(method, plan):
    """Duration picker showing the FINAL price for THIS method -
    discounts come from the method's own price book."""

    from services import plan_service, pricing_service

    durations = pricing_service.get_duration_options(plan)

    rows = []
    for d in durations:
        months = d["months"]

        if method == "crypto":
            cp = pricing_service.calculate_crypto_price(plan, months, pricing_service.get_crypto_discount_for_duration(d))
            label = pricing_service.format_crypto_label(cp)
        elif method == "stars":
            sp = plan_service.get_plan_stars_price(plan)
            label = f"⭐ {sp * months}/mo"
        else:
            ip = pricing_service.calculate_price(plan, months, d["discount_percent"], d["price_inr_override"])
            label = pricing_service.format_duration_label(ip)

        rows.append([InlineKeyboardButton(label, callback_data=f"pay:create:{method}:{plan}:{months}")])

    rows.append([InlineKeyboardButton("⬅ Change Plan", callback_data=f"pay:plans:{method}")])
    return InlineKeyboardMarkup(rows)


def crypto_payment_keyboard(payment_url, request_id):
    """Live crypto invoice card (Prompt 3 section 27). Pay Now opens
    the REAL provider URL; Check Status polls server-side."""

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Pay Now", url=payment_url)],
        [
            InlineKeyboardButton("🔄 Check Status", callback_data=f"pay:status:{request_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"pay:cancel:{request_id}"),
        ],
    ])


def upi_payment_keyboard(request_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Verify", callback_data=f"upgrade:verify:{request_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"upgrade:cancel:{request_id}")],
    ])


def payment_success_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 Open Projects", callback_data="nav:projects")],
        [InlineKeyboardButton("💳 View Plan", callback_data="acct:plan")],
    ])


def payment_failed_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Try Again", callback_data="pay:method")],
        [InlineKeyboardButton("🆘 Support", callback_data="help:support")],
    ])


# ==========================================
# ADMIN KEYBOARD
# ==========================================

admin_keyboard = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="admin:refresh")],
        [
            InlineKeyboardButton("💳 Payments", callback_data="admin:payments"),
            InlineKeyboardButton("📢 Broadcast", callback_data="admin:broadcast"),
        ],
        [InlineKeyboardButton("🛠 Maintenance Mode", callback_data="admin:maintenance")],
    ]
)

# ==========================================
# PROJECT PLATFORM SELECTION (new project flow)
# ==========================================

def platform_selection_keyboard():

    from services.platform_registry import is_selectable, get_platform

    rows = [[InlineKeyboardButton("📡 Telegram → Telegram", callback_data="platform:telegram")]]

    # WhatsApp - check if available
    if is_selectable("whatsapp_channel"):
        rows.append([InlineKeyboardButton("🟢 Telegram → WhatsApp Channel", callback_data="platform:whatsapp_channel")])
    else:
        rows.append([InlineKeyboardButton("🟢 Telegram → WhatsApp Channel (Setup Required)", callback_data="platform:locked")])

    # Threads - check if available
    if is_selectable("threads"):
        rows.append([InlineKeyboardButton("🧵 Telegram → Threads", callback_data="platform:threads")])
    else:
        rows.append([InlineKeyboardButton("🧵 Telegram → Threads (Setup Required)", callback_data="platform:locked")])

    # Pinterest - always coming soon
    rows.append([InlineKeyboardButton("📌 Telegram → Pinterest (Coming Soon)", callback_data="platform:locked")])

    return InlineKeyboardMarkup(rows)


# ==========================================
# INSTAGRAM / PROCESSING MANAGEMENT
# ==========================================

def instagram_management_keyboard(project_id, has_processing_channel, destinations):

    buttons = []

    buttons.append([
        InlineKeyboardButton(
            "🔗 Set Converter Output Channel" if not has_processing_channel else "🔄 Change Converter Output Channel",
            callback_data=f"igsetprocessing:{project_id}",
        )
    ])

    for dest in destinations:
        label = dest["username"] or dest["ig_user_id"] or "Instagram destination"
        status_icon = "🟢" if dest["status"] == "available" else "🟡"
        buttons.append([
            InlineKeyboardButton(
                f"{status_icon} {label} ({dest['target_type']})",
                callback_data=f"igdestination:{dest['id']}",
            )
        ])

    buttons.append([
        InlineKeyboardButton("➕ Add Instagram Destination", callback_data=f"igadddest:{project_id}")
    ])

    buttons.append([
        InlineKeyboardButton("📋 Review Approval Queue", callback_data=f"igqueue:{project_id}:0")
    ])

    buttons.append([
        InlineKeyboardButton("🎨 Caption Formatting", callback_data=f"igformat:{project_id}")
    ])

    buttons.append([
        InlineKeyboardButton("⬅ Back to Project", callback_data=f"projcard:{project_id}")
    ])

    return InlineKeyboardMarkup(buttons)


def instagram_destination_type_keyboard(project_id):

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📋 Broadcast Channel (approval queue)",
            callback_data=f"igtargettype:{project_id}:broadcast_channel",
        )],
        [InlineKeyboardButton(
            "📰 Regular Feed (auto-publish)",
            callback_data=f"igtargettype:{project_id}:feed",
        )],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"instagram:{project_id}")],
    ])


def approval_queue_item_keyboard(job_id, project_id, offset):

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Mark Published", callback_data=f"igjobdone:{job_id}:{project_id}:{offset}"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"igjobskip:{job_id}:{project_id}:{offset}"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data=f"instagram:{project_id}")],
    ])


# Legacy alias used by older screens still referencing account_card_keyboard
def account_card_keyboard(connected: bool):
    return account_section_keyboard(connected=connected)


# ==========================================
# BACKWARDS COMPATIBILITY
# ==========================================

def project_keyboard(project_id, running=None, platform_type=None):
    """Legacy alias - delegates to project_actions_keyboard for backwards
    compatibility with existing handlers that still import this name."""
    return project_actions_keyboard(project_id, running=running, platform_type=platform_type)
