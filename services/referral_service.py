"""
ChannelFlow AI - Referral Program
====================================

Two separate moments, on purpose:

    1. capture_referral() - called from /start when a brand-new user
       arrives via a `ref_<inviter_id>` deep link. Just records who
       invited whom, status "not verified" (reward_granted=0). No
       reward yet.
    2. grant_reward_if_pending() - called from the admin payment-
       approval handler (services/payment_service.py) whenever a
       user's plan is set via a REAL payment (any purchasable paid
       plan - Beginner, Pro, or Creator; see
       payment_service.PURCHASABLE_PLANS for what's currently
       purchasable).
       Only then does the referrer get +7 days, and only then does
       this referral flip from "not verified" to "active" in
       get_referral_stats(). This matches the confirmed business rule:
       the reward is for a paying referral, not merely a signup, and
       it doesn't matter which paid plan they chose.

Trial-plan grants (services/plan_service.start_trial, CREATOR) never
call this - only services/payment_service.approve_payment does, so a
free trial can never accidentally trigger a referral reward.

Rewards stack: each distinct referred person who converts adds another
+7 days on top of the referrer's current plan_expiry (not replacing
it), via plan_service.extend_plan().
"""

from database.db import get_connection
from services import plan_service

REFERRAL_REWARD_DAYS = 7
REFERRAL_REWARD_PLAN = "PRO"  # floor plan the referrer is raised to if lower


def build_referral_code(telegram_id) -> str:
    """The inviter's own telegram_id IS the code - simple, unique by
    construction, and traceable without a second lookup table."""

    return f"ref_{telegram_id}"


def parse_referral_code(start_param) -> "int | None":

    if not start_param or not start_param.startswith("ref_"):
        return None

    try:
        return int(start_param[len("ref_"):])
    except ValueError:
        return None


def capture_referral(referrer_id, referred_id):
    """No-ops safely (does not raise) if: the referrer is the same
    person as the referred user, the referrer doesn't exist, or this
    referred_id already has a referral row (UNIQUE constraint - first
    link used wins, silently)."""

    if referrer_id == referred_id:
        return False

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT 1 FROM users WHERE telegram_id=?", (referrer_id,))
    if cur.fetchone() is None:
        conn.close()
        return False

    try:
        cur.execute(
            "INSERT INTO referrals(referrer_id, referred_id) VALUES (?, ?)",
            (referrer_id, referred_id),
        )
        conn.commit()
        captured = True

    except Exception:
        # Already referred by someone else, or some other integrity
        # issue - either way, not this call's job to fix.
        captured = False

    conn.close()

    return captured


def grant_reward_if_pending(referred_id, new_plan):
    """Call this every time a user's plan is set via a REAL payment
    (services/payment_service.py's admin-approval path) - never from a
    trial grant. Fires for ANY paid plan the referred user converts
    to (not just PRO), as long as this referred_id has an unrewarded
    referral row. Everything else is a silent no-op, so callers don't
    need to pre-check anything themselves.

    "Paid plan" here means anything payment_service actually sells -
    delegated to that module's PURCHASABLE_PLANS rather than hard-coding the
    list here a second time."""

    from services import payment_service  # local import: payment_service
    # already imports plan_service + referral_service, so a top-level
    # import here would be circular.

    if new_plan not in payment_service.PURCHASABLE_PLANS:
        return None

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT id, referrer_id FROM referrals WHERE referred_id=? AND reward_granted=0",
        (referred_id,),
    )
    row = cur.fetchone()

    if row is None:
        conn.close()
        return None

    cur.execute(
        "UPDATE referrals SET reward_granted=1, rewarded_at=CURRENT_TIMESTAMP WHERE id=?",
        (row["id"],),
    )

    conn.commit()
    conn.close()

    plan_service.extend_plan(row["referrer_id"], REFERRAL_REWARD_PLAN, REFERRAL_REWARD_DAYS)

    # Grant any milestone tiers the referrer has now crossed.
    check_and_grant_milestones(row["referrer_id"])

    return row["referrer_id"]


def get_referral_stats(telegram_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (telegram_id,))
    total = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND reward_granted=1",
        (telegram_id,),
    )
    rewarded = cur.fetchone()[0]

    conn.close()

    return {"total_invited": total, "active_referrals": rewarded}


def get_referral_leaderboard(limit=10):
    """Returns the top referrers ranked by qualified (rewarded) referrals
    then total invites. Excludes owners/admins from the public board."""

    from config import ADMIN_IDS

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.referrer_id AS uid,
               COUNT(*) AS total,
               SUM(CASE WHEN r.reward_granted=1 THEN 1 ELSE 0 END) AS rewarded
        FROM referrals r
        GROUP BY r.referrer_id
        ORDER BY rewarded DESC, total DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()

    board = []
    for r in rows:
        uid = r["uid"]
        if uid in ADMIN_IDS:
            continue
        board.append({"user_id": uid, "total": r["total"], "rewarded": r["rewarded"]})
    return board


def get_milestone_progress(telegram_id):
    """Returns the user's rewarded-referral count and the next milestone
    tier they can still reach (configurable in referral_milestones table)."""

    stats = get_referral_stats(telegram_id)
    rewarded = stats["active_referrals"]

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM referral_milestones ORDER BY referrals_required ASC"
    )
    tiers = cur.fetchall()
    conn.close()

    next_tier = None
    for tier in tiers:
        if rewarded < tier["referrals_required"]:
            next_tier = tier
            break

    return {"rewarded": rewarded, "tiers": tiers, "next_tier": next_tier}


def check_and_grant_milestones(telegram_id):
    """Called after every referral reward grant. Gives the referrer the
    reward for each milestone tier they have now crossed but never
    received before (tracked in referral_milestone_grants). Each tier is
    granted exactly once."""

    stats = get_referral_stats(telegram_id)
    rewarded = stats["active_referrals"]

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM referral_milestones WHERE active=1 ORDER BY referrals_required ASC")
    tiers = cur.fetchall()

    granted_any = []
    for tier in tiers:
        if rewarded < tier["referrals_required"]:
            continue

        # Already granted this tier?
        cur.execute(
            "SELECT 1 FROM referral_milestone_grants WHERE user_id=? AND milestone_id=?",
            (telegram_id, tier["id"]),
        )
        if cur.fetchone():
            continue

        # Grant the milestone reward once
        try:
            if tier["reward_type"] == "plan":
                plan_service.extend_plan(
                    telegram_id, tier["reward_value"],
                    tier["reward_days"] or 0,
                )
            elif tier["reward_type"] == "wallet_credit":
                from services import wallet_service
                wallet_service.credit(
                    telegram_id,
                    float(tier["reward_value"]),
                    reference=f"referral_milestone:{tier['id']}",
                )
        except Exception:
            continue

        cur.execute(
            "INSERT OR IGNORE INTO referral_milestone_grants(user_id, milestone_id) VALUES (?, ?)",
            (telegram_id, tier["id"]),
        )
        granted_any.append(tier)

    conn.commit()
    conn.close()
    return granted_any
