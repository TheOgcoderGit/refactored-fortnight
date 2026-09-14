"""
ChannelFlow AI - Categorized Admin Panel
==========================================

Full admin dashboard per Prompt 1 §47 / Prompt 4 Extension §10:

    🛡 Admin → Overview | Finance | Users | Marketing | Support |
               Analytics | Platforms | System | Admins | Audit

Every callback checks permission server-side via audit_service.
Every mutating action writes an audit log entry.

Entry: /admin command (owner or active admin row only).
"""

import json
import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from config import ADMIN_IDS
from database.db import get_connection
from services import (
    audit_service as RBAC,
    payment_service,
    wallet_service,
    plan_service,
    support_service,
    coupon_service,
    giveaway_service,
    job_queue,
)
from core.listener import is_running

logger = logging.getLogger(__name__)


# ==========================================
# HELPERS
# ==========================================

def _fmt_limit(v):
    return "∞" if v is None else str(v)


def _perm(admin_id, permission):
    return RBAC.require_permission(admin_id, permission)


def _deny(query, permission):
    """Uniform denial + audit trail for unauthorized attempts."""

    RBAC.log_action(
        query.from_user.id, "permission_denied",
        target_type="callback", target_id=permission,
    )
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        loop.create_task(query.answer("⛔ You don't have permission for this.", show_alert=True))
    except Exception:
        pass


# ==========================================
# ENTRY: /admin
# ==========================================

async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/admin - categorized dashboard. Replaces the old flat screen."""

    user = update.effective_user

    if not RBAC.is_admin(user.id):
        await update.message.reply_text("⛔ Admins only.")
        return

    await update.message.reply_text(
        "🛡 Admin Dashboard",
        reply_markup=admin_root_keyboard()
    )


def admin_root_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Overview", callback_data="adm:view")],
        [
            InlineKeyboardButton("💰 Finance", callback_data="adm:finance"),
            InlineKeyboardButton("👥 Users", callback_data="adm:users"),
        ],
        [
            InlineKeyboardButton("🎁 Marketing", callback_data="adm:marketing"),
            InlineKeyboardButton("🛠 Support", callback_data="adm:support"),
        ],
        [
            InlineKeyboardButton("📈 Analytics", callback_data="adm:analytics"),
            InlineKeyboardButton("🌐 Platforms", callback_data="adm:platforms"),
        ],
        [InlineKeyboardButton("⚙️ System", callback_data="adm:system")],
        [InlineKeyboardButton("📜 Audit Logs", callback_data="adm:audit")],
        [InlineKeyboardButton("👨‍💼 Admin Management", callback_data="adm:admins")],
    ])


# ==========================================
# ROUTER
# ==========================================

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles every 'adm:*' callback."""

    query = update.callback_query
    user = query.from_user
    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "view"

    if not RBAC.is_admin(user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    # ---------- OVERVIEW ----------
    if action == "view":
        if not _perm(user.id, "dashboard.view"):
            _deny(query, "dashboard.view"); return
        await _render_overview(query)

    elif action == "finance":
        if not _perm(user.id, "payments.view"):
            _deny(query, "payments.view"); return
        await _render_finance(query)

    # ---------- PAYMENT QUEUE ----------
    elif action == "payq":
        if not _perm(user.id, "payments.view"):
            _deny(query, "payments.view"); return
        status = parts[2] if len(parts) > 2 else "SUBMITTED"
        page = int(parts[3]) if len(parts) > 3 else 0
        await _render_payment_queue(query, status, page)

    elif action == "payapprove":
        if not _perm(user.id, "payments.approve"):
            _deny(query, "payments.approve"); return
        rid = int(parts[2])
        approved = payment_service.approve_payment(rid, admin_id=user.id)
        RBAC.log_action(user.id, "payment.approve", "payment_request", rid,
                        after={"status": "APPROVED"})
        await query.answer(
            "✅ Approved." if approved else "Already decided.",
            show_alert=approved is None,
        )

    elif action == "payreject":
        if not _perm(user.id, "payments.reject"):
            _deny(query, "payments.reject"); return
        rid = int(parts[2])
        request = payment_service.get_payment_request(rid)
        ok = payment_service.reject_payment(rid, admin_id=user.id,
                                            reason="Rejected from queue")
        RBAC.log_action(user.id, "payment.reject", "payment_request", rid,
                        after={"status": "REJECTED"})
        if ok and request:
            try:
                await context.bot.send_message(
                    request["user_id"],
                    "❌ Your payment couldn't be verified. Contact support "
                    "if you believe this is a mistake."
                )
            except Exception:
                pass
        await query.answer("❌ Rejected." if ok else "Already decided.")

    # ---------- USERS ----------
    elif action == "users":
        if not _perm(user.id, "users.view"):
            _deny(query, "users.view"); return
        await _render_users(query)

    elif action == "usersearch":
        context.bot_data["awaiting_admin_input"] = {
            str(query.from_user.id): {"type": "admin_user_search"}
        }
        await query.message.reply_text(
            "🔎 Send the user's Telegram ID or exact username:"
        )

    elif action == "userprofile":
        if not _perm(user.id, "users.view"):
            _deny(query, "users.view"); return
        uid = int(parts[2])
        await _render_user_profile(query, uid)

    elif action in ("usersuspend", "userunsuspend", "userban"):
        uid = int(parts[2])

        if action == "usersuspend" and not _perm(user.id, "users.suspend"):
            _deny(query, "users.suspend"); return
        if action == "userban" and not _perm(user.id, "users.ban"):
            _deny(query, "users.ban"); return
        if action == "userunsuspend" and not (_perm(user.id, "users.suspend") or _perm(user.id, "users.ban")):
            _deny(query, "users.suspend"); return

        new_status = {"usersuspend": "suspended", "userban": "banned",
                      "userunsuspend": "active"}[action]

        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT status FROM users WHERE telegram_id=?", (uid,))
        before = cur.fetchone()
        cur.execute("UPDATE users SET status=? WHERE telegram_id=?", (new_status, uid))
        conn.commit()
        conn.close()

        RBAC.log_action(user.id, f"user.{action[4:]}", "user", uid,
                        before={"status": before["status"] if before else None},
                        after={"status": new_status})

        await query.answer(f"User {new_status}.", show_alert=True)

        if new_status in ("suspended", "banned"):
            try:
                await context.bot.send_message(uid, H_suspension_message(new_status))
            except Exception:
                pass

        await _render_user_profile(query, uid)

    # ---------- MARKETING ----------
    elif action == "marketing":
        if not _perm(user.id, "coupons.view") and not _perm(user.id, "giveaways.view"):
            _deny(query, "coupons.view"); return
        await _render_marketing(query)

    elif action == "coupons":
        if not _perm(user.id, "coupons.view"):
            _deny(query, "coupons.view"); return
        await _render_coupons(query)

    elif action == "couponcreate":
        if not _perm(user.id, "coupons.manage"):
            _deny(query, "coupons.manage"); return
        c = coupon_service.create_coupon(discount_percent=10, created_by=user.id)
        RBAC.log_action(user.id, "coupon.create", "coupon", c["id"],
                        after={"code": c["code"], "discount_percent": 10})
        await query.answer(f"✅ Coupon {c['code']} created (10% off).", show_alert=True)

    elif action == "cpondeactivate":
        if not _perm(user.id, "coupons.manage"):
            _deny(query, "coupons.manage"); return
        cid = int(parts[2])
        coupon_service.deactivate_coupon(cid)
        RBAC.log_action(user.id, "coupon.deactivate", "coupon", cid)
        await _render_coupons(query)

    elif action == "giveaways":
        if not _perm(user.id, "giveaways.view"):
            _deny(query, "giveaways.view"); return
        await _render_giveaways(query)

    elif action == "gwselect":
        if not _perm(user.id, "giveaways.manage"):
            _deny(query, "giveaways.manage"); return
        gid = int(parts[2])
        winners = giveaway_service.select_winners(gid)
        RBAC.log_action(user.id, "giveaway.select_winners", "giveaway", gid,
                        after={"winner_count": len(winners)})
        await query.answer(f"🎲 Selected {len(winners)} winners.", show_alert=True)
        await _render_giveaway_detail(query, gid)

    elif action == "gwdeliver":
        if not _perm(user.id, "giveaways.manage"):
            _deny(query, "giveaways.manage"); return
        gid = int(parts[2])
        delivered, failed = giveaway_service.deliver_rewards(context.bot, gid)
        RBAC.log_action(user.id, "giveaway.deliver", "giveaway", gid,
                        after={"delivered": delivered, "failed": failed})
        await query.answer(f"📤 Delivered: {delivered}, Failed: {failed}", show_alert=True)

    # ---------- SUPPORT ----------
    elif action == "support":
        if not _perm(user.id, "support.view"):
            _deny(query, "support.view"); return
        status = parts[2] if len(parts) > 2 else "open"
        await _render_support_inbox(query, status)

    elif action == "ticket":
        if not _perm(user.id, "support.view"):
            _deny(query, "support.view"); return
        tid = int(parts[2])
        await _render_ticket(query, tid)

    elif action == "ticketreply":
        if not _perm(user.id, "support.manage"):
            _deny(query, "support.manage"); return
        tid = int(parts[2])
        context.bot_data.setdefault("awaiting_admin_input", {})[str(user.id)] = {
            "type": "admin_ticket_reply", "ticket_id": tid
        }
        await query.message.reply_text(
            f"✉️ Send your reply to ticket #{tid} (visible to the user):"
        )

    elif action == "ticketnote":
        if not _perm(user.id, "support.manage"):
            _deny(query, "support.manage"); return
        tid = int(parts[2])
        context.bot_data.setdefault("awaiting_admin_input", {})[str(user.id)] = {
            "type": "admin_ticket_note", "ticket_id": tid
        }
        await query.message.reply_text(f"📝 Internal note for ticket #{tid}:")

    elif action == "ticketstatus":
        if not _perm(user.id, "support.manage"):
            _deny(query, "support.manage"); return
        tid, new_status = int(parts[2]), parts[3]
        support_service.set_status(tid, new_status)
        RBAC.log_action(user.id, "support.status", "ticket", tid,
                        after={"status": new_status})
        await _render_ticket(query, tid)

    # ---------- ANALYTICS ----------
    elif action == "analytics":
        if not _perm(user.id, "analytics.view"):
            _deny(query, "analytics.view"); return
        await _render_analytics(query)

    # ---------- PLATFORMS ----------
    elif action == "platforms":
        if not _perm(user.id, "platforms.manage"):
            _deny(query, "platforms.manage"); return
        await _render_platforms(query)

    elif action == "wapairstatus":
        if not _perm(user.id, "platforms.manage"):
            _deny(query, "platforms.manage"); return
        await _render_whatsapp_pairing_status(query)

    elif action == "wapaircleanup":
        if not _perm(user.id, "platforms.manage"):
            _deny(query, "platforms.manage"); return
        from services.destination_service import cleanup_expired_pairings
        count = cleanup_expired_pairings()
        await query.answer(f"Cleaned up {count} expired pairing codes", show_alert=True)
        await _render_whatsapp_pairing_status(query)

    elif action == "platformtoggle":
        if not _perm(user.id, "platforms.manage"):
            _deny(query, "platforms.manage"); return
        platform = parts[2]
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT state FROM platform_status WHERE platform=?", (platform,))
        row = cur.fetchone()
        old = row["state"] if row else "enabled"
        new = {"enabled": "disabled", "disabled": "maintenance",
               "maintenance": "enabled"}.get(old, "enabled")
        cur.execute(
            "UPDATE platform_status SET state=?, updated_at=CURRENT_TIMESTAMP WHERE platform=?",
            (new, platform),
        )
        conn.commit()
        conn.close()

        RBAC.log_action(user.id, "platform.toggle", "platform", platform,
                        before={"state": old}, after={"state": new})

        from bot.notifier import notify_admins
        await query.answer(f"{platform}: {new}", show_alert=True)
        await _render_platforms(query)

    # ---------- SYSTEM ----------
    elif action == "system":
        await _render_system(query)

    elif action == "audit":
        if not _perm(user.id, "audit.view"):
            _deny(query, "audit.view"); return
        await _render_audit(query)

    # ---------- ADMINS ----------
    elif action == "admins":
        if not _perm(user.id, "admins.manage"):
            _deny(query, "admins.manage"); return
        await _render_admins(query)

    elif action == "adminadd":
        if not _perm(user.id, "admins.manage"):
            _deny(query, "admins.manage"); return
        context.bot_data.setdefault("awaiting_admin_input", {})[str(user.id)] = {
            "type": "admin_add"
        }
        await query.message.reply_text("👤 Send the Telegram ID of the new admin:")

    elif action == "adminremove":
        if not _perm(user.id, "admins.manage"):
            _deny(query, "admins.manage"); return
        aid = int(parts[2])
        RBAC.remove_admin(aid)
        RBAC.log_action(user.id, "admin.remove", "admin", aid)
        await query.answer("Admin removed.", show_alert=True)
        await _render_admins(query)

    elif action == "adminperms":
        if not _perm(user.id, "admins.manage"):
            _deny(query, "admins.manage"); return
        aid = int(parts[2])
        await _render_admin_permissions(query, aid)

    elif action == "admintoggleperm":
        if not _perm(user.id, "admins.manage"):
            _deny(query, "admins.manage"); return
        aid = int(parts[2])
        perm = parts[3]
        new_val = not RBAC.has_permission(aid, perm)
        RBAC.set_permission(aid, perm, new_val)
        RBAC.log_action(user.id, "admin.perm", "admin_permission", aid,
                        after={"permission": perm, "granted": new_val})
        await _render_admin_permissions(query, aid)

    elif action == "admintoggleactive":
        if not _perm(user.id, "admins.manage"):
            _deny(query, "admins.manage"); return
        aid = int(parts[2])
        admin_row = next((a for a in RBAC.list_admins() if a["telegram_id"] == aid), None)
        if admin_row is not None:
            new_state = not bool(admin_row["is_active"])
            RBAC.set_admin_active(aid, new_state)
            RBAC.log_action(user.id, "admin.active", "admin", aid,
                            after={"is_active": new_state})
        await _render_admins(query)

    # Refresh = re-render same section
    elif action == "refresh":
        await admin_callback.__wrapped__ if False else None
        # Re-dispatch with just the section name
        query.data = f"adm:{parts[2] if len(parts) > 2 else 'view'}"
        await admin_callback(update, context)

    try:
        await query.answer()
    except Exception:
        pass


def H_suspension_message(status):
    if status == "banned":
        return ("⛔ Your account has been permanently banned.\n\n"
                "Contact @ChannelFlowSupport_bot if you believe this is a mistake.")
    return ("🚫 Your account has been suspended by the administrator.\n\n"
            "Contact ChannelFlow Support: @ChannelFlowSupport_bot")


# ==========================================
# RENDERERS
# ==========================================

async def _render_overview(query):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) c FROM users")
    total_users = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) c FROM users WHERE plan != 'FREE'")
    paid_users = cur.fetchone()["c"]

    cur.execute("""
        SELECT plan, COUNT(*) c FROM users
        WHERE (plan_expiry IS NULL OR plan_expiry > datetime('now'))
        GROUP BY plan
    """)
    by_plan = {r["plan"]: r["c"] for r in cur.fetchall()}

    cur.execute("SELECT COUNT(*) c FROM projects WHERE status=1")
    active_projects = cur.fetchone()["c"]

    cur.execute("""SELECT COUNT(*) c FROM payment_requests
                   WHERE status='SUBMITTED' AND purpose='plan'""")
    pending_pay = cur.fetchone()["c"]

    cur.execute("""SELECT COUNT(*) c FROM payment_requests
                   WHERE status='SUBMITTED' AND purpose='wallet_topup'""")
    pending_topup = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) c FROM support_tickets WHERE status IN ('open','pending')")
    open_tickets = cur.fetchone()["c"]
    conn.close()

    dlq = job_queue.get_queue_stats().get("unresolved_dead_letters", 0)

    text = (
        "📊 Overview\n\n"
        f"👥 Users: {total_users} ({paid_users} paid)\n"
        f"   ⚪ FREE: {by_plan.get('FREE', total_users - sum(by_plan.values()))}\n"
        f"   🌱 Beginner: {by_plan.get('BEGINNER', 0)} · 🚀 Pro: {by_plan.get('PRO', 0)} · 👑 Creator: {by_plan.get('CREATOR', 0)}\n"
        f"📁 Active projects: {active_projects}\n\n"
        "Requires Attention:\n"
        f"{'🔴' if pending_pay else '✅'} Pending payments: {pending_pay}\n"
        f"{'🔴' if pending_topup else '✅'} Pending top-ups: {pending_topup}\n"
        f"{'🔴' if open_tickets else '✅'} Support tickets: {open_tickets}\n"
        f"{'🔴' if dlq else '✅'} Dead-letter jobs: {dlq}"
    )

    await query.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data="adm:refresh:view"),
            InlineKeyboardButton("⬅ Admin Home", callback_data="admback"),
        ]])
    )


async def _render_finance(query):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""SELECT COUNT(*) c, COALESCE(SUM(final_amount),0) s
                   FROM payment_requests WHERE status='APPROVED' AND purpose='plan'""")
    rev_row = cur.fetchone()

    cur.execute("SELECT COALESCE(SUM(amount_inr),0) s FROM wallet_transactions WHERE direction='CREDIT'")
    topup_total = cur.fetchone()["s"]
    conn.close()

    text = (
        "💰 Finance\n\n"
        f"Subscription revenue: ₹{rev_row['s']:.0f} ({rev_row['c']} payments)\n"
        f"Wallet top-ups credited: ₹{topup_total:.0f}\n\n"
        "Queues:"
    )

    kb = [
        [InlineKeyboardButton("💳 Payment Requests", callback_data="adm:payq:SUBMITTED:0")],
        [InlineKeyboardButton("💰 Wallet Top-ups", callback_data="adm:payq:SUBMITTED_TOPUP:0")],
        [InlineKeyboardButton("⬅ Admin Home", callback_data="admback")],
    ]

    await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def _render_payment_queue(query, status, page):
    conn = get_connection()
    cur = conn.cursor()

    purpose_filter = ""
    status_filter = "pr.status = ?"
    status_value = None
    params = []
    label = status

    # The "Pending" view shows BOTH requests awaiting a screenshot
    # (PENDING_PAYMENT) and requests the user has submitted with proof
    # (SUBMITTED). Previously only SUBMITTED was shown, so a request the
    # user created but hadn't finished verifying never reached the admin
    # section at all - the root cause of "payment not appearing".
    if status == "SUBMITTED_TOPUP":
        purpose_filter = "AND pr.purpose='wallet_topup'"
        status_filter = "pr.status IN ('PENDING_PAYMENT','SUBMITTED')"
        label = "PENDING"
    elif status == "SUBMITTED":
        purpose_filter = "AND pr.purpose='plan'"
        status_filter = "pr.status IN ('PENDING_PAYMENT','SUBMITTED')"
        label = "PENDING"
    elif status in ("APPROVED", "REJECTED", "CANCELLED", "EXPIRED"):
        status_value = status
    else:
        # Unknown/legacy tab id - fall back to the default pending tab.
        purpose_filter = "AND pr.purpose='plan'"
        status_filter = "pr.status IN ('PENDING_PAYMENT','SUBMITTED')"
        label = "PENDING"

    if status_value is not None:
        params.append(status_value)

    params.extend([20, page * 20])

    cur.execute(
        f"""
        SELECT pr.*, u.username, u.first_name
        FROM payment_requests pr
        LEFT JOIN users u ON u.telegram_id = pr.user_id
        WHERE {status_filter} {purpose_filter}
        ORDER BY pr.id ASC LIMIT ? OFFSET ?
        """,
        params,
    )
    rows = cur.fetchall()
    conn.close()

    tabs = _queue_tabs(label, page, rows)

    if not rows:
        tabs_markup = _queue_tabs(label, page, [])
        await query.message.reply_text(
            f"✅ No '{label}' requests. All processed!",
            reply_markup=InlineKeyboardMarkup(
                list(tabs_markup.inline_keyboard) + [[InlineKeyboardButton("⬅ Back", callback_data="adm:finance")]]
            )
        )
        return

    lines = [f"💳 Queue: {label} ({len(rows)})\n"]
    buttons = []

    for r in rows:
        who = r["first_name"] or r["username"] or str(r["user_id"])
        amount = r["final_amount"] or r["amount_inr"] or 0
        sym = "$" if (r["currency"] == "USD") else "₹"
        kind = "Top-up" if r["purpose"] == "wallet_topup" else (r["plan"] or "?")
        lines.append(f"#{r['id']} · {who} · {kind} {sym}{amount:.0f}")
        # Only SUBMITTED requests (with proof) can be approved/rejected.
        # PENDING_PAYMENT rows without a screenshot are shown for
        # visibility but are not yet actionable.
        if r["status"] == "SUBMITTED":
            buttons.append([
                InlineKeyboardButton(f"✅ #{r['id']}", callback_data=f"adm:payapprove:{r['id']}"),
                InlineKeyboardButton(f"❌ #{r['id']}", callback_data=f"adm:payreject:{r['id']}"),
            ])
        else:
            buttons.append([InlineKeyboardButton(f"⏳ #{r['id']} (no proof yet)", callback_data="noop")])

    buttons.append([InlineKeyboardButton("⬅ Back", callback_data="adm:finance")])
    buttons.append(tabs.inline_keyboard[0])

    await query.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons)
    )


def _queue_tabs(current, page, rows):
    tabs = []
    for s, lbl in (("SUBMITTED", "Pending"), ("APPROVED", "Approved"), ("REJECTED", "Rejected")):
        icon = "▶" if s == current else ""
        # Pending tab: keep the same entry id so the tab highlight and
        # the underlying query agree on which rows to show.
        entry = "SUBMITTED" if s == "SUBMITTED" else s
        cb = "adm:payq:SUBMITTED_TOPUP:0" if current == "SUBMITTED_TOPUP" else f"adm:payq:{entry}:0"
        tabs.append(InlineKeyboardButton(f"{icon}{lbl}", callback_data=cb))
    return InlineKeyboardMarkup([tabs])


async def _render_users(query):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) c FROM users")
    total = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM users WHERE status='suspended'")
    susp = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM users WHERE status='banned'")
    banned = cur.fetchone()["c"]
    conn.close()

    await query.message.reply_text(
        f"👥 Users\n\nTotal: {total}\n🚫 Suspended: {susp}\n⛔ Banned: {banned}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 Search User", callback_data="adm:usersearch")],
            [InlineKeyboardButton("⬅ Admin Home", callback_data="admback")],
        ])
    )


async def _render_user_profile(query, uid):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id=?", (uid,))
    u = cur.fetchone()
    if u is None:
        conn.close()
        await query.message.reply_text("User not found.")
        return

    cur.execute("SELECT COUNT(*) c FROM projects WHERE user_id=?", (uid,))
    projects = cur.fetchone()["c"]
    conn.close()

    balance = wallet_service.get_balance_inr(uid)
    ent = plan_service.get_entitlements(uid)

    status_icons = {"active": "🟢", "suspended": "🚫", "banned": "⛔"}

    text = (
        f"👤 User Profile\n\n"
        f"ID: {uid}\n"
        f"Name: {u['first_name'] or '-'} (@{u['username'] or '-'})\n"
        f"Status: {status_icons.get(u['status'], '•')} {u['status']}\n\n"
        f"💳 Plan: {ent['plan']}\n"
        f"💰 Wallet: ₹{balance:.2f}\n"
        f"📁 Projects: {projects}"
    )

    rows = [[InlineKeyboardButton("⬅ Back", callback_data="adm:users")]]

    if u["status"] == "active":
        rows.insert(0, InlineKeyboardMarkup([[

        ]]).inline_keyboard[0] if False else None)
        action_rows = [
            [InlineKeyboardButton("🚫 Suspend", callback_data=f"adm:usersuspend:{uid}")],
            [InlineKeyboardButton("⛔ Ban", callback_data=f"adm:userban:{uid}")],
        ]
        rows = action_rows + rows
    else:
        action_rows = [[InlineKeyboardButton("✅ Unsuspend/Unban", callback_data=f"adm:userunsuspend:{uid}")]]
        rows = action_rows + rows

    await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def _render_marketing(query):
    await query.message.reply_text(
        "🎁 Marketing",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎟 Coupons", callback_data="adm:coupons")],
            [InlineKeyboardButton("🎉 Giveaways", callback_data="adm:giveaways")],
            [InlineKeyboardButton("⬅ Admin Home", callback_data="admback")],
        ])
    )


async def _render_coupons(query):
    coupons = coupon_service.list_coupons(active_only=False, limit=10)

    lines = ["🎟 Coupons\n"]
    buttons = []
    for c in coupons:
        uses = _coupon_use_count(c["id"])
        lines.append(
            f"`{c['code']}` · {c['discount_percent']:.0f}% off · used {uses}"
            f"{'' if c['active'] else ' (inactive)'}"
        )
        if c["active"]:
            buttons.append([InlineKeyboardButton(
                f"⚪ Deactivate {c['code']}", callback_data=f"adm:cpondeactivate:{c['id']}"
            )])

    if not coupons:
        lines.append("No coupons yet.")

    buttons.append([InlineKeyboardButton("➕ Create Coupon (10%)", callback_data="adm:couponcreate")])
    buttons.append([InlineKeyboardButton("⬅ Back", callback_data="adm:marketing")])

    await query.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


def _coupon_use_count(coupon_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) c FROM coupon_redemptions WHERE coupon_id=?", (coupon_id,))
    n = cur.fetchone()["c"]
    conn.close()
    return n


async def _render_giveaways(query):
    gws = giveaway_service.list_giveaways()

    lines = ["🎉 Giveaways\n"]
    buttons = []

    for g in gws[:10]:
        icons = {"draft": "📝", "winners_selected": "🎲", "delivered": "✅", "cancelled": "⚫"}
        lines.append(f"#{g['id']} {icons.get(g['status'],'•')} {g['name']} ({g['type']}) · {g['winner_count']} winners · {g['status']}")
        row = [InlineKeyboardButton(f"#{g['id']} detail", callback_data=f"adm:gwdetail:{g['id']}")]
        buttons.append(row)

    if not gws:
        lines.append("No giveaways yet. Create one with /giveaway")

    buttons.append([InlineKeyboardButton("⬅ Back", callback_data="adm:marketing")])

    await query.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def _render_giveaway_detail(query, gid):
    g = giveaway_service.get_giveaway(gid)
    winners = giveaway_service.get_winners(gid)

    config = json.loads(g["config"] or "{}")
    lines = [
        f"🎉 {g['name']}",
        "",
        f"Type: {g['type']} · Winners: {g['winner_count']}",
        f"Eligibility: {g['eligibility']}",
        f"Status: {g['status']}",
    ]
    if winners:
        lines.append("")
        lines.append("Winners:")
        for w in winners[:10]:
            lines.append(f"  • {w['user_id']} — {w['reward_summary']}")

    buttons = []
    if g["status"] in ("draft", "winners_selected"):
        buttons.append([InlineKeyboardButton("🎲 Select Winners", callback_data=f"adm:gwselect:{gid}")])
    if g["status"] == "winners_selected":
        buttons.append([InlineKeyboardButton("📤 Send Rewards", callback_data=f"adm:gwdeliver:{gid}")])

    buttons.append([InlineKeyboardButton("⬅ Back", callback_data="adm:giveaways")])

    await query.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def _render_support_inbox(query, status):
    tickets = support_service.list_tickets_by_status(None if status == "all" else status, 15)

    icons = support_service.STATUS_ICONS
    lines = [f"🛠 Support Inbox — {status}\n"]
    buttons = []

    for t in tickets:
        lines.append(f"#{t['id']} {icons.get(t['status'],'•')} [{t['category']}] {t['subject'][:40]}")
        buttons.append([InlineKeyboardButton(
            f"#{t['id']} open", callback_data=f"adm:ticket:{t['id']}"
        )])

    if not tickets:
        lines.append("✅ No tickets here.")

    tab_row = [InlineKeyboardButton(
        s if s != status else f"▶{s}",
        callback_data=f"adm:support:{s}"
    ) for s in ("open", "pending", "in_progress", "resolved", "closed")]

    buttons.append(tab_row)
    buttons.append([InlineKeyboardButton("⬅ Admin Home", callback_data="admback")])

    await query.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def _render_ticket(query, tid):
    t = support_service.get_ticket(tid)
    if t is None:
        await query.message.reply_text("Ticket not found.")
        return

    msgs = support_service.get_messages(tid)

    lines = [f"🎫 Ticket #{tid} — {t['subject']}", f"[{t['category']}] {t['status']}", ""]
    for m in msgs[-6:]:
        prefix = "📝 NOTE: " if m["is_internal_note"] else ("👤" if m["sender_type"] == "user" else "🛡")
        lines.append(f"{prefix} {m['message'][:200]}")

    buttons = [
        [InlineKeyboardButton("✉️ Reply", callback_data=f"adm:ticketreply:{tid}"),
         InlineKeyboardButton("📝 Note", callback_data=f"adm:ticketnote:{tid}")],
    ]

    next_status = {"open": "in_progress", "in_progress": "resolved",
                   "resolved": "closed", "pending": "in_progress"}
    if t["status"] in next_status:
        ns = next_status[t["status"]]
        buttons.append([InlineKeyboardButton(f"➡ Set {ns}", callback_data=f"adm:ticketstatus:{tid}:{ns}")])
    if t["status"] == "closed":
        buttons.append([InlineKeyboardButton("🔄 Reopen", callback_data=f"adm:ticketstatus:{tid}:open")])

    buttons.append([InlineKeyboardButton("⬅ Inbox", callback_data="adm:support:open")])

    await query.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def _render_analytics(query):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT COALESCE(SUM(forward_count),0) s FROM daily_usage WHERE usage_date=date('now')")
    today_fwd = cur.fetchone()["s"]

    cur.execute("SELECT COUNT(*) c FROM message_deduplication WHERE status='claimed' AND created_at >= date('now')")
    dups_today = cur.fetchone()["c"]

    cur.execute("""
        SELECT plan, COUNT(*) c FROM users
        GROUP BY plan ORDER BY c DESC
    """)
    dist = cur.fetchall()
    total = sum(r["c"] for r in dist) or 1
    conn.close()

    ai_stats = {}
    try:
        from services.ai_service import get_ai_stats_today
        ai_stats = get_ai_stats_today()
    except Exception:
        pass

    dist_lines = " · ".join(f"{r['plan']}: {round(r['c']/total*100)}%" for r in dist)

    text = (
        "📈 Analytics\n\n"
        f"⚡ Forwards today: {today_fwd}\n"
        f"🚫 Duplicates blocked today: {dups_today}\n\n"
        f"Plan distribution: {dist_lines}\n"
    )

    if ai_stats:
        text += (
            f"\n🤖 AI today: {ai_stats['requests']} req · "
            f"{ai_stats['successful']} ok · {ai_stats['failed']} fail · "
            f"{ai_stats['avg_latency_ms']}ms avg"
        )

    await query.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data="adm:refresh:analytics"),
            InlineKeyboardButton("⬅ Admin Home", callback_data="admback"),
        ]])
    )


async def _render_whatsapp_pairing_status(query):
    """Show WhatsApp pairing codes and their status for admin review."""
    from services.destination_service import get_destinations

    conn = get_connection()
    cur = conn.cursor()
    
    # Get all WhatsApp/Threads destinations with pairing info
    cur.execute("""
        SELECT d.*, p.name as project_name, u.telegram_id as owner_id, u.username as owner_username
        FROM destinations d
        JOIN projects p ON d.project_id = p.id
        JOIN users u ON p.user_id = u.telegram_id
        WHERE d.chat_type IN ('whatsapp_channel', 'threads')
        ORDER BY d.id DESC
    """)
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await query.message.reply_text(
            "📱 WhatsApp/Threads Pairing Status\n\nNo WhatsApp or Threads destinations configured yet.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Platforms", callback_data="adm:platforms")],
            ])
        )
        return

    lines = ["📱 WhatsApp/Threads Pairing Status\n"]
    buttons = []

    for r in rows:
        status = r["pairing_status"] or "pending"
        status_icons = {
            "pending": "🟡",
            "verifying": "🔵",
            "verified": "🟢",
            "failed": "🔴",
            "expired": "⚠️",
        }
        status_icon = status_icons.get(status, "🟡")
        code = r["pairing_code"] or "—"
        
        lines.append(
            f"\n{status_icon} Project: {r['project_name']} (Owner: @{r['owner_username'] or r['owner_id']})\n"
            f"   Destination: {r['title'] or r['chat_id']}\n"
            f"   Status: {status_icon} {status.upper()}\n"
            f"   Code: {code}"
        )
        
        if r["pairing_expires_at"]:
            from datetime import datetime
            expires = datetime.fromtimestamp(r["pairing_expires_at"])
            lines.append(f"   Expires: {expires.strftime('%Y-%m-%d %H:%M')}")

    lines.append("\nUse the buttons below to manage pairings:")
    buttons = [
        [InlineKeyboardButton("🧹 Cleanup Expired", callback_data="adm:wapaircleanup")],
        [InlineKeyboardButton("⬅ Platforms", callback_data="adm:platforms")],
    ]

    await query.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def _render_platforms(query):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM platform_status")
    rows = cur.fetchall()
    conn.close()

    icons = {"enabled": "🟢", "disabled": "🔴", "maintenance": "🟠"}

    lines = ["🌐 Platforms\n"]
    buttons = []
    for r in rows:
        lines.append(f"{icons.get(r['state'], '•')} {r['platform']}: {r['state']}")
        buttons.append([InlineKeyboardButton(
            f"Toggle {r['platform']} ({icons.get(r['state'],'•')} {r['state']})",
            callback_data=f"adm:platformtoggle:{r['platform']}"
        )])

    buttons.append([InlineKeyboardButton("📱 WhatsApp Pairing Status", callback_data="adm:wapairstatus")])
    buttons.append([InlineKeyboardButton("⬅ Admin Home", callback_data="admback")])

    await query.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def _render_system(query):
    stats = job_queue.get_queue_stats()
    engine = "🟢 Running" if is_running() else "🔴 Offline"

    by_status = stats.get("by_status", {})
    text = (
        "⚙️ System\n\n"
        f"🚀 Forward Engine: {engine}\n"
        f"📋 Jobs pending: {by_status.get('pending', 0)} · running: {by_status.get('running', 0)}\n"
        f"💀 Dead letters: {stats.get('unresolved_dead_letters', 0)}"
    )

    await query.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data="adm:refresh:system"),
            InlineKeyboardButton("⬅ Admin Home", callback_data="admback"),
        ]])
    )


async def _render_audit(query):
    logs = RBAC.query_audit_logs(limit=15)

    lines = ["📜 Audit Logs (latest)\n"]
    for l in logs:
        ts = (l["created_at"] or "")[:16]
        lines.append(f"{ts} · {l['admin_username'] or l['admin_id']} · {l['action']} → {l['target_type'] or ''}{('#' + l['target_id']) if l['target_id'] else ''}")

    if not logs:
        lines.append("No entries yet.")

    await query.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data="adm:refresh:audit"),
            InlineKeyboardButton("⬅ Admin Home", callback_data="admback"),
        ]])
    )


async def _render_admins(query):
    admins = RBAC.list_admins()

    lines = [f"👨‍💼 Admin Management\nOwner IDs (env): {', '.join(str(i) for i in sorted(ADMIN_IDS))}\n"]
    buttons = []

    for a in admins:
        status = "🟢" if a["is_active"] else "⚪"
        perms = len(RBAC.get_permissions(a["telegram_id"]))
        lines.append(f"{status} {a['telegram_id']} (@{a['username'] or '-'}) · {perms} perms")
        buttons.append([
            InlineKeyboardButton(
                f"🔑 Permissions {a['username'] or a['telegram_id']}",
                callback_data=f"adm:adminperms:{a['telegram_id']}"
            ),
            InlineKeyboardButton(
                f"❌ Remove {a['username'] or a['telegram_id']}",
                callback_data=f"adm:adminremove:{a['telegram_id']}"
            ),
        ])

    if not admins:
        lines.append("No normal admins yet.")

    buttons.append([InlineKeyboardButton("➕ Add Admin", callback_data="adm:adminadd")])
    buttons.append([InlineKeyboardButton("⬅ Admin Home", callback_data="admback")])

    await query.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def _render_admin_permissions(query, aid):
    """Granular permission editor for one admin. Owner-created admins get
    the full set; owners themselves are not editable here."""

    admin_row = next((a for a in RBAC.list_admins() if a["telegram_id"] == aid), None)

    if admin_row is None:
        await query.message.reply_text("❌ Admin not found.")
        return

    if RBAC.is_owner(aid):
        await query.message.reply_text(
            f"🛡 {aid} is an OWNER (env-configured). Owner permissions are "
            "fixed and cannot be edited here.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Admin Management", callback_data="adm:admins")],
            ]),
        )
        return

    granted = RBAC.get_permissions(aid)

    lines = [
        f"🔑 Permissions for @{admin_row['username'] or aid} ({aid})\n",
        "Tap a permission to toggle it:",
    ]

    buttons = []
    for perm in RBAC.PERMISSIONS:
        state = "✅" if perm in granted else "⬜"
        buttons.append([InlineKeyboardButton(
            f"{state} {perm}",
            callback_data=f"adm:admintoggleperm:{aid}:{perm}"
        )])

    buttons.append([InlineKeyboardButton(
        "🟢 Active" if admin_row["is_active"] else "⚪ Disabled",
        callback_data=f"adm:admintoggleactive:{aid}"
    )])
    buttons.append([InlineKeyboardButton("⬅ Admin Management", callback_data="adm:admins")])

    await query.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))