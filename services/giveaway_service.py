"""
ChannelFlow AI - Giveaway Service
===================================

Admin-run giveaways (Prompt 1 §25-27):

    Types:  'discount'      - winners get a personal coupon code
            'subscription'  - winners get plan days granted directly

    Eligibility targeting:
        all | connected | never_purchased | expired | active | inactive
        | plan:<NAME>

    Flow: create -> select_winners() -> deliver_rewards(bot) ->
          winners redeem via /redeem in Wallet or Plan section.

Anti-abuse: winner coupons are user-bound single-use codes; redemption
goes through coupon_service.validate() like any other code.
"""

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from database.db import get_connection

logger = logging.getLogger(__name__)


def _rand_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    body = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"GIFT-{body}"


# ==========================================
# CREATE / QUERY
# ==========================================

def create_giveaway(name, gw_type, winner_count, config, eligibility="all", created_by=None):
    """config JSON keys:
        discount:  discount_pct, validity_days
        subscription: plan, days
    """

    if gw_type not in ("discount", "subscription"):
        raise ValueError("type must be 'discount' or 'subscription'")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO giveaways(name, type, winner_count, config, eligibility, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (name, gw_type, winner_count, json.dumps(config), eligibility, created_by),
    )

    conn.commit()
    gid = cur.lastrowid
    conn.close()

    return gid


def get_giveaway(giveaway_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,))
    row = cur.fetchone()
    conn.close()
    return row


def list_giveaways(active_only=True):
    conn = get_connection()
    cur = conn.cursor()
    if active_only:
        cur.execute("SELECT * FROM giveaways WHERE status != 'cancelled' ORDER BY id DESC")
    else:
        cur.execute("SELECT * FROM giveaways ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows


def cancel_giveaway(giveaway_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE giveaways SET status='cancelled' WHERE id=?", (giveaway_id,))
    conn.commit()
    conn.close()


def get_winners(giveaway_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM giveaway_winners WHERE giveaway_id=? ORDER BY id", (giveaway_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


# ==========================================
# ELIGIBILITY
# ==========================================

def get_eligible_users(eligibility):
    """Returns a list of telegram_ids matching the segment."""

    conn = get_connection()
    cur = conn.cursor()

    now = datetime.now(timezone.utc)

    if eligibility == "connected":
        cur.execute(
            "SELECT telegram_id FROM users u "
            "JOIN user_telegram_sessions s ON s.telegram_id = u.telegram_id "
            "WHERE s.status='connected'"
        )
        rows = [r["telegram_id"] for r in cur.fetchall()]

    elif eligibility == "never_purchased":
        cur.execute(
            "SELECT telegram_id FROM users WHERE last_purchase_plan IS NULL"
        )
        rows = [r["telegram_id"] for r in cur.fetchall()]

    elif eligibility == "expired":
        cur.execute(
            """
            SELECT telegram_id FROM users
            WHERE last_purchase_plan IS NOT NULL
              AND (plan_expiry IS NULL OR plan_expiry <= ?)
            """,
            (now.isoformat(),),
        )
        rows = [r["telegram_id"] for r in cur.fetchall()]

    elif eligibility == "active":
        # Active paid subscriber right now
        cur.execute(
            """
            SELECT telegram_id FROM users
            WHERE plan != 'FREE'
              AND plan_expiry IS NOT NULL AND plan_expiry > ?
            """,
            (now.isoformat(),),
        )
        rows = [r["telegram_id"] for r in cur.fetchall()]

    elif eligibility == "inactive":
        # No projects and never purchased - rough proxy for dormant
        cur.execute(
            """
            SELECT u.telegram_id FROM users u
            LEFT JOIN projects p ON p.user_id = u.telegram_id
            WHERE p.id IS NULL AND u.last_purchase_plan IS NULL
            """
        )
        rows = [r["telegram_id"] for r in cur.fetchall()]

    elif eligibility.startswith("plan:"):
        plan_name = eligibility.split(":", 1)[1].upper()
        cur.execute("SELECT telegram_id FROM users WHERE plan=?", (plan_name,))
        rows = [r["telegram_id"] for r in cur.fetchall()]

    else:  # 'all'
        cur.execute("SELECT telegram_id FROM users")
        rows = [r["telegram_id"] for r in cur.fetchall()]

    conn.close()
    return rows


# ==========================================
# WINNER SELECTION + REWARD DELIVERY
# ==========================================

def select_winners(giveaway_id):
    """Randomly picks N eligible users. Idempotent-ish: re-running adds
    no duplicates of the same user; tops up only if fewer than needed.
    Returns the full winners list."""

    gw = get_giveaway(giveaway_id)
    if gw is None or gw["status"] == "cancelled":
        return []

    import random

    existing = {w["user_id"] for w in get_winners(giveaway_id)}
    pool = [u for u in get_eligible_users(gw["eligibility"]) if u not in existing]
    random.shuffle(pool)

    need = max(0, int(gw["winner_count"]) - len(existing))
    picked = pool[:need]

    config = json.loads(gw["config"] or "{}")

    conn = get_connection()
    cur = conn.cursor()

    for uid in picked:

        if gw["type"] == "discount":
            from services.coupon_service import generate_code
            code = generate_code(prefix="GIFT")

            reward_summary = (
                f"{config.get('discount_pct', 10)}% discount coupon"
                f"{' (valid ' + str(config.get('validity_days', 30)) + ' days)' if config.get('validity_days') else ''}"
            )
        else:
            code = None  # subscription rewards grant directly, no coupon
            reward_summary = (
                f"{config.get('plan', 'PRO')} for {config.get('days', 7)} days"
            )

        cur.execute(
            """
            INSERT INTO giveaway_winners(giveaway_id, user_id, coupon_code, reward_summary)
            VALUES (?, ?, ?, ?)
            """,
            (giveaway_id, uid, code, reward_summary),
        )

    cur.execute(
        "UPDATE giveaways SET status='winners_selected' WHERE id=? AND status='draft'",
        (giveaway_id,),
    )

    conn.commit()
    conn.close()

    return get_winners(giveaway_id)


def deliver_rewards(bot, giveaway_id):
    """Sends each winner their personalized reward message.

    discount:      delivers a unique user-bound coupon (created on the
                   fly so it's redeemable through the normal path).
    subscription:  grants plan days immediately via plan_service.extend_plan.

    Returns (delivered, failed) counts."""

    from services import plan_service, coupon_service

    gw = get_giveaway(giveaway_id)
    if gw is None:
        return 0, 0

    config = json.loads(gw["config"] or "{}")
    delivered = failed = 0

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM giveaway_winners WHERE giveaway_id=?", (giveaway_id,))
    winners = cur.fetchall()
    conn.close()

    for w in winners:

        # Grants are made idempotent: a winner whose reward was already
        # granted (granted_at set) is skipped on re-delivery so a retried
        # delivery cannot extend the plan or recreate the coupon.
        if w["granted_at"]:
            continue

        try:

            message_lines = [
                "Hey Creator! 🎉 You just won our giveaway!",
                "",
                "Your reward is ready:",
            ]

            if gw["type"] == "discount":

                # Create a real user-bound single-use backing coupon
                code = w["coupon_code"]
                coupon_service.create_coupon(
                    code=code,
                    discount_percent=config.get("discount_pct", 10),
                    max_uses_per_user=1,
                    max_total_uses=1,
                    eligible_users=[w["user_id"]],
                    expires_at=(
                        datetime.now(timezone.utc)
                        + timedelta(days=int(config.get("validity_days", 30)))
                    ).isoformat(),
                    created_by=None,
                )
                message_lines.append(f"CODE: {code}")
                message_lines.append("")
                message_lines.append("Redeem it before it expires.")

            else:
                plan_name = config.get("plan", "PRO")
                days = int(config.get("days", 7))

                plan_service.extend_plan(w["user_id"], plan_name, days)
                message_lines.append(
                    f"Congratulations! Your {plan_name} reward is now active "
                    f"for {days} days. Enjoy ChannelFlow!"
                )

            # Mark the grant as recorded BEFORE attempting delivery. If the
            # send fails below, granted_at is still set, so a retried
            # delivery cannot re-grant the plan / recreate the coupon.
            conn2 = get_connection()
            cur2 = conn2.cursor()
            cur2.execute(
                "UPDATE giveaway_winners SET granted_at=CURRENT_TIMESTAMP WHERE id=?",
                (w["id"],),
            )
            conn2.commit()
            conn2.close()

            import asyncio

            async def _send():
                await bot.send_message(w["user_id"], "\n".join(message_lines))

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_send())
                notified = True
            except RuntimeError:
                try:
                    asyncio.run(_send())
                    notified = True
                except Exception:
                    notified = False

            if notified:
                conn3 = get_connection()
                cur3 = conn3.cursor()
                cur3.execute(
                    "UPDATE giveaway_winners SET notified_at=CURRENT_TIMESTAMP WHERE id=?",
                    (w["id"],),
                )
                conn3.commit()
                conn3.close()
                delivered += 1
            else:
                failed += 1

        except Exception:
            logger.exception("Reward delivery failed for winner %s", w["user_id"])
            failed += 1

    if delivered > 0 and gw["status"] != "cancelled":
        conn3 = get_connection()
        cur3 = conn3.cursor()
        cur3.execute("UPDATE giveaways SET status='delivered' WHERE id=?", (giveaway_id,))
        conn3.commit()
        conn3.close()

    return delivered, failed