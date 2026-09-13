# ChannelFlow AI - Subscription Plans, Billing and Payment Handlers
# ==============================================================
# Complete Master PRD Implementation:
# - Smart Jin Payment System (Remembers user preferred method)
# - Safe Duration Calculation (Dict conversion, no sqlite3.Row crashes)
# - Native Telegram Stars Checkout (Official XTR currency & Instant Activation)
# - Live Oxapay Crypto Invoices (Real browser checkout & status polling)
# - UPI Payments with Payee details & Screenshot submission
# - Extra Forwarding Credits Store (Multi-currency: INR / USD / Stars)
# - Dynamic Referral Link & Top 10 Leaderboard
# - BadRequest (Message is not modified) immune

import logging
from datetime import datetime, timezone
import httpx

from telegram import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from database.db import get_connection
from services import plan_service, pricing_service, referral_service, payment_service
from services.forward_credit_service import balance as get_credits_balance, adjust as adjust_credits
from config import OXAPAY_API_KEY, UPI_ID, UPI_PAYEE_NAME

logger = logging.getLogger(__name__)

WAITING_PAYMENT_SCREENSHOT = {}
_user_preferred_method = {}

STARS_DEFAULT_PRICES = {
    "FREE": 0,
    "STARTER": 150,
    "BEGINNER": 150,
    "PRO": 300,
    "CREATOR": 600
}

CREDITS_PRICING = {
    500: {"inr": 99, "usd": 1.20, "stars": 75},
    1000: {"inr": 179, "usd": 2.10, "stars": 140},
    5000: {"inr": 699, "usd": 8.50, "stars": 550},
    10000: {"inr": 1199, "usd": 14.50, "stars": 950}
}


def get_user_pay_method(user_id: int) -> str:
    """Retrieves user's saved payment method (Jin feature). Defaults to 'upi'."""
    if user_id in _user_preferred_method:
        return _user_preferred_method[user_id]
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT wallet_currency FROM users WHERE telegram_id=?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if row and row["wallet_currency"] in ("upi", "crypto", "stars"):
            _user_preferred_method[user_id] = row["wallet_currency"]
            return row["wallet_currency"]
    except Exception:
        pass
    return "upi"


def set_user_pay_method(user_id: int, method: str):
    """Saves user's preferred payment method in DB and cache."""
    method = method.lower()
    _user_preferred_method[user_id] = method
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET wallet_currency=? WHERE telegram_id=?", (method, user_id))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _get_clean_durations(plan_name: str) -> list:
    """Safe retrieval of duration rows converted to standard Python dicts."""
    plan_upper = plan_name.upper()
    query_plan = "BEGINNER" if plan_upper == "STARTER" else plan_upper
    try:
        raw = pricing_service.get_duration_options(query_plan)
        if not raw:
            raw = pricing_service.get_duration_options(plan_upper)
        options = [dict(r) for r in raw]
    except Exception:
        options = []
    if not options:
        options = [
            {"months": 1, "discount_percent": 0.0},
            {"months": 3, "discount_percent": 5.0},
            {"months": 6, "discount_percent": 10.0},
            {"months": 12, "discount_percent": 20.0},
        ]
    return options


async def render_plans_view(message, user_id: int, edit: bool = False):
    """PRD §21: Rich Plans Hub with Live Quotas and Currency Conversion."""
    ent = plan_service.get_entitlements(user_id)
    c_bal = get_credits_balance(user_id)
    active_m = get_user_pay_method(user_id)

    method_labels = {
        "upi": "🇮🇳 UPI (INR ₹)",
        "crypto": "🪙 Crypto (USD $) via Oxapay",
        "stars": "⭐ Telegram Stars"
    }

    text = (
        f"💎 **ChannelFlow Plans & Credits Hub**\n\n"
        f"👤 Current Plan: **{ent['plan']}**\n"
        f"⚡ Daily Allowance: **{ent['daily_forward_limit']} forwards/day**\n"
        f"📦 Extra Credits: **{c_bal}**\n"
        f"💳 Active Currency: **{method_labels.get(active_m, 'UPI')}**\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 **Plan Features & Limits:**\n\n"
        f"🆓 **Free Plan:**\n"
        f"• 3 Projects · 10 Sources · 10 Targets\n"
        f"• 100 Forwards/day · Basic Forwarding\n\n"
        f"🚀 **Starter Plan (from ₹199/mo | $6.99 | ⭐150):**\n"
        f"• 7 Projects · 25 Sources · 25 Targets\n"
        f"• 200 Forwards/day · Post Edit Sync · Auto Reactions\n\n"
        f"⭐ **Pro Plan (from ₹399/mo | $14.99 | ⭐300):**\n"
        f"• 15 Projects · 50 Sources · 50 Targets\n"
        f"• 1,000 Forwards/day · AI Rewriter · Watermark · Affiliate Replacer\n\n"
        f"👑 **Creator Plan (from ₹799/mo | $19.99 | ⭐600):**\n"
        f"• 15 Projects · 50 Sources · 50 Targets\n"
        f"• 2,000+ Forwards/day · High Speed · VIP Owner Support (@devpurushh)\n\n"
        f"Select an upgrade tier or top-up extra credits:"
    )

    if active_m == "crypto":
        buttons = [
            [InlineKeyboardButton("🚀 Starter ($6.99/mo)", callback_data="pay:plan:STARTER")],
            [InlineKeyboardButton("⭐ Pro ($14.99/mo)", callback_data="pay:plan:PRO")],
            [InlineKeyboardButton("👑 Creator ($19.99/mo)", callback_data="pay:plan:CREATOR")],
        ]
    elif active_m == "stars":
        buttons = [
            [InlineKeyboardButton("🚀 Starter (⭐ 150/mo)", callback_data="pay:plan:STARTER")],
            [InlineKeyboardButton("⭐ Pro (⭐ 300/mo)", callback_data="pay:plan:PRO")],
            [InlineKeyboardButton("👑 Creator (⭐ 600/mo)", callback_data="pay:plan:CREATOR")],
        ]
    else:
        buttons = [
            [InlineKeyboardButton("🚀 Starter (₹199/mo)", callback_data="pay:plan:STARTER")],
            [InlineKeyboardButton("⭐ Pro (₹399/mo)", callback_data="pay:plan:PRO")],
            [InlineKeyboardButton("👑 Creator (₹799/mo)", callback_data="pay:plan:CREATOR")],
        ]

    buttons.append([InlineKeyboardButton("📦 Extra Credits Store", callback_data="cred:store")])
    buttons.append([InlineKeyboardButton("🔄 Change Payment Method", callback_data="pay:select_method")])
    buttons.append([InlineKeyboardButton("🏠 Home", callback_data="nav:home")])

    markup = InlineKeyboardMarkup(buttons)
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
        except BadRequest:
            pass
    else:
        await message.reply_text(text, reply_markup=markup)


async def render_earn_view(message, user_id: int):
    """PRD §23: Referral Hub with Dynamic Bot Username and Live Stats."""
    code = referral_service.build_referral_code(user_id)
    stats = referral_service.get_referral_stats(user_id)
    bot_username = getattr(message._bot, "username", "ChannelFlowBot") if hasattr(message, "_bot") else "ChannelFlowBot"

    text = (
        f"🎁 **Rewards & Referral Program**\n\n"
        f"Share your link with creators. When they upgrade to any paid plan, you earn +7 Days of Pro!\n\n"
        f"🔗 **Your Personal Link:**\n"
        f"https://t.me/{bot_username}?start={code}\n\n"
        f"👥 **Total Invited:** {stats['total_invited']}\n"
        f"✅ **Paid Conversions:** {stats['active_referrals']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 **Milestone Tiers (Stacking):**\n"
        f"• 50 Paid Referrals: +7 Days Pro\n"
        f"• 100 Paid Referrals: +15 Days Pro\n"
        f"• 250 Paid Referrals: +30 Days Creator\n"
        f"• 500 Paid Referrals: ₹500 Credit"
    )
    buttons = [
        [InlineKeyboardButton("🏆 Top 10 Leaderboard", callback_data="ref:lead")],
        [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
    ]
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def handle_callbacks(query: CallbackQuery, user_id: int, action: str, parts: list, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Dispatches all billing, payment, and credit callbacks."""

    # 1. Switch Active Payment Method (Jin Feature)
    if action == "pay" and parts[1] == "select_method":
        cur_m = get_user_pay_method(user_id)
        text = (
            f"💳 **Select Your Preferred Payment Method**\n\n"
            f"Choose once, and all plans, durations, and credit prices will automatically display in this currency.\n\n"
            f"Currently Active: **{cur_m.upper()}**"
        )
        buttons = [
            [InlineKeyboardButton("🇮🇳 UPI (INR ₹)", callback_data="pay:set_method:upi")],
            [InlineKeyboardButton("🪙 Crypto via Oxapay (USD $)", callback_data="pay:set_method:crypto")],
            [InlineKeyboardButton("⭐ Telegram Stars", callback_data="pay:set_method:stars")],
            [InlineKeyboardButton("◀️ Back to Plans", callback_data="nav:plans")],
        ]
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest:
            pass
        return True

    if action == "pay" and parts[1] == "set_method":
        new_method = parts[2]
        set_user_pay_method(user_id, new_method)
        await query.answer(f"Payment method set to {new_method.upper()}!", show_alert=True)
        return await render_plans_view(query.message, user_id, edit=True)

    # 2. Plan Duration Picker (Formatted in User's Preferred Currency)
    if action == "pay" and parts[1] == "plan":
        plan = parts[2].upper()
        active_m = get_user_pay_method(user_id)
        options = _get_clean_durations(plan)
        query_p = "BEGINNER" if plan == "STARTER" else plan

        method_labels = {
            "upi": "🇮🇳 UPI (INR)",
            "crypto": "🪙 Crypto (USD)",
            "stars": "⭐ Telegram Stars"
        }

        text = (
            f"💎 **{plan} Plan Subscription**\n\n"
            f"Active Method: **{method_labels.get(active_m, 'UPI')}**\n"
            f"Select duration (discounts applied automatically):"
        )
        buttons = []
        dur_emojis = {1: "🗓️", 3: "✨", 6: "🔥", 12: "👑"}

        for opt in options:
            m = int(opt["months"])
            disc = float(opt.get("discount_percent") or 0)
            emo = dur_emojis.get(m, "📅")

            if active_m == "crypto":
                p_usd = pricing_service.calculate_crypto_price(query_p, m, disc)
                label = f"{emo} {m} Mo — ${p_usd['price_usd']:.2f} (Save {disc:.0f}%)"
            elif active_m == "stars":
                base_s = plan_service.get_plan_stars_price(plan) or STARS_DEFAULT_PRICES.get(plan, 300)
                tot_s = int(base_s * m * (1 - disc / 100))
                label = f"{emo} {m} Mo — ⭐ {tot_s} (Save {disc:.0f}%)"
            else:
                p_inr = pricing_service.calculate_price(query_p, m, disc)
                label = f"{emo} {m} Mo — ₹{p_inr['price_inr']:.0f} (Save {disc:.0f}%)"

            buttons.append([InlineKeyboardButton(label, callback_data=f"pay:dur:{plan}:{m}")])

        buttons.append([InlineKeyboardButton("🔄 Change Payment Method", callback_data="pay:select_method")])
        buttons.append([InlineKeyboardButton("◀️ Back to Plans", callback_data="nav:plans")])
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest:
            pass
        return True

    # 3. Direct Checkout Execution
    if action == "pay" and parts[1] == "dur":
        plan, months = parts[2].upper(), int(parts[3])
        active_m = get_user_pay_method(user_id)
        query_p = "BEGINNER" if plan == "STARTER" else plan
        opts = _get_clean_durations(query_p)
        d_row = next((r for r in opts if r["months"] == months), {"discount_percent": 0})
        disc = float(d_row.get("discount_percent") or 0)

        # Checkout 1: UPI
        if active_m == "upi":
            p_inr = pricing_service.calculate_price(query_p, months, disc)
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO payment_requests(user_id, plan, months, method, amount_inr, status, purpose) VALUES(?, ?, ?, 'upi', ?, 'PENDING_PAYMENT', 'plan')",
                (user_id, plan, months, p_inr["price_inr"])
            )
            conn.commit()
            req_id = cur.lastrowid
            conn.close()

            text = (
                f"🇮🇳 **Pay via UPI**\n\n"
                f"Plan: **{plan} ({months} Month{'s' if months>1 else ''})**\n"
                f"Amount: **₹{p_inr['price_inr']:.0f}**\n"
                f"UPI ID: `{UPI_ID}`\n"
                f"Payee Name: **{UPI_PAYEE_NAME or 'ChannelFlow'}**\n\n"
                f"Instructions:\n"
                f"1. Pay exact amount using any UPI App (GPay/PhonePe/Paytm).\n"
                f"2. Tap '📸 Submit Screenshot' below to upload proof."
            )
            buttons = [
                [InlineKeyboardButton("📸 Submit Screenshot", callback_data=f"pay:proof:{req_id}")],
                [InlineKeyboardButton("🔄 Change Method", callback_data="pay:select_method"), InlineKeyboardButton("❌ Cancel", callback_data="nav:plans")]
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return True

        # Checkout 2: Crypto (Live Oxapay URL)
        if active_m == "crypto":
            p_usd = pricing_service.calculate_crypto_price(query_p, months, disc)
            amt_usd = p_usd["price_usd"]
            pay_link, track_id = None, None

            if OXAPAY_API_KEY:
                try:
                    async with httpx.AsyncClient(timeout=20) as http:
                        resp = await http.post("https://api.oxapay.com/merchants/request", json={
                            "merchant": OXAPAY_API_KEY,
                            "amount": amt_usd,
                            "currency": "USD",
                            "lifeTime": 30,
                            "orderId": f"CF-{user_id}-{plan}-{months}m",
                            "description": f"ChannelFlow {plan} {months}m"
                        })
                        d = resp.json()
                        if d.get("result") == 100:
                            pay_link = d.get("payLink")
                            track_id = d.get("trackId")
                except Exception as ex:
                    logger.warning("Oxapay invoice error: %s", ex)

            conn = get_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO payment_requests(user_id, plan, months, method, amount_usd, status, purpose, payment_reference, provider_payment_id) 
                VALUES(?, ?, ?, 'crypto', ?, 'PENDING_PAYMENT', 'plan', ?, ?)
            """, (user_id, plan, months, amt_usd, pay_link, track_id))
            conn.commit()
            req_id = cur.lastrowid
            conn.close()

            if pay_link:
                text = (
                    f"🪙 **Crypto Checkout via Oxapay**\n\n"
                    f"Plan: **{plan} ({months} Month{'s' if months>1 else ''})**\n"
                    f"Total: **${amt_usd:.2f} USD**\n\n"
                    f"Tap '💳 Open Payment Link' below to complete your checkout in browser/wallet.\n"
                    f"Once done, tap '🔄 Check Status' to activate."
                )
                buttons = [
                    [InlineKeyboardButton("💳 Open Payment Link", url=pay_link)],
                    [InlineKeyboardButton("🔄 Check Status", callback_data=f"pay:check_oxapay:{req_id}")],
                    [InlineKeyboardButton("📸 Submit Screenshot", callback_data=f"pay:proof:{req_id}")],
                    [InlineKeyboardButton("🔄 Change Method", callback_data="pay:select_method")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="nav:plans")]
                ]
            else:
                text = (
                    f"🪙 **Crypto Payment**\n\n"
                    f"Plan: **{plan} ({months} Month{'s' if months>1 else ''})**\n"
                    f"Amount: **${amt_usd:.2f} USD**\n\n"
                    f"Submit your payment screenshot proof below:"
                )
                buttons = [
                    [InlineKeyboardButton("📸 Submit Screenshot", callback_data=f"pay:proof:{req_id}")],
                    [InlineKeyboardButton("🔄 Change Method", callback_data="pay:select_method")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="nav:plans")]
                ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return True

        # Checkout 3: Native Telegram Stars (XTR)
        if active_m == "stars":
            base_s = plan_service.get_plan_stars_price(plan) or STARS_DEFAULT_PRICES.get(plan, 300)
            tot_s = int(base_s * months * (1 - disc / 100))
            month_label = "Months" if months > 1 else "Month"
            title = f"{plan.capitalize()} Plan ({months} {month_label})"
            desc = f"ChannelFlow AI — {plan.capitalize()} subscription for {months * 30} days. Activates instantly after payment."

            try:
                await context.bot.send_invoice(
                    chat_id=user_id,
                    title=title,
                    description=desc,
                    payload=f"stars_sub:{user_id}:{plan}:{months}:{tot_s}",
                    currency="XTR",  # Official Telegram Stars currency code
                    prices=[LabeledPrice(label=title, amount=tot_s)],
                    provider_token="",  # Empty string for native Stars
                )
            except Exception as e:
                logger.exception("Failed to send Stars invoice: %s", e)
                await query.message.reply_text(f"⚠️ Stars Invoice Error: {e}")
            return True

    # 4. Oxapay Polling Status
    if action == "pay" and parts[1] == "check_oxapay":
        req_id = int(parts[2])
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT provider_payment_id, status FROM payment_requests WHERE id=?", (req_id,))
        row = cur.fetchone()
        conn.close()

        if row and row["provider_payment_id"] and OXAPAY_API_KEY:
            try:
                async with httpx.AsyncClient(timeout=15) as http:
                    r = await http.post(
                        "https://api.oxapay.com/merchants/inquiry",
                        json={"merchant": OXAPAY_API_KEY, "trackId": row["provider_payment_id"]}
                    )
                    res = r.json()
                    st = (res.get("status") or "").lower()
                    if st in ("paid", "confirmed"):
                        payment_service.approve_crypto_payment(req_id)
                        await query.message.reply_text("✅ Payment Verified! Your subscription is now ACTIVE.")
                        return True
                    else:
                        await query.answer(f"Status: {st.upper() or 'WAITING'}", show_alert=True)
                        return True
            except Exception:
                pass
        await query.answer("Payment still awaiting network confirmation.", show_alert=True)
        return True

    # 5. Screenshot Proof Trigger
    if action == "pay" and parts[1] == "proof":
        req_id = int(parts[2])
        WAITING_PAYMENT_SCREENSHOT[user_id] = req_id
        await query.message.reply_text("📸 Send the screenshot of your payment receipt now:")
        return True

    # 6. Extra Credits Store (Multi-Currency)
    if action == "cred" and parts[1] == "store":
        c_bal = get_credits_balance(user_id)
        active_m = get_user_pay_method(user_id)
        text = (
            f"📦 **Extra Forwarding Credits Store**\n\n"
            f"• Current Balance: **{c_bal} Credits**\n"
            f"• Active Currency: **{active_m.upper()}**\n\n"
            f"Credits are used only after your daily plan quota exhausts. Never expires!\n\n"
            f"Select a package below:"
        )
        buttons = []
        for units, p_dict in CREDITS_PRICING.items():
            if active_m == "crypto":
                label = f"⚡ {units:,} Credits — ${p_dict['usd']:.2f}"
            elif active_m == "stars":
                label = f"⚡ {units:,} Credits — ⭐ {p_dict['stars']}"
            else:
                label = f"⚡ {units:,} Credits — ₹{p_dict['inr']}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"cred:buy:{units}")])

        buttons.append([InlineKeyboardButton("🔄 Change Payment Method", callback_data="pay:select_method")])
        buttons.append([InlineKeyboardButton("◀️ Back to Plans", callback_data="nav:plans")])
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest:
            pass
        return True

    if action == "cred" and parts[1] == "buy":
        units = int(parts[2])
        active_m = get_user_pay_method(user_id)
        p_dict = CREDITS_PRICING.get(units, {"inr": 99, "usd": 1.20, "stars": 75})

        if active_m == "upi":
            price = p_dict["inr"]
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO payment_requests(user_id, plan, months, method, amount_inr, status, purpose) VALUES(?, ?, 1, 'upi', ?, 'PENDING_PAYMENT', 'credits')",
                (user_id, f"{units} Credits", price)
            )
            conn.commit()
            req_id = cur.lastrowid
            conn.close()

            text = (
                f"📦 **Order {units:,} Credits via UPI**\n\n"
                f"Amount: **₹{price}**\n"
                f"UPI ID: `{UPI_ID}`\n"
                f"Payee: **{UPI_PAYEE_NAME or 'ChannelFlow'}**\n\n"
                f"Submit your screenshot proof below after paying."
            )
            buttons = [
                [InlineKeyboardButton("📸 Submit Screenshot", callback_data=f"pay:proof:{req_id}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cred:store")]
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return True

        if active_m == "crypto":
            price = p_dict["usd"]
            pay_link, track_id = None, None
            if OXAPAY_API_KEY:
                try:
                    async with httpx.AsyncClient(timeout=20) as http:
                        resp = await http.post("https://api.oxapay.com/merchants/request", json={
                            "merchant": OXAPAY_API_KEY,
                            "amount": price,
                            "currency": "USD",
                            "lifeTime": 30,
                            "orderId": f"CF-{user_id}-{units}c",
                            "description": f"{units} Credits"
                        })
                        d = resp.json()
                        if d.get("result") == 100:
                            pay_link = d.get("payLink")
                            track_id = d.get("trackId")
                except Exception:
                    pass

            conn = get_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO payment_requests(user_id, plan, months, method, amount_usd, status, purpose, payment_reference, provider_payment_id) 
                VALUES(?, ?, 1, 'crypto', ?, 'PENDING_PAYMENT', 'credits', ?, ?)
            """, (user_id, f"{units} Credits", price, pay_link, track_id))
            conn.commit()
            req_id = cur.lastrowid
            conn.close()

            if pay_link:
                text = (
                    f"🪙 **Order {units:,} Credits via Oxapay**\n\n"
                    f"Total: **${price:.2f} USD**\n\n"
                    f"Tap '💳 Open Payment Link' below to complete your checkout:"
                )
                buttons = [
                    [InlineKeyboardButton("💳 Open Payment Link", url=pay_link)],
                    [InlineKeyboardButton("🔄 Check Status", callback_data=f"pay:check_oxapay:{req_id}")],
                    [InlineKeyboardButton("📸 Submit Screenshot", callback_data=f"pay:proof:{req_id}")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cred:store")]
                ]
            else:
                text = (
                    f"🪙 **Order {units:,} Credits**\n\n"
                    f"Total: **${price:.2f} USD**\n\n"
                    f"Submit payment proof screenshot below:"
                )
                buttons = [
                    [InlineKeyboardButton("📸 Submit Screenshot", callback_data=f"pay:proof:{req_id}")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cred:store")]
                ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return True

        if active_m == "stars":
            price_stars = p_dict["stars"]
            title = f"{units:,} Extra Forwarding Credits"
            desc = f"Top-up {units:,} extra forwarding credits for ChannelFlow AI. Instant delivery."
            try:
                await context.bot.send_invoice(
                    chat_id=user_id,
                    title=title,
                    description=desc,
                    payload=f"stars_credits:{user_id}:{units}:{price_stars}",
                    currency="XTR",
                    prices=[LabeledPrice(label="Credits", amount=price_stars)],
                    provider_token="",
                )
            except Exception as e:
                await query.message.reply_text(f"⚠️ Stars Invoice Error: {e}")
            return True

    # 7. Referrals Leaderboard (Top 10)
    if action == "ref" and parts[1] == "lead":
        board = referral_service.get_referral_leaderboard(limit=10)
        text = "🏆 **Top 10 ChannelFlow Creators**\n\n"
        if not board:
            text += "No referral conversions recorded yet. Share your personal link to be first!"
        else:
            for idx, r in enumerate(board[:10], 1):
                medal = "🥇" if idx == 1 else ("🥈" if idx == 2 else ("🥉" if idx == 3 else f"{idx}."))
                text += f"{medal} User #{r['user_id']} — {r['rewarded']} paid ({r['total']} invited)\n"
        buttons = [[InlineKeyboardButton("◀️ Back to Rewards", callback_data="nav:earn")]]
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest:
            pass
        return True

    return False


# ==========================================
# TELEGRAM STARS NATIVE CALLBACKS (PRD §26)
# ==========================================

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answers Telegram PreCheckout query to display the official purchase confirmation sheet."""
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Instantly activates plans and credits upon Stars checkout completion."""
    msg = update.effective_message
    if not msg or not msg.successful_payment:
        return

    payment = msg.successful_payment
    payload = payment.invoice_payload or ""
    parts = payload.split(":")

    if payload.startswith("stars_sub:"):
        _, uid_str, plan, months_str, _ = parts
        uid = int(uid_str)
        months = int(months_str)

        from datetime import datetime, timedelta, timezone
        exp = (datetime.now(timezone.utc) + timedelta(days=30 * months)).isoformat()
        plan_service.set_user_plan(uid, plan, expiry=exp)

        await msg.reply_text(
            f"🎉 **Payment Successful!**\n\n"
            f"Your **{plan.capitalize()} Plan** is now ACTIVE for {months * 30} days.\n"
            f"Total Paid: ⭐ {payment.total_amount} Stars.",
        )
        return

    if payload.startswith("stars_credits:"):
        _, uid_str, units_str, _ = parts
        uid = int(uid_str)
        units = int(units_str)

        adjust_credits(uid, units, f"stars:{payment.telegram_payment_charge_id}", "Telegram Stars Credits Purchase")

        await msg.reply_text(
            f"🎉 **Credits Added!**\n\n"
            f"Successfully added **{units:,} Extra Forwarding Credits** to your account.\n"
            f"Total Paid: ⭐ {payment.total_amount} Stars.",
        )
        return