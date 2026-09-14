"""
ChannelFlow AI - Plan / Entitlement Service
==============================================

Plan configuration is now stored in the database (plan_configs table)
instead of hardcoded. This allows admins to configure all plan limits
and pricing from the admin panel without code changes.

Plans: FREE, BEGINNER, PRO, CREATOR
"""

import json
import math
from datetime import datetime, timedelta, timezone

from database.db import get_connection

VALID_PLANS = ("FREE", "BEGINNER", "PRO", "CREATOR")

TRIAL_PLAN = "CREATOR"
TRIAL_DAYS = 7

# Fallback limits used only if DB is unavailable (should never happen in production)
FALLBACK_LIMITS = {
    "FREE": {
        "max_projects": 1,
        "max_sources_per_project": 2,
        "max_destinations_per_project": 1,
        "max_middle_destinations_per_project": 0,
        "daily_forward_limit": 100,
        "per_project_daily_forward_limit": 100,
        "requires_attribution": True,
        "feature_flags": {"ai_rewrite": False, "scheduling": False, "watermark": False, "analytics": False, "whatsapp": False, "threads": False, "multi_account": False},
    },
    "BEGINNER": {
        "max_projects": 5,
        "max_sources_per_project": 5,
        "max_destinations_per_project": 2,
        "max_middle_destinations_per_project": 1,
        "daily_forward_limit": 200,
        "per_project_daily_forward_limit": 200,
        "requires_attribution": False,
        "feature_flags": {"ai_rewrite": False, "scheduling": True, "watermark": False, "analytics": True, "whatsapp": False, "threads": False, "multi_account": False},
    },
    "PRO": {
        "max_projects": 10,
        "max_sources_per_project": 10,
        "max_destinations_per_project": 5,
        "max_middle_destinations_per_project": 5,
        "daily_forward_limit": 1000,
        "per_project_daily_forward_limit": 1000,
        "requires_attribution": False,
        "feature_flags": {"ai_rewrite": True, "scheduling": True, "watermark": True, "analytics": True, "whatsapp": True, "threads": False, "multi_account": False},
    },
    "CREATOR": {
        "max_projects": 20,
        "max_sources_per_project": 20,
        "max_destinations_per_project": 10,
        "max_middle_destinations_per_project": 10,
        "daily_forward_limit": 2000,
        "per_project_daily_forward_limit": 2000,
        "requires_attribution": False,
        "feature_flags": {"ai_rewrite": True, "scheduling": True, "watermark": True, "analytics": True, "whatsapp": True, "threads": True, "multi_account": True},
    },
}


def _get_plan_config(plan_name: str) -> dict:
    """Fetch plan configuration from database."""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM plan_configs WHERE plan_name=? AND active=1", (plan_name,))
    row = cur.fetchone()
    conn.close()

    if row:
        return {
            "max_projects": row["max_projects"],
            "max_sources_per_project": row["max_sources_per_project"],
            "max_destinations_per_project": row["max_destinations_per_project"],
            "max_middle_destinations_per_project": row["max_middle_destinations_per_project"],
            "daily_forward_limit": row["daily_forward_limit"],
            "per_project_daily_forward_limit": row["per_project_daily_forward_limit"],
            "requires_attribution": bool(row["requires_attribution"]),
            "feature_flags": json.loads(row["feature_flags"] or "{}"),
            "monthly_price_inr": row["monthly_price_inr"],
            "crypto_monthly_price_usd": row["crypto_monthly_price_usd"],
            "display_name": row["display_name"],
        }

    # Fallback to hardcoded values
    fallback = dict(FALLBACK_LIMITS.get(plan_name, FALLBACK_LIMITS["FREE"]))
    fallback.setdefault("monthly_price_inr", 0)
    fallback.setdefault("crypto_monthly_price_usd", 0)
    fallback.setdefault("display_name", plan_name)
    return fallback


def _get_all_plan_configs() -> dict:
    """Fetch all active plan configurations."""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM plan_configs WHERE active=1 ORDER BY monthly_price_inr ASC")
    rows = cur.fetchall()
    conn.close()

    configs = {}
    for row in rows:
        configs[row["plan_name"]] = {
            "max_projects": row["max_projects"],
            "max_sources_per_project": row["max_sources_per_project"],
            "max_destinations_per_project": row["max_destinations_per_project"],
            "max_middle_destinations_per_project": row["max_middle_destinations_per_project"],
            "daily_forward_limit": row["daily_forward_limit"],
            "per_project_daily_forward_limit": row["per_project_daily_forward_limit"],
            "requires_attribution": bool(row["requires_attribution"]),
            "feature_flags": json.loads(row["feature_flags"] or "{}"),
            "monthly_price_inr": row["monthly_price_inr"],
            "crypto_monthly_price_usd": row["crypto_monthly_price_usd"],
            "stars_monthly_price": row["stars_monthly_price"],
            "display_name": row["display_name"],
        }

    # Fill in any missing plans with fallbacks
    for plan_name in VALID_PLANS:
        if plan_name not in configs:
            fallback = dict(FALLBACK_LIMITS[plan_name])
            fallback.setdefault("monthly_price_inr", 0)
            fallback.setdefault("crypto_monthly_price_usd", 0)
            fallback.setdefault("display_name", plan_name)
            configs[plan_name] = fallback

    return configs


PLAN_CONFIGS_CACHE = None
PLAN_CONFIGS_CACHE_TIME = 0
CACHE_TTL = 60  # seconds


def get_cached_plan_configs() -> dict:
    """Get plan configs with simple in-memory caching."""
    global PLAN_CONFIGS_CACHE, PLAN_CONFIGS_CACHE_TIME
    import time

    now = time.time()
    if PLAN_CONFIGS_CACHE is None or (now - PLAN_CONFIGS_CACHE_TIME) > CACHE_TTL:
        PLAN_CONFIGS_CACHE = _get_all_plan_configs()
        PLAN_CONFIGS_CACHE_TIME = now
    return PLAN_CONFIGS_CACHE


def invalidate_plan_configs_cache():
    """Call this after admin updates plan configs."""
    global PLAN_CONFIGS_CACHE
    PLAN_CONFIGS_CACHE = None


def get_user_plan(telegram_id) -> str:
    """Returns the effective plan - if plan_expiry has passed, the user
    is treated as FREE regardless of the stored plan column."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT plan, plan_expiry FROM users WHERE telegram_id=?", (telegram_id,)
    )
    row = cur.fetchone()
    conn.close()

    if row is None:
        return "FREE"

    if row["plan_expiry"]:
        try:
            expiry = datetime.fromisoformat(row["plan_expiry"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expiry:
                return "FREE"
        except (TypeError, ValueError):
            pass

    plan = row["plan"] or "FREE"
    # Normalize CREATORS to CREATOR
    if plan == "CREATORS":
        plan = "CREATOR"
    return plan


def set_user_plan(telegram_id, plan, expiry=None):

    if plan not in VALID_PLANS:
        raise ValueError(f"Invalid plan: {plan}")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "UPDATE users SET plan=?, plan_expiry=? WHERE telegram_id=?",
        (plan, expiry, telegram_id),
    )

    conn.commit()
    conn.close()


_PLAN_RANK = {"FREE": 0, "BEGINNER": 1, "PRO": 2, "CREATOR": 3}


def extend_plan(telegram_id, minimum_plan, days):
    """Adds `days` on top of whatever plan_expiry this user currently
    has (rather than overwriting it), and raises their plan to at
    least `minimum_plan` if they're currently on something lower -
    never downgrades someone who's already on a better plan."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT plan, plan_expiry FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()

    if row is None:
        conn.close()
        return

    current_plan = row["plan"] or "FREE"
    # Normalize
    if current_plan == "CREATORS":
        current_plan = "CREATOR"
    if minimum_plan == "CREATORS":
        minimum_plan = "CREATOR"

    base = datetime.now(timezone.utc)

    if row["plan_expiry"]:
        try:
            existing_expiry = datetime.fromisoformat(row["plan_expiry"])
            if existing_expiry.tzinfo is None:
                existing_expiry = existing_expiry.replace(tzinfo=timezone.utc)
            if existing_expiry > base:
                base = existing_expiry
        except (TypeError, ValueError):
            pass

    new_expiry = (base + timedelta(days=days)).isoformat()

    new_plan = (
        minimum_plan
        if _PLAN_RANK.get(minimum_plan, 0) > _PLAN_RANK.get(current_plan, 0)
        else current_plan
    )

    cur.execute(
        "UPDATE users SET plan=?, plan_expiry=? WHERE telegram_id=?",
        (new_plan, new_expiry, telegram_id),
    )

    conn.commit()
    conn.close()


def start_trial(telegram_id):
    expiry = (datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)).isoformat()
    conn = get_connection()
    try:
        with conn:
            result = conn.execute("""UPDATE users SET plan=?,plan_expiry=?,trial_started_at=CURRENT_TIMESTAMP
                WHERE telegram_id=? AND trial_started_at IS NULL AND plan='FREE' AND plan_expiry IS NULL""",
                (TRIAL_PLAN,expiry,telegram_id))
        return result.rowcount == 1
    finally:
        conn.close()


def get_plan_status(telegram_id) -> dict:
    """Plan plus how much of it is left.

    ``plan_expiry`` was already stored and already enforced by
    get_user_plan(), but nothing ever surfaced it, so a user on a 7-day
    Creator trial had no way to know the trial existed - let alone when it
    ended.
    """

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT plan, plan_expiry FROM users WHERE telegram_id=?",
                (telegram_id,))
    row = cur.fetchone()
    conn.close()

    if row is None:
        return {"plan": "FREE", "plan_expiry": None, "days_left": None,
                "expired": False, "on_trial": False}

    expiry_raw = row["plan_expiry"]
    expiry = None
    if expiry_raw:
        try:
            expiry = datetime.fromisoformat(expiry_raw)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            expiry = None

    now = datetime.now(timezone.utc)
    if expiry is None:
        days_left = None
        expired = False
    else:
        remaining = expiry - now
        seconds = remaining.total_seconds()
        expired = seconds <= 0
        # Round partial days UP: an expiry 6 days 23 hours away is "7 days
        # left" to a user, not 6. Floor would show 6 the moment the plan is
        # granted, and then 0 for the last 23 hours.
        days_left = max(0, math.ceil(seconds / 86400)) if not expired else 0

    effective = row["plan"]
    if expired:
        effective = "FREE"

    return {
        "plan": effective,
        "stored_plan": row["plan"],
        "plan_expiry": expiry_raw,
        "days_left": days_left,
        "expired": expired,
        # A trial is a non-free plan the user did not buy - surfaced so the
        # UI can say "trial" instead of "subscription".
        "on_trial": effective != "FREE" and days_left is not None,
    }


def get_entitlements(telegram_id) -> dict:
    plan = get_user_plan(telegram_id)
    configs = get_cached_plan_configs()
    plan_config = configs.get(plan, FALLBACK_LIMITS["FREE"])
    return {**plan_config, "plan": plan, **get_plan_status(telegram_id)}


def requires_attribution(telegram_id) -> bool:
    """The single, centralized answer to "does this user's content need
    the attribution footer"."""
    return get_entitlements(telegram_id)["requires_attribution"]


def has_feature(telegram_id, feature_name: str) -> bool:
    """Check if user's plan includes a specific feature."""
    return get_entitlements(telegram_id)["feature_flags"].get(feature_name, False)


def get_plan_display_name(plan_name: str) -> str:
    """Get the display name for a plan."""
    configs = get_cached_plan_configs()
    return configs.get(plan_name, {}).get("display_name", plan_name)


def get_plan_monthly_price_inr(plan_name: str) -> float:
    """Get the monthly price in INR for a plan."""
    configs = get_cached_plan_configs()
    return configs.get(plan_name, {}).get("monthly_price_inr", 0)


def get_plan_crypto_price_usd(plan_name: str) -> float:
    """Get the monthly crypto price in USD for a plan (separate from
    INR pricing per Prompt 3 - crypto prices are intentionally NOT a
    currency conversion)."""
    configs = get_cached_plan_configs()
    return configs.get(plan_name, {}).get("crypto_monthly_price_usd", 0) or 0.0


def get_plan_stars_price(plan_name: str) -> int:
    """Get the monthly price in Telegram Stars for a plan.
    Reads from the configured stars_monthly_price in plan_configs."""
    configs = get_cached_plan_configs()
    return configs.get(plan_name, {}).get("stars_monthly_price", 0) or 0


# ==========================================
# LIMIT CHECKS
# ==========================================

def can_create_project(telegram_id) -> tuple:

    limits = get_entitlements(telegram_id)
    max_projects = limits["max_projects"]

    if max_projects is None:
        return True, None

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM projects WHERE user_id=?", (telegram_id,))
    current = cur.fetchone()[0]
    conn.close()

    if current >= max_projects:
        return False, (
            f"Your {limits['plan']} plan allows up to {max_projects} project(s). "
            "Upgrade to create more."
        )

    return True, None


def can_add_source(telegram_id, project_id) -> tuple:

    limits = get_entitlements(telegram_id)
    max_sources = limits["max_sources_per_project"]

    if max_sources is None:
        return True, None

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sources WHERE project_id=?", (project_id,))
    current = cur.fetchone()[0]
    conn.close()

    if current >= max_sources:
        return False, (
            f"Your {limits['plan']} plan allows up to {max_sources} source(s) "
            "per project. Upgrade to add more."
        )

    return True, None


def can_add_destination(telegram_id, project_id) -> tuple:

    limits = get_entitlements(telegram_id)
    max_destinations = limits["max_destinations_per_project"]

    if max_destinations is None:
        return True, None

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM destinations WHERE project_id=?", (project_id,))
    current = cur.fetchone()[0]
    conn.close()

    if current >= max_destinations:
        return False, (
            f"Your {limits['plan']} plan allows up to {max_destinations} "
            "destination(s) per project. Upgrade to add more."
        )

    return True, None


def can_add_middle_destination(telegram_id, project_id) -> tuple:

    limits = get_entitlements(telegram_id)
    max_middle = limits["max_middle_destinations_per_project"]

    if max_middle is None:
        return True, None

    conn = get_connection()
    cur = conn.cursor()
    # Assuming middle destinations are tracked in a separate table or as a flag
    # For now, check if any destination is marked as middle
    cur.execute("SELECT COUNT(*) FROM destinations WHERE project_id=? AND is_middle=1", (project_id,))
    current = cur.fetchone()[0]
    conn.close()

    if current >= max_middle:
        return False, (
            f"Your {limits['plan']} plan allows up to {max_middle} middle destination(s) "
            "per project. Upgrade to add more."
        )

    return True, None


def within_daily_forward_limit(telegram_id, project_id) -> bool:
    """Backed by daily_usage (bumped in services/stats_service.increment()
    alongside the lifetime 'forwarded' counter) rather than `logs`, which
    is capped/trimmed at 500 rows per project and so can't reliably
    answer "how many today" once a project is at all active."""

    limits = get_entitlements(telegram_id)
    daily_limit = limits["per_project_daily_forward_limit"]

    if daily_limit is None:
        return True

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT forward_count FROM daily_usage WHERE project_id=? AND usage_date=date('now')",
        (project_id,),
    )
    row = cur.fetchone()
    conn.close()

    today_count = row["forward_count"] if row else 0

    return today_count < daily_limit


def reserve_daily_forward(telegram_id, project_id) -> bool:
    """Atomically check AND reserve one unit of the per-project daily
    forward quota. Returns True only if the reservation succeeded (i.e.
    the project is still under its daily limit) - the increment is applied
    in the same serialized transaction, so N concurrent forwards at the
    limit can never all pass the check (prevents race overage).

    The lifecycle pairs with release_daily_forward() on forward failure so
    a reserved unit that never forwards is given back."""
    limits = get_entitlements(telegram_id)
    daily_limit = limits["per_project_daily_forward_limit"]

    if daily_limit is None:
        return True  # unlimited

    conn = get_connection()
    try:
        cur = conn.cursor()
        conn.execute("BEGIN IMMEDIATE")

        # Ensure a row exists for today
        cur.execute(
            "INSERT INTO daily_usage(project_id, usage_date, forward_count) "
            "VALUES (?, date('now'), 0) "
            "ON CONFLICT(project_id, usage_date) DO NOTHING",
            (project_id,),
        )

        cur.execute(
            "SELECT forward_count FROM daily_usage "
            "WHERE project_id=? AND usage_date=date('now')",
            (project_id,),
        )
        row = cur.fetchone()
        current = row["forward_count"] if row else 0

        if current >= daily_limit:
            conn.rollback()
            return False

        cur.execute(
            "UPDATE daily_usage SET forward_count = forward_count + 1 "
            "WHERE project_id=? AND usage_date=date('now')",
            (project_id,),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_daily_forward(project_id):
    """Give back one daily-quota unit when a reserved forward did not
    actually publish (so a failed attempt doesn't consume the quota)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE daily_usage SET forward_count = MAX(forward_count - 1, 0) "
        "WHERE project_id=? AND usage_date=date('now')",
        (project_id,),
    )
    conn.commit()
    conn.close()


def get_global_daily_forward_limit(telegram_id) -> int:
    """Get the global daily forward limit across all projects for a user."""
    limits = get_entitlements(telegram_id)
    return limits["daily_forward_limit"]


def within_global_daily_forward_limit(telegram_id) -> bool:
    """Check if user is within their global daily forward limit."""
    limit = get_global_daily_forward_limit(telegram_id)
    if limit is None:
        return True

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT SUM(forward_count) as total FROM daily_usage du
        JOIN projects p ON du.project_id = p.id
        WHERE p.user_id = ? AND du.usage_date = date('now')
    """, (telegram_id,))
    row = cur.fetchone()
    conn.close()

    total = row["total"] if row and row["total"] else 0
    return total < limit


def get_plan_limits(plan_name: str) -> dict:
    """Get limits for a specific plan (for admin display)."""
    configs = get_cached_plan_configs()
    return configs.get(plan_name, FALLBACK_LIMITS.get(plan_name, FALLBACK_LIMITS["FREE"]))


def get_project_daily_usage(project_id: int) -> tuple:
    """Get the daily forward usage for a project.
    
    Returns (used, total_limit) where used is the count today and
    total_limit is the per-project daily forward limit from the plan.
    """
    from services import plan_service as _ps
    
    # Get the project's user to determine the plan
    conn = _ps.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM projects WHERE id=?", (project_id,))
    row = cur.fetchone()
    conn.close()
    
    if row is None:
        return (0, None)
    
    user_id = row[0]
    limits = _ps.get_entitlements(user_id)
    total_limit = limits.get("per_project_daily_forward_limit")
    
    if total_limit is None:
        return (0, None)
    
    # Get today's count from daily_usage table
    conn2 = _ps.get_connection()
    cur2 = conn2.cursor()
    cur2.execute(
        "SELECT forward_count FROM daily_usage WHERE project_id=? AND usage_date=date('now')",
        (project_id,),
    )
    row2 = cur2.fetchone()
    conn2.close()
    
    used = row2["forward_count"] if row2 else 0
    return (used, total_limit)