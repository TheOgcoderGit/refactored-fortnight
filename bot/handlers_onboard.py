# ChannelFlow AI - Master Onboarding, Auth & Account Handlers
# ==========================================================

import asyncio
import logging
import re
from telegram import Update, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from database.db import get_connection
from database.models import register_user
from core import user_sessions, client_pool
from services import plan_service, referral_service, notification_service
from services.feature_explorer_service import PLAN_SYMBOLS, get_plan_page, get_feature_by_id
from services.forward_credit_service import balance as get_credits_balance
from config import ADMIN_IDS

logger = logging.getLogger(__name__)

WAITING_CONNECT_PHONE = {}
WAITING_CONNECT_STAGE = {}

WELCOME_COPY_EN = (
    "👋 Welcome, creator, to ChannelFlow AI!\n\n"
    "ChannelFlow watches the Telegram channels and groups you choose "
    "and automatically forwards or copies new posts to your selected destinations.\n\n"
    "Set filters, reformat or replace text, replace affiliate links, add watermarks, and let ChannelFlow "
    "handle reliability so you don't have to babysit your automation.\n\n"
    "Everything runs through your own connected Telegram account. Your session is encrypted at rest.\n\n"
    "🎁 Your first 7 days start automatically when you register and include the Creator trial. Plan limits apply.\n\n"
    "Tap Connect below to get started."
)

WELCOME_COPY_HI = (
    "👋 ChannelFlow AI me aapka swagat hai, creator!\n\n"
    "ChannelFlow aapke chune hue Telegram channels aur groups ko monitor karta hai "
    "aur unke naye posts ko aapke selected destinations me automatically forward ya copy kar deta hai.\n\n"
    "Filters set karein, text reformat ya replace karein, affiliate links auto-change karein, watermark lagayein, "
    "aur reliability ChannelFlow par chhod dein.\n\n"
    "Sab kuch aapke apne connected Telegram account se chalta hai. Aapka login session encrypted store hota hai.\n\n"
    "🎁 Register karte hi aapka 7-din ka Creator trial shuru ho chuka hai. Plan limits laagu hain.\n\n"
    "Shuru karne ke liye neeche Connect Account dabayein."
)


def get_user_lang(user_id: int) -> str:
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT language FROM users WHERE telegram_id=?", (user_id,))
    row = cur.fetchone(); conn.close()
    return row["language"] if row and row["language"] else "en"


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇬🇧 English", callback_data="lang:set:en"),
            InlineKeyboardButton("🇮🇳 Hinglish", callback_data="lang:set:hi"),
        ]
    ])


def welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔌 Connect Account", callback_data="acct:connect")],
        [InlineKeyboardButton("✨ Explore Features", callback_data="explore:landing")],
        [InlineKeyboardButton("🔐 Why Connect?", callback_data="onboard:why")],
        [
            InlineKeyboardButton("💎 Plans & Credits", callback_data="nav:plans"),
            InlineKeyboardButton("❓ How It Works", callback_data="onboard:how"),
        ],
        [InlineKeyboardButton("🆘 Support", callback_data="nav:support")],
    ])


def connected_home_keyboard(is_vip: bool = False, lang: str = "en") -> InlineKeyboardMarkup:
    t_feat = "✨ Explore Features" if lang == "en" else "✨ Features Dekhein"
    t_earn = "💰 Earn / Affiliate" if lang == "en" else "💰 Earn / Referrals"
    t_acct = "👤 My Account" if lang == "en" else "👤 Mera Account"

    keyboard = [
        [InlineKeyboardButton("📁 Projects", callback_data="nav:projects")],
        [InlineKeyboardButton(t_feat, callback_data="explore:landing")],
        [
            InlineKeyboardButton("💎 Plans & Credits", callback_data="nav:plans"),
            InlineKeyboardButton(t_earn, callback_data="nav:earn"),
        ],
        [
            InlineKeyboardButton(t_acct, callback_data="nav:account"),
            InlineKeyboardButton("🆘 Support", callback_data="nav:support"),
        ],
    ]
    if is_vip:
        keyboard.append([InlineKeyboardButton("👑 VIP Owner Contact", callback_data="sup:owner")])
    return InlineKeyboardMarkup(keyboard)


def disconnected_home_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    t_rec = "🔌 Reconnect Account" if lang == "en" else "🔌 Reconnect Karein"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t_rec, callback_data="acct:connect")],
        [InlineKeyboardButton("📁 Projects", callback_data="nav:projects")],
        [
            InlineKeyboardButton("👤 My Account", callback_data="nav:account"),
            InlineKeyboardButton("🏠 Home", callback_data="nav:welcome"),
        ],
    ])


def returning_unconnected_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    t_conn = "🔌 Connect Account Now" if lang == "en" else "🔌 Abhi Account Connect Karein"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t_conn, callback_data="acct:connect")],
        [InlineKeyboardButton("✨ Explore Features", callback_data="explore:landing")],
        [InlineKeyboardButton("🔐 Why Connect?", callback_data="onboard:why")],
        [
            InlineKeyboardButton("💎 Plans & Credits", callback_data="nav:plans"),
            InlineKeyboardButton("❓ How It Works", callback_data="onboard:how"),
        ],
        [InlineKeyboardButton("🆘 Support", callback_data="nav:support")],
    ])


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user: return

    register_user(user.id, user.username, user.first_name)

    if context.args:
        ref_id = referral_service.parse_referral_code(context.args[0])
        if ref_id: referral_service.capture_referral(ref_id, user.id)

    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT language, onboarding_step FROM users WHERE telegram_id=?", (user.id,))
    row = cur.fetchone(); conn.close()

    # Tier 1: First-time prompt for Language
    if row is None or not row["onboarding_step"] or row["onboarding_step"] == "language":
        plan_service.start_trial(user.id)
        await update.message.reply_text(
            "🌐 Choose your preferred language:\n\nApni pasandeeda bhasha chunein:",
            reply_markup=language_keyboard(),
        )
        return

    lang = row["language"] or "en"
    is_conn = user.id in ADMIN_IDS or user_sessions.is_connected(user.id)

    # 4-Tier State Detection
    if is_conn:
        # Tier 4: Connected user
        ent = plan_service.get_entitlements(user.id)
        is_vip = ent["plan"] == "CREATOR" or user.id in ADMIN_IDS
        t_msg = (
            "👋 Welcome back!\n\n✅ Your Telegram account is connected and your automation engine is ready."
            if lang == "en" else
            "👋 Welcome back!\n\n✅ Aapka Telegram account connected hai aur automation engine ready hai."
        )
        await update.message.reply_text(t_msg, reply_markup=connected_home_keyboard(is_vip=is_vip, lang=lang))
    else:
        conn = get_connection(); cur = conn.cursor()
        cur.execute("SELECT 1 FROM user_telegram_sessions WHERE telegram_id=?", (user.id,))
        had_session = cur.fetchone() is not None; conn.close()

        if had_session:
            # Tier 3: Disconnected user
            t_msg = (
                "👋 Welcome back!\n\n"
                "⚠️ **Your Telegram account session is currently disconnected.**\n\n"
                "Your automation projects are paused. Reconnect your account now to restore access to your projects and resume automatic forwarding."
                if lang == "en" else
                "👋 Wapas swagat hai!\n\n"
                "⚠️ **Aapka Telegram account session disconnected hai.**\n\n"
                "Aapke automation projects pause hain. Apne projects ka access wapas paane aur auto-forwarding resume karne ke liye abhi reconnect karein."
            )
            await update.message.reply_text(t_msg, reply_markup=disconnected_home_keyboard(lang=lang))
        else:
            # Tier 2: Returning user who registered but NEVER connected
            t_msg = (
                "👋 Welcome back to ChannelFlow AI!\n\n"
                "⚠️ You haven't connected your Telegram account yet.\n\n"
                "🎁 **Connect your account now to claim your 7-Day Free Trial of the Creator Plan!**\n\n"
                "Once connected, you will unlock full automated forwarding across all your channels."
                if lang == "en" else
                "👋 ChannelFlow AI me wapas swagat hai!\n\n"
                "⚠️ Aapne abhi tak apna Telegram account connect nahi kiya hai.\n\n"
                "🎁 **Abhi apna account connect karein aur Creator Plan ka 7-Din ka Free Trial claim karein!**\n\n"
                "Connect karte hi aapke channels ke liye live auto-forwarding chalu ho jayegi."
            )
            await update.message.reply_text(t_msg, reply_markup=returning_unconnected_keyboard(lang=lang))

start = start_cmd
start_command = start_cmd


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = get_user_lang(user.id)
    is_conn = user.id in ADMIN_IDS or user_sessions.is_connected(user.id)
    t_msg = "❌ Action cancelled." if lang == "en" else "❌ Action cancel kar diya gaya hai."
    await update.message.reply_text(
        t_msg,
        reply_markup=connected_home_keyboard(lang=lang) if is_conn else welcome_keyboard(),
    )

cancel_cmd = cancel


# Shared so /connect, the account screen's button and the "Reconnect
# account" button on failure screens all show the same instructions. They
# used to be three different messages of varying detail, and the short one
# (on the button) did not mention that spaces break the number - which is
# the single most common reason connecting fails.
CONNECT_PROMPT = (
    "📱 **Connect Account**\n\n"
    "Please send your Telegram phone number with country code.\n"
    "Example (India): `+919876543210`\n\n"
    "⚠️ **Important:**\n"
    "• Number must start with `+` and country code\n"
    "• NO spaces inside the number\n"
    "• Correct: `+914527896325`\n"
    "• Wrong: `+91 45278 96325`\n\n"
    "What happens next:\n"
    "1. Telegram sends an official verification code to your Telegram app.\n"
    "2. Send it as `FLOW12345` (replace 12345 with your OTP).\n"
    "3. Your session is encrypted immediately with AES-256."
)

CONNECT_WHY = (
    "🤔 **Why does ChannelFlow need my number?**\n\n"
    "ChannelFlow forwards posts **as you**, using your own Telegram "
    "account. That is the only way it can read your private channels and "
    "post to channels where you are an admin — a bot account cannot do "
    "either.\n\n"
    "• The number is only used to log in once.\n"
    "• The resulting session is encrypted at rest (AES-256).\n"
    "• The OTP is never stored or logged.\n"
    "• You can disconnect any time from ⚙️ Settings.\n\n"
    "ChannelFlow never posts anything on your behalf without a project "
    "you created."
)


def connect_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤔 Why is this needed?", callback_data="connect:why")],
        [
            InlineKeyboardButton("◀️ Back", callback_data="nav:home"),
            InlineKeyboardButton("❌ Cancel", callback_data="act:cancel"),
            InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
        ],
    ])


async def connect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user_sessions.is_connected(user.id):
        await update.message.reply_text("✅ Your Telegram account is already connected.")
        return

    if not context.args:
        WAITING_CONNECT_PHONE[user.id] = True
        await update.message.reply_text(
            CONNECT_PROMPT, reply_markup=connect_keyboard(), parse_mode="Markdown"
        )
        return

    await initiate_phone_connect(update.message, user.id, context.args[0].strip())

connect_command = connect_cmd


# Telegram can take a while to answer a code request, and the client
# retries on a weak connection. Without a ceiling the user sits staring
# at "Requesting..." with no way to tell failure from slowness, so cap it
# and say what happened.
CONNECT_REQUEST_TIMEOUT = 60


def normalise_phone(raw: str) -> str:
    """Accept the ways people actually type a number.

    "+91 98765 43210" is the same number as "+919876543210", and getting
    it wrong here means the user is told their number is invalid when it
    is not - and the flow ends there.
    """
    return re.sub(r"[\s\-()]", "", raw or "")


async def initiate_phone_connect(message, user_id: int, phone: str):
    phone = normalise_phone(phone)
    if not phone.startswith("+") or not phone[1:].isdigit():
        await message.reply_text(
            "❌ Include the country code, e.g. `+919876543210`.\n\n"
            "Spaces and dashes are fine — I'll strip them out."
        )
        return

    WAITING_CONNECT_PHONE.pop(user_id, None)
    await message.reply_text("⏳ Requesting verification code from Telegram...")

    try:
        await asyncio.wait_for(
            user_sessions.start_connect(user_id, phone),
            timeout=CONNECT_REQUEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        await message.reply_text(
            "⏳ Telegram didn't answer in "
            f"{CONNECT_REQUEST_TIMEOUT} seconds.\n\n"
            "This is usually a network problem on the bot's side. "
            "Please wait a moment and send /connect to try again.",
            reply_markup=(
                connect_keyboard()
            ),
        )
        return
    except user_sessions.ConnectError as e:
        await message.reply_text(f"❌ {e}")
        return
    except Exception as e:
        # Anything else - most often SESSION_ENCRYPTION_KEY missing, or
        # Telethon cannot reach Telegram at all - used to escape to the
        # global error handler, which answers with a generic notice the
        # user cannot act on. Say what went wrong instead.
        logger.exception("start_connect failed for user %s", user_id)
        await message.reply_text(
            "❌ Could not request the login code.\n\n"
            f"Reason: `{type(e).__name__}: {e}`\n\n"
            "If this mentions SESSION_ENCRYPTION_KEY, the bot owner needs "
            "to set it in .env before Telegram login can be used.\n"
            "Otherwise it is usually connectivity - try /connect again.",
            reply_markup=(
                connect_keyboard()
            ),
        )
        return

    WAITING_CONNECT_STAGE[user_id] = "code"
    await message.reply_text(
        "🔑 **Enter Login OTP**\n\n"
        "Telegram sent an official verification code to your Telegram app.\n\n"
        "Send it in chat as:\n\n"
        "`FLOW12345`\n\n"
        "or:\n\n"
        "`FLOW 12345`\n\n"
        "⚠️ The **FLOW** prefix is required."
    )


async def render_account_view(message, user, edit: bool = False):
    ent = plan_service.get_entitlements(user.id)
    c_bal = get_credits_balance(user.id)
    phone = "Connected" if user_sessions.is_connected(user.id) else "Disconnected"
    lang = get_user_lang(user.id)

    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT auto_renew, language, wallet_currency FROM users WHERE telegram_id=?", (user.id,))
    u_row = cur.fetchone(); conn.close()

    auto_renew_state = "ON" if (u_row and u_row["auto_renew"]) else "OFF"
    lang_name = "Hinglish" if lang == "hi" else "English"

    # How much plan is left. plan_expiry was stored and enforced but never
    # shown, so nobody could tell a 7-day Creator trial was running.
    days_left = ent.get("days_left")
    if ent.get("expired"):
        plan_state = "Ended — now on Free"
    elif ent.get("on_trial"):
        plan_state = f"{ent['plan']} (trial, {days_left} day(s) left)"
    elif days_left is not None:
        plan_state = f"{ent['plan']} ({days_left} day(s) left)"
    else:
        plan_state = ent["plan"]
    cur_m = (u_row["wallet_currency"] if u_row and u_row["wallet_currency"] else "upi").upper()

    if lang == "hi":
        text = (
            f"👤 Mera Account aur Settings\n\n"
            f"• Naam: {user.first_name}\n"
            f"• User ID: `{user.id}`\n"
            f"• Telegram Account: {phone}\n"
            f"• Current Plan: {plan_state}\n"
            f"• Daily Quota: {ent['daily_forward_limit']} forwards/din\n"
            f"• Extra Credits: {c_bal}\n"
            f"• Currency: {cur_m}\n"
            f"• Bhasha: {lang_name}\n"
            f"• Auto-Renew: {auto_renew_state}\n"
        )
    else:
        text = (
            f"👤 My Account & Settings\n\n"
            f"• Name: {user.first_name}\n"
            f"• User ID: `{user.id}`\n"
            f"• Telegram Account: {phone}\n"
            f"• Plan: {plan_state}\n"
            f"• Daily Quota: {ent['daily_forward_limit']} forwards/day\n"
            f"• Extra Credits: {c_bal}\n"
            f"• Currency: {cur_m}\n"
            f"• Language: {lang_name}\n"
            f"• Auto-Renew: {auto_renew_state}\n"
        )

    buttons = [
        [InlineKeyboardButton(f"🌐 Language ({lang_name})", callback_data="acct:lang_picker"), InlineKeyboardButton(f"🔁 Auto-Renew: {auto_renew_state}", callback_data="acct:autorenew")],
        [InlineKeyboardButton("🔔 Notifications", callback_data="acct:notifs"), InlineKeyboardButton("🎟 Redeem Coupon", callback_data="acct:redeem_prompt")],
        [InlineKeyboardButton("🧾 Payment History", callback_data="acct:payhistory"), InlineKeyboardButton("🔌 Disconnect Session", callback_data="settings:disconnect")],
        [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    if edit:
        try: await message.edit_text(text, reply_markup=markup)
        except BadRequest: pass
    else:
        await message.reply_text(text, reply_markup=markup)


async def handle_callbacks(query: CallbackQuery, user_id: int, action: str, parts: list, context: ContextTypes.DEFAULT_TYPE) -> bool:
    lang = get_user_lang(user_id)

    # Language Switcher
    if action == "lang" and parts[1] == "set":
        new_lang = parts[2]
        conn = get_connection(); cur = conn.cursor()
        cur.execute("SELECT onboarding_step FROM users WHERE telegram_id=?", (user_id,))
        u_row = cur.fetchone()
        cur.execute("UPDATE users SET language=?, onboarding_step='welcome' WHERE telegram_id=?", (new_lang, user_id))
        conn.commit(); conn.close()

        if u_row and u_row["onboarding_step"] == "language":
            w_text = WELCOME_COPY_HI if new_lang == "hi" else WELCOME_COPY_EN
            await query.edit_message_text(w_text, reply_markup=welcome_keyboard())
        else:
            lang_lbl = "Hinglish" if new_lang == "hi" else "English"
            await query.answer(f"✅ Language updated to {lang_lbl}!", show_alert=True)
            return await render_account_view(query.message, query.from_user, edit=True)
        return True

    if action == "nav" and parts[1] == "welcome":
        w_text = WELCOME_COPY_HI if lang == "hi" else WELCOME_COPY_EN
        await query.edit_message_text(w_text, reply_markup=welcome_keyboard())
        return

    # Detailed Why Connect
    if action == "onboard" and parts[1] == "why":
        text = (
            "🔐 **Why Connect Your Telegram Account?**\n\n"
            "ChannelFlow needs to connect with your Telegram account so it can:\n"
            "• Monitor source channels and groups you are a member of (including private chats).\n"
            "• Post, copy, and forward content into your destinations seamlessly.\n"
            "• Handle reactions, topic routing, and post-edit synchronizations.\n\n"
            "🛡️ **Security & Privacy Guarantee:**\n"
            "• Your login session is encrypted at rest using AES-256-GCM authenticated encryption.\n"
            "• We never store or log your passwords or OTP verification codes.\n"
            "• Your data is completely isolated from other users.\n"
            "• You can disconnect your session at any time with one tap."
            if lang == "en" else
            "🔐 **Telegram Account Connect Kyun Karein?**\n\n"
            "ChannelFlow aapka account isliye connect karta hai taaki:\n"
            "• Aapke un channels aur groups ko monitor kar sake jinhe aapne join kiya hua hai (private chats bhi).\n"
            "• Destination channels me posts ko bina ruke automatically forward ya copy kar sake.\n"
            "• Post edit sync aur auto-reactions ko smoothly chala sake.\n\n"
            "🛡️ **Suraksha aur Privacy:**\n"
            "• Aapka login session AES-256 encryption se 100% surakshit store hota hai.\n"
            "• Hum aapke OTP ya password ko kabhi log ya store nahi karte.\n"
            "• Aapka data doosre kisi bhi user se share nahi hota.\n"
            "• Aap jab chahein ek tap me apna session disconnect kar sakte hain."
        )
        buttons = [
            [InlineKeyboardButton("🔌 Connect Now", callback_data="acct:connect")],
            [InlineKeyboardButton("◀️ Back", callback_data="nav:welcome"), InlineKeyboardButton("🏠 Home", callback_data="nav:home")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return True

    # Detailed How It Works
    if action == "onboard" and parts[1] == "how":
        text = (
            "❓ **How ChannelFlow Works (5 Steps)**\n\n"
            "1️⃣ **Connect Telegram Account:**\n"
            "Connect your account via /connect. This enables ChannelFlow to access your channels.\n\n"
            "2️⃣ **Create a Project:**\n"
            "Pick a template (e.g. Channel -> Channel, Affiliate Deals) or start from scratch.\n\n"
            "3️⃣ **Add Sources & Targets:**\n"
            "Add the channels you want to watch (Sources) and where to deliver (Targets).\n\n"
            "4️⃣ **Configure Rules:**\n"
            "Set filters (keywords, regex), formatting (strip handles/links), watermark, and AI rewriting.\n\n"
            "5️⃣ **Activate & Enjoy:**\n"
            "Tap Start! ChannelFlow will automatically relay new posts 24/7 without needing you to babysit it."
        )
        buttons = [
            [InlineKeyboardButton("➕ Create Project", callback_data="onboard:create_gate")],
            [InlineKeyboardButton("🔌 Connect Account", callback_data="acct:connect")],
            [InlineKeyboardButton("◀️ Back", callback_data="nav:welcome")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return True

    # Gate Create Project from How It Works if not connected
    if action == "onboard" and parts[1] == "create_gate":
        is_conn = user_id in ADMIN_IDS or user_sessions.is_connected(user_id)
        if not is_conn:
            text = (
                "🔒 **Account Connection Required**\n\n"
                "To create an automation project, please connect your Telegram account first.\n\n"
                "ChannelFlow needs your connected account to monitor your source channels and post to destinations."
                if lang == "en" else
                "🔒 **Account Connect Karna Zaroori Hai**\n\n"
                "Automation project banane ke liye pehle apna Telegram account connect karein.\n\n"
                "ChannelFlow aapke account ke zariye hi source channels padh kar destinations me post kar sakta hai."
            )
            buttons = [
                [InlineKeyboardButton("🔌 Connect Account Now", callback_data="acct:connect")],
                [InlineKeyboardButton("◀️ Back to Guide", callback_data="onboard:how")]
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return True
        else:
            # Connected: redirect to project creation
            query.data = "proj:new"
            return False  # Let handlers_projects handle proj:new

    # Feature Explorer Handlers
    if action == "explore" and parts[1] == "landing":
        text = "✨ ChannelFlow Features\n\nChoose a plan to explore its features:" if lang == "en" else "✨ ChannelFlow Features\n\nPlan chunein aur uske features dekhein:"
        keyboard = [
            [InlineKeyboardButton("🆓 Free", callback_data="explore:plan:FREE:0"), InlineKeyboardButton("🚀 Starter", callback_data="explore:plan:STARTER:0")],
            [InlineKeyboardButton("⭐ Pro", callback_data="explore:plan:PRO:0"), InlineKeyboardButton("👑 Creator", callback_data="explore:plan:CREATOR:0")],
            [InlineKeyboardButton("💎 Compare Plans", callback_data="nav:plans")],
            [InlineKeyboardButton("◀️ Back", callback_data="nav:welcome"), InlineKeyboardButton("🏠 Home", callback_data="nav:home")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return True

    if action == "explore" and parts[1] == "plan":
        plan = parts[2]
        page = int(parts[3])
        items, cur_page, total_pages = get_plan_page(plan, page)
        plan_display = PLAN_SYMBOLS.get(plan, plan)
        text = f"{plan_display} Plan — Features\n\n"
        for it in items: text += f"• {it['name']}: {it['desc']}\n"
        text += f"\nPage {cur_page + 1} of {total_pages}"
        keyboard = []
        for item in items: keyboard.append([InlineKeyboardButton(item["name"], callback_data=f"feat:view:{item['id']}:{plan}:{cur_page}")])
        nav_row = []
        if cur_page > 0: nav_row.append(InlineKeyboardButton("◀ Previous", callback_data=f"explore:plan:{plan}:{cur_page - 1}"))
        if cur_page < total_pages - 1: nav_row.append(InlineKeyboardButton("Next ▶", callback_data=f"explore:plan:{plan}:{cur_page + 1}"))
        if nav_row: keyboard.append(nav_row)
        keyboard.append([InlineKeyboardButton("🆓", callback_data="explore:plan:FREE:0"), InlineKeyboardButton("🚀", callback_data="explore:plan:STARTER:0"), InlineKeyboardButton("⭐", callback_data="explore:plan:PRO:0"), InlineKeyboardButton("👑", callback_data="explore:plan:CREATOR:0")])
        keyboard.append([InlineKeyboardButton("💎 View Plan Pricing", callback_data="nav:plans"), InlineKeyboardButton("◀️ Back", callback_data="explore:landing")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return True

    if action == "feat" and parts[1] == "view":
        feat_id, plan, page = parts[2], parts[3], int(parts[4])
        feat = get_feature_by_id(feat_id)
        text = f"{feat['name']}\n\n{feat['desc']}\n\nAvailable on:\n{feat['min_plan']}+ Plans\n\n• Automated processing\n• Real-time reliability"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 View Plans", callback_data="nav:plans")],
            [InlineKeyboardButton("◀️ Back to Features", callback_data=f"explore:plan:{plan}:{page}"), InlineKeyboardButton("🏠 Home", callback_data="nav:home")]
        ]))
        return True

    if action == "acct" and parts[1] == "connect":
        if user_sessions.is_connected(user_id):
            await query.edit_message_text("✅ Telegram account is already connected.", reply_markup=connected_home_keyboard(lang=lang))
            return
        WAITING_CONNECT_PHONE[user_id] = True
        await query.message.reply_text(
            CONNECT_PROMPT, reply_markup=connect_keyboard(), parse_mode="Markdown"
        )
        return

    if action == "connect":
        if parts[1] == "why":
            await query.edit_message_text(
                CONNECT_WHY,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📱 Continue", callback_data="acct:connect")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="act:cancel"),
                     InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
                ]),
            )
            return True
        return False

    # Disconnect Confirmation Sheet
    if action == "settings" and parts[1] == "disconnect":
        text = (
            "⚠️ Are you sure you want to disconnect your Telegram session?\n\nActive forwarding projects will be paused."
            if lang == "en" else
            "⚠️ Kya aap sach me apna Telegram session disconnect karna chahte hain?\nAapke active forwarding projects pause ho jayenge."
        )
        buttons = [
            [InlineKeyboardButton("🔌 Yes, Disconnect", callback_data="acct:confirm_disconnect")],
            [InlineKeyboardButton("❌ Cancel", callback_data="nav:account")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return True

    if action == "acct" and parts[1] == "confirm_disconnect":
        await client_pool.stop_owner_engine(user_id)
        user_sessions.disconnect_user(user_id)
        text = "🔌 Session disconnected successfully." if lang == "en" else "🔌 Session disconnect ho chuka hai."
        await query.edit_message_text(text, reply_markup=disconnected_home_keyboard(lang=lang))
        return True

    if action == "acct" and parts[1] == "lang_picker":
        text = "🌐 Select Preferred Language / Bhasha chunein:"
        buttons = [
            [InlineKeyboardButton("🇬🇧 English", callback_data="lang:set:en"), InlineKeyboardButton("🇮🇳 Hinglish", callback_data="lang:set:hi")],
            [InlineKeyboardButton("◀️ Back to Account", callback_data="nav:account")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return True

    if action == "acct" and parts[1] == "autorenew":
        conn = get_connection(); cur = conn.cursor()
        cur.execute("SELECT auto_renew FROM users WHERE telegram_id=?", (user_id,))
        row = cur.fetchone(); cur_renew = bool(row["auto_renew"]) if row else False
        new_val = 0 if cur_renew else 1
        cur.execute("UPDATE users SET auto_renew=? WHERE telegram_id=?", (new_val, user_id))
        conn.commit(); conn.close()
        await query.answer(f"Auto-Renew {'Enabled' if new_val else 'Disabled'}!", show_alert=True)
        return await render_account_view(query.message, query.from_user, edit=True)

    if action == "acct" and parts[1] == "notifs":
        prefs = notification_service.get_prefs(user_id)
        text = "🔔 Notification Preferences:"
        buttons = [
            [InlineKeyboardButton(f"📣 Promotions: {'🟢 ON' if prefs.get('marketing', True) else '🔴 OFF'}", callback_data="notif:toggle:marketing")],
            [InlineKeyboardButton(f"🆕 Updates: {'🟢 ON' if prefs.get('product_updates', True) else '🔴 OFF'}", callback_data="notif:toggle:product_updates")],
            [InlineKeyboardButton(f"🎁 Referral Alerts: {'🟢 ON' if prefs.get('referral_rewards', True) else '🔴 OFF'}", callback_data="notif:toggle:referral_rewards")],
            [InlineKeyboardButton("◀️ Back to Account", callback_data="nav:account")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return True

    if action == "notif" and parts[1] == "toggle":
        k = parts[2]; p = notification_service.get_prefs(user_id)
        notification_service.set_pref(user_id, k, not p.get(k, True))
        query.data = "acct:notifs"
        return await handle_callbacks(query, user_id, "acct", ["acct", "notifs"], context)

    return False


async def handle_text(message, user_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    lang = get_user_lang(user_id)

    # 1. Phone number connect
    if WAITING_CONNECT_PHONE.get(user_id):
        await initiate_phone_connect(message, user_id, text)
        return True

    # 2. FLOW<OTP> Code Submission
    if WAITING_CONNECT_STAGE.get(user_id) == "code" or text.startswith("FLOW"):
        stage = user_sessions.pending_stage(user_id)
        if not stage:
            WAITING_CONNECT_STAGE.pop(user_id, None)
            await message.reply_text("No active login attempt. Use /connect first.")
            return True

        ok, code_or_err = user_sessions.extract_otp_from_command(text)
        if not ok:
            await message.reply_text(f"⚠️ {code_or_err}")
            return True

        try:
            # FIX: user_id used directly (no user.id NameError)
            await user_sessions.submit_code(user_id, code_or_err)
            WAITING_CONNECT_STAGE.pop(user_id, None)

            # Safely start client engine in background (never blocks user)
            try:
                await client_pool.start_owner_engine(user_id)
            except Exception as ex:
                logger.warning("Auto engine startup delayed: %s", ex)

            # Instantly open Connected Home
            t_connected = (
                "🎉 **Telegram Account Connected Successfully!**\n\n"
                "Your ChannelFlow automation engine is now live and ready.\n"
                "Use the menu below to create your projects:"
                if lang == "en" else
                "🎉 **Telegram Account Successfully Connect Ho Gaya!**\n\n"
                "Aapka automation engine live ho chuka hai.\n"
                "Projects create karne ke liye neeche diye gaye menu ka use karein:"
            )
            await message.reply_text(t_connected, reply_markup=connected_home_keyboard(lang=lang))

        except user_sessions.NeedsPassword:
            WAITING_CONNECT_STAGE[user_id] = "password"
            await message.reply_text("🔐 **Two-Step Verification Enabled**\nEnter your Telegram 2FA password:")
        except user_sessions.ConnectError as e:
            await message.reply_text(f"❌ {e}")
        except Exception as e:
            logger.exception("Unexpected error in OTP handler: %s", e)
            await message.reply_text(f"❌ Error: {e}")
        return True

    # 3. 2FA Password Submission
    if WAITING_CONNECT_STAGE.get(user_id) == "password":
        try:
            # FIX: user_id used directly (no user.id NameError)
            await user_sessions.submit_password(user_id, text)
            WAITING_CONNECT_STAGE.pop(user_id, None)

            try:
                await client_pool.start_owner_engine(user_id)
            except Exception:
                pass

            t_connected = (
                "🎉 **Telegram Connected Successfully!**\n\nYour automation engine is live."
                if lang == "en" else
                "🎉 **Telegram Successfully Connect Ho Gaya!**\n\nAapka automation engine live hai."
            )
            await message.reply_text(t_connected, reply_markup=connected_home_keyboard(lang=lang))
        except user_sessions.ConnectError as e:
            await message.reply_text(f"❌ {e}")
        except Exception as e:
            await message.reply_text(f"❌ Error: {e}")
        return True

    return False