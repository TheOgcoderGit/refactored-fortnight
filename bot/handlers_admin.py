# ChannelFlow AI - Support, Admin and Owner Handlers
# ================================================

import logging
from telegram import Update, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from database.db import get_connection
from database.models import count_users, get_all_user_ids
from core.listener import is_running
from services import support_service, support_ai_service, knowledge_service, coupon_service, clone_bot_service, payment_service, plan_service
from config import ADMIN_IDS

logger = logging.getLogger(__name__)

WAITING_AI_SUPPORT = {}
WAITING_TICKET_SUBJ = {}
WAITING_TICKET_BODY = {}
WAITING_COUPON_CODE = {}
WAITING_CLONE_TOKEN = {}
WAITING_BROADCAST_MSG = {}

OWNER_HANDLE = "@devpurushh"
UPDATES_CHANNEL_URL = "https://t.me/BotFoundrry"


async def admin_panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("Admins only.")
        return
    await render_admin_dashboard(update.message)


async def owner_panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("Platform owner only.")
        return
    text = f"👑 Platform Owner Control Panel\n\nOfficial Updates Channel: {UPDATES_CHANNEL_URL}\nOwner Handle: {OWNER_HANDLE}\n\nSelect a management section:"
    buttons = [
        [InlineKeyboardButton("🤖 Clone Bot Network", callback_data="clone:list")],
        [InlineKeyboardButton("💳 Payment Approval Queue", callback_data="adm:payments")],
        [InlineKeyboardButton("👥 Userbase Overview", callback_data="adm:users")],
        [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def render_admin_dashboard(message, edit: bool = False):
    total = count_users()
    engine = "Online" if is_running() else "Offline"
    text = f"🛠 Admin Control Dashboard\n\nUsers: {total}\nForward Engine: {engine}\n\nSelect section:"
    buttons = [
        [InlineKeyboardButton("Review Pending Payments", callback_data="adm:payments")],
        [InlineKeyboardButton("Users Management", callback_data="adm:users")],
        [InlineKeyboardButton("Clone Bot Network", callback_data="clone:list")],
        [InlineKeyboardButton("Broadcast Message", callback_data="adm:bcast_prompt")],
        [InlineKeyboardButton("Home", callback_data="nav:home")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    if edit:
        try: await message.edit_text(text, reply_markup=markup)
        except BadRequest: pass
    else:
        await message.reply_text(text, reply_markup=markup)


async def render_admin_payments(message, user_id: int, edit: bool = False):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM payment_requests WHERE status IN ('SUBMITTED', 'PENDING_PAYMENT') ORDER BY id DESC LIMIT 5")
    rows = cur.fetchall(); conn.close()

    if not rows:
        text = "Payment Review Queue\n\nNo pending payment requests awaiting approval."
        buttons = [[InlineKeyboardButton("Back to Admin", callback_data="adm:dashboard")]]
        markup = InlineKeyboardMarkup(buttons)
        if edit:
            try: await message.edit_text(text, reply_markup=markup)
            except BadRequest: pass
        else:
            await message.reply_text(text, reply_markup=markup)
        return

    text = f"Pending Payments Queue ({len(rows)} requests):\n\n"
    buttons = []
    for r in rows:
        amt = f"Rs {r['amount_inr']:.0f}" if r["amount_inr"] else f"${r['amount_usd']:.2f}"
        text += f"#{r['id']} | User: {r['user_id']} | {r['plan']} ({amt} via {r['method'].upper()})\n"
        buttons.append([InlineKeyboardButton(f"Approve #{r['id']}", callback_data=f"pay:approve:{r['id']}"), InlineKeyboardButton(f"Reject #{r['id']}", callback_data=f"pay:reject:{r['id']}")])
    buttons.append([InlineKeyboardButton("Back to Admin", callback_data="adm:dashboard")])
    markup = InlineKeyboardMarkup(buttons)
    if edit:
        try: await message.edit_text(text, reply_markup=markup)
        except BadRequest: pass
    else:
        await message.reply_text(text, reply_markup=markup)


async def render_clones_view(message, user_id: int, edit: bool = False):
    clones = clone_bot_service.list_bot_instances()
    text = "Owner Clone Bot Network\n\nManage multiple ChannelFlow entry-point bots on your central engine:\n\n"
    if not clones:
        text += "No clone bots registered yet."
    else:
        for c in clones:
            status_text = "Active" if c['status'] == 'active' else "Paused"
            text += f"- @{c['bot_username']} ({status_text})\n"
    buttons = [
        [InlineKeyboardButton("Connect New Clone Bot", callback_data="clone:add_start")],
        [InlineKeyboardButton("Home", callback_data="nav:home")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    if edit:
        try: await message.edit_text(text, reply_markup=markup)
        except BadRequest: pass
    else:
        await message.reply_text(text, reply_markup=markup)


async def render_support_view(message, user_id: int):
    text = (
        f"🆘 ChannelFlow Support Center\n\n"
        f"1️⃣ 🤖 AI Chatbot — Instant answers to setup & feature questions\n"
        f"2️⃣ 👥 Support Team — Chat directly with our team (@ChannelFlowSupport_bot)\n"
        f"3️⃣ 📢 Updates Channel — Official bot updates & announcements\n"
        f"4️⃣ 👑 Contact Owner — Direct VIP line for Creator plan members"
    )
    buttons = [
        [InlineKeyboardButton("🤖 AI Support Chatbot", callback_data="sup:ai_start")],
        [InlineKeyboardButton("👥 Support Team (@ChannelFlowSupport_bot)", url="https://t.me/ChannelFlowSupport_bot")],
        [InlineKeyboardButton("📢 Updates Channel (@BotFoundrry)", url=UPDATES_CHANNEL_URL)],
        [InlineKeyboardButton("👑 Contact Owner (Creator Only)", callback_data="sup:owner")],
        [InlineKeyboardButton("🎫 My Tickets", callback_data="sup:tickets"), InlineKeyboardButton("🔎 FAQ", callback_data="sup:faq")],
        [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
    ]
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def handle_callbacks(query: CallbackQuery, user_id: int, action: str, parts: list, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if action == "pay" and parts[1] == "approve":
        if user_id not in ADMIN_IDS: return True
        req_id = int(parts[2])
        approved_req = payment_service.approve_payment(req_id, admin_id=user_id)
        if approved_req:
            try: await query.edit_message_caption(caption=f"Payment #{req_id} APPROVED.")
            except Exception: await query.message.reply_text(f"Payment #{req_id} APPROVED.")
            try:
                await context.bot.send_message(chat_id=approved_req["user_id"], text=f"🎉 Payment Approved! Your subscription for {approved_req['plan']} is now ACTIVE!")
            except Exception: pass
        else:
            await query.answer("Already decided.", show_alert=True)
        return True

    if action == "pay" and parts[1] == "reject":
        if user_id not in ADMIN_IDS: return True
        req_id = int(parts[2])
        payment_service.reject_payment(req_id, admin_id=user_id, reason="Verification failed")
        try: await query.edit_message_caption(caption=f"Payment #{req_id} REJECTED.")
        except Exception: await query.message.reply_text(f"Payment #{req_id} REJECTED.")
        return True

    if action == "adm" and parts[1] == "payments":
        if user_id not in ADMIN_IDS: return True
        return await render_admin_payments(query.message, user_id, edit=True)

    if action == "adm" and parts[1] == "dashboard":
        if user_id not in ADMIN_IDS: return True
        return await render_admin_dashboard(query.message, edit=True)

    if action == "adm" and parts[1] == "bcast_prompt":
        if user_id not in ADMIN_IDS: return True
        WAITING_BROADCAST_MSG[user_id] = True
        await query.message.reply_text("Send announcement message to broadcast to ALL bot users:")
        return True

    if action == "adm" and parts[1] == "users":
        if user_id not in ADMIN_IDS: return True
        total = count_users()
        engine = "Online" if is_running() else "Offline"
        text = f"Userbase Overview\n\nTotal Users: {total}\nEngine State: {engine}"
        buttons = [[InlineKeyboardButton("Back to Admin", callback_data="adm:dashboard")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return True

    if action == "clone" and parts[1] == "add_start":
        if user_id not in ADMIN_IDS: return True
        WAITING_CLONE_TOKEN[user_id] = True
        await query.edit_message_text("Send your BotFather Bot Token:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="clone:list")]]))
        return True

    if action == "clone" and parts[1] == "list":
        if user_id not in ADMIN_IDS: return True
        return await render_clones_view(query.message, user_id, edit=True)

# Point 4: Creator Plan Gating for Owner Contact
    if action == "sup" and parts[1] == "owner":
        ent = plan_service.get_entitlements(user_id)
        if ent["plan"] == "CREATOR" or user_id in ADMIN_IDS:
            text = (
                f"👑 **VIP Creator Support & Official Channel**\n\n"
                f"As a Creator plan member, you have direct priority access:\n\n"
                f"📢 **Official Updates Channel:**\n{UPDATES_CHANNEL_URL}\n\n"
                f"👑 **Owner / Developer Direct Handle:**\n{OWNER_HANDLE}"
            )
            buttons = [
                [InlineKeyboardButton("📢 Official Channel (@BotFoundrry)", url=UPDATES_CHANNEL_URL)],
                [InlineKeyboardButton(f"👑 Message Developer ({OWNER_HANDLE})", url=f"https://t.me/{OWNER_HANDLE.replace('@','')}")],
                [InlineKeyboardButton("◀️ Back to Support", callback_data="nav:support")]
            ]
        else:
            text = (
                "🔒 **Creator Plan Required**\n\n"
                "Direct contact with the platform owner is exclusively reserved for 👑 Creator Plan members.\n\n"
                "Please use our Support Team (@ChannelFlowSupport_bot) or AI Chatbot, or upgrade your plan to Creator."
            )
            buttons = [
                [InlineKeyboardButton("👑 Upgrade to Creator", callback_data="pay:plan:CREATOR")],
                [InlineKeyboardButton("◀️ Back to Support", callback_data="nav:support")]
            ]
        try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest: pass
        return True
    if action == "sup" and parts[1] == "tickets":
        t_list = support_service.list_user_tickets(user_id)
        text = "Your Support Tickets:\n\n"
        if not t_list:
            text += "No active tickets."
        else:
            for t in t_list:
                text += f"- #{t['id']}: {t['subject']} ({t['status'].upper()})\n"
        buttons = [[InlineKeyboardButton("Open Ticket", callback_data="sup:new_start")], [InlineKeyboardButton("Back to Support", callback_data="nav:support")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return True

    if action == "sup" and parts[1] == "new_start":
        WAITING_TICKET_SUBJ[user_id] = True
        await query.message.reply_text("Send ticket subject:")
        return True

    # Point 4: Creator Plan Gating for Owner Contact
    if action == "sup" and parts[1] == "owner":
        ent = plan_service.get_entitlements(user_id)
        if ent["plan"] == "CREATOR" or user_id in ADMIN_IDS:
            text = (
                f"👑 **VIP Creator Owner & Updates Channel**\n\n"
                f"As a Creator plan member, you have direct access to the platform developer and official channel:\n\n"
                f"📢 **Official Updates Channel:**\n{UPDATES_CHANNEL_URL}\n\n"
                f"👑 **Direct Owner Handle:**\n{OWNER_HANDLE}"
            )
            buttons = [
                [InlineKeyboardButton("📢 Open Updates Channel", url=UPDATES_CHANNEL_URL)],
                [InlineKeyboardButton(f"👑 Message Owner ({OWNER_HANDLE})", url=f"https://t.me/{OWNER_HANDLE.replace('@','')}")],
                [InlineKeyboardButton("◀️ Back to Support", callback_data="nav:support")]
            ]
        else:
            text = (
                "🔒 **Creator Plan Required**\n\n"
                "Direct contact with the platform owner is exclusively reserved for 👑 Creator Plan members.\n\n"
                "Please use our Support Team (@ChannelFlowSupport_bot) or AI Chatbot, or upgrade your plan to Creator."
            )
            buttons = [
                [InlineKeyboardButton("👑 Upgrade to Creator", callback_data="pay:plan:CREATOR")],
                [InlineKeyboardButton("◀️ Back to Support", callback_data="nav:support")]
            ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return True

    return False


async def handle_text(message, user_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if WAITING_AI_SUPPORT.get(user_id):
        WAITING_AI_SUPPORT.pop(user_id, None)
        status_msg = await message.reply_text("Analyzing your question...")

        ans = None
        q = text.lower()
        if "connect" in q or "otp" in q or "flow" in q:
            ans = "Connect karne ke liye Welcome screen par [ 🔌 Connect Account ] dabayein aur apna phone number bhejein. Telegram app me code aane par format `FLOW12345` me reply karein."
        elif "source" in q or "target" in q or "channel" in q:
            ans = "Apne project me jakar [ 📥 Sources ] ya [ 🎯 Targets ] dabayein aur public username (@channel) ya private invite link bhejein. Target channel me bot/account ka admin hona zaroori hai."
        elif "plan" in q or "price" in q or "quota" in q:
            ans = "ChannelFlow ke 4 Plans hain:\n• Free: 100 forwards/day\n• Starter: 200 forwards/day (Rs 199/mo)\n• Pro: 1,000 forwards/day (Rs 399/mo)\n• Creator: 2,000+ forwards/day (Rs 799/mo)"
        elif "affiliate" in q or "amazon" in q:
            ans = "Project me [ 💰 Affiliate ] kholkar apna Amazon Associate tag add karein. ChannelFlow source posts ke sare product links ko aapke tag se auto-replace kar dega!"

        if not ans:
            resp = await support_ai_service.get_support_ai_response(user_id, text)
            ans = resp.text if resp and resp.text else "ChannelFlow AI support: You can manage projects, sources, targets, filters, and auto-forwarding easily. Contact @ChannelFlowSupport_bot for further help."

        await status_msg.edit_text(
            f"🤖 *AI Assistant:*\n\n{ans}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Ask Another Question", callback_data="sup:ai_start")],
                [InlineKeyboardButton("Home", callback_data="nav:home")],
            ])
        )
        return True

    if WAITING_TICKET_SUBJ.get(user_id):
        WAITING_TICKET_SUBJ.pop(user_id, None)
        context.user_data["ticket_subj"] = text
        WAITING_TICKET_BODY[user_id] = True
        await message.reply_text("Now describe the issue in detail:")
        return True

    if WAITING_TICKET_BODY.get(user_id):
        WAITING_TICKET_BODY.pop(user_id, None)
        subj = context.user_data.pop("ticket_subj", "Support Request")
        tid = support_service.create_ticket(user_id, "general", subj, text)
        await message.reply_text(f"Ticket #{tid} Created!\nSubject: {subj}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("My Tickets", callback_data="sup:tickets")]]))
        return True

    if WAITING_COUPON_CODE.get(user_id):
        WAITING_COUPON_CODE.pop(user_id, None)
        ok, c_row, saved, err = coupon_service.validate(text, user_id)
        if ok and c_row:
            coupon_service.record_redemption(c_row["id"], user_id, amount_discounted=saved)
            await message.reply_text(f"Coupon `{text}` Redeemed Successfully!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("My Account", callback_data="nav:account")]]))
        else:
            await message.reply_text(f"Invalid Coupon: {err or 'Code expired.'}")
        return True

    if WAITING_BROADCAST_MSG.get(user_id):
        WAITING_BROADCAST_MSG.pop(user_id, None)
        uids = get_all_user_ids()
        sent = 0
        for uid in uids:
            try:
                await context.bot.send_message(chat_id=uid, text=f"Announcement:\n\n{text}")
                sent += 1
            except Exception:
                pass
        await message.reply_text(f"Broadcast Sent to {sent} users!")
        return True

    if WAITING_CLONE_TOKEN.get(user_id):
        WAITING_CLONE_TOKEN.pop(user_id, None)
        token = text.strip()
        status_msg = await message.reply_text("Validating Bot Token with Telegram API...")
        try:
            bot_meta = await clone_bot_service.validate_bot_token(token)
            inst_id = clone_bot_service.register_bot_instance(user_id, token, bot_meta["bot_id"], bot_meta["username"], bot_meta["first_name"])
            await clone_bot_service.start_clone_app(token, bot_meta["bot_id"])
            await status_msg.edit_text(f"Clone Bot @{bot_meta['username']} is LIVE!\nStatus: Active", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("View Clones", callback_data="clone:list")]]))
        except Exception as e:
            await status_msg.edit_text(f"Error starting Clone Bot: {e}")
        return True

    return False