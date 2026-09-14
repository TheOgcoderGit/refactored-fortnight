"""
ChannelFlow AI - Subscription Scheduler
===========================================

One periodic background loop, started alongside the forward engine
(core/listener.py), handling everything that needs to happen based on
the CALENDAR rather than in direct response to a user action:

    1. Expiry reminders - plans expiring within REMINDER_DAYS get a
       one-time DM (tracked so it's not repeated every run).
    2. Auto-renew - plans that have already expired, for users with
       auto_renew=1: attempt a wallet debit for last_purchase_plan/
       last_purchase_months (services/wallet_service.py). Success
       extends the plan by that many months; insufficient funds falls
       through to step 3 exactly like a user with auto_renew=0.
    3. Downgrade fallback - plans that have expired and were NOT
       renewed (auto_renew off, or renewal failed) drop to FREE, with
       a DM explaining why.
    4. Stale payment cleanup - payment_requests sitting unreviewed past
       the timeout (services/payment_service.expire_stale_requests)
       get marked EXPIRED and the user is notified to try again.

Runs every CHECK_INTERVAL_SECONDS - frequent enough that a plan
expiring "today" is handled within the hour, without hammering the
database. All four steps have their own error boundary so one broken
row (e.g. a bad expiry timestamp) can't take the others down.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from database.db import get_connection
from services import plan_service, wallet_service, payment_service, pricing_service
from services import dedup_service, job_queue
from services.source_health import notify_unhealthy_sources

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 3600  # hourly
REMINDER_DAYS = (3, 1)  # send a reminder at each of these day-marks


def _parse_expiry(value):

    if not value:
        return None

    try:
        expiry = datetime.fromisoformat(value)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry
    except (TypeError, ValueError):
        return None


async def _send_reminders(bot):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT telegram_id, plan, plan_expiry FROM users "
        "WHERE plan != 'FREE' AND plan_expiry IS NOT NULL"
    )
    rows = cur.fetchall()
    conn.close()

    now = datetime.now(timezone.utc)

    for row in rows:

        expiry = _parse_expiry(row["plan_expiry"])

        if expiry is None:
            continue

        days_left = (expiry - now).days

        if days_left not in REMINDER_DAYS:
            continue

        conn2 = get_connection()
        cur2 = conn2.cursor()

        try:
            cur2.execute(
                "INSERT INTO reminder_log(user_id, plan_expiry, days_mark) VALUES (?, ?, ?)",
                (row["telegram_id"], row["plan_expiry"], days_left),
            )
            conn2.commit()
            already_sent = False
        except Exception:
            already_sent = True  # UNIQUE constraint hit - sent for this expiry+mark already
        finally:
            conn2.close()

        if already_sent:
            continue

        try:
            await bot.send_message(
                row["telegram_id"],
                f"⏰ Your {row['plan']} plan expires in {days_left} day"
                f"{'s' if days_left != 1 else ''}. Tap 💎 Upgrade in /start "
                "to renew, or turn on Auto-Renew in Settings if you have "
                "wallet balance."
            )
        except Exception:
            logger.exception("Failed to send expiry reminder to %s", row["telegram_id"])


async def _process_expired_plans(bot):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT telegram_id, plan, plan_expiry, auto_renew, last_purchase_plan, last_purchase_months "
        "FROM users WHERE plan != 'FREE' AND plan_expiry IS NOT NULL"
    )
    rows = cur.fetchall()
    conn.close()

    now = datetime.now(timezone.utc)

    for row in rows:

        expiry = _parse_expiry(row["plan_expiry"])

        if expiry is None or expiry > now:
            continue  # not expired yet

        user_id = row["telegram_id"]

        try:

            renewed = False

            if False and row["auto_renew"] and row["last_purchase_plan"] and row["last_purchase_months"]:

                plan = row["last_purchase_plan"]
                months = row["last_purchase_months"]

                # Calculate renewal price using pricing_service
                durations = {row["months"]: row for row in pricing_service.get_duration_options(plan)}
                if months in durations:
                    duration_row = durations[months]
                    pricing = pricing_service.calculate_price(
                        plan,
                        months,
                        duration_row["discount_percent"],
                        duration_row["price_inr_override"]
                    )
                    price_inr = pricing["price_inr"]
                else:
                    # Fallback to monthly price * months
                    price_inr = plan_service.get_plan_monthly_price_inr(plan) * months

                if price_inr > 0 and wallet_service.debit(user_id, price_inr, reference=f"auto_renew:{plan}:{months}mo"):

                    new_expiry = (now + timedelta(days=30 * months)).isoformat()
                    plan_service.set_user_plan(user_id, plan, expiry=new_expiry)
                    renewed = True

                    try:
                        await bot.send_message(
                            user_id,
                            f"🔄 Auto-renewed your {plan} plan for {months} month"
                            f"{'s' if months != 1 else ''} - ₹{price_inr:.0f} deducted from your wallet."
                        )
                    except Exception:
                        pass

            if not renewed:

                plan_service.set_user_plan(user_id, "FREE", expiry=None)

                reason = "automatic paid renewal is disabled in this hardening build"

                try:
                    await bot.send_message(
                        user_id,
                        f"⚠ Your {row['plan']} plan has expired and moved to FREE "
                        f"({reason}). Tap 💎 Upgrade in /start to get back on a paid plan."
                    )
                except Exception:
                    pass

        except Exception:
            logger.exception("Error processing expired plan for user %s", user_id)


async def _cleanup_stale_payments(bot):

    try:
        expired = payment_service.expire_stale_requests()
    except Exception:
        logger.exception("Error expiring stale payment requests")
        return

    for request in expired:

        try:
            plan_text = request['plan'] or 'wallet top-up'
            await bot.send_message(
                request["user_id"],
                f"⏰ Your payment request for {plan_text} "
                f"expired after {payment_service.STALE_HOURS}h without admin review. "
                "Please try again - tap 💎 Upgrade in /start."
            )
        except Exception:
            logger.exception("Failed to notify user %s about expired payment", request["user_id"])


async def run_subscription_scheduler(bot):
    """Started once from core/listener.py, alongside the forward
    engine - takes the bot instance so it can DM users directly."""

    logger.info("Subscription scheduler running")

    while True:

        try:
            await _send_reminders(bot)
        except Exception:
            logger.exception("Error sending expiry reminders")

        try:
            await _process_expired_plans(bot)
        except Exception:
            logger.exception("Error processing expired plans")

        try:
            await _cleanup_stale_payments(bot)
        except Exception:
            logger.exception("Error cleaning up stale payment requests")

        # ---- Phase 4 maintenance tasks ----

        try:
            dedup_service.cleanup_expired_claims()
        except Exception:
            logger.exception("Error cleaning expired dedup claims")

        try:
            job_queue.recover_stuck_jobs()
        except Exception:
            logger.exception("Error recovering stuck jobs")

        # ---- Source health monitoring ----
        try:
            await notify_unhealthy_sources()
        except Exception:
            logger.exception("Error checking source health")

        # ---- WhatsApp pairing cleanup ----
        try:
            from services.destination_service import cleanup_expired_pairings
            expired = cleanup_expired_pairings()
            if expired:
                logger.info("Cleaned up %s expired pairing codes", expired)
        except Exception:
            logger.exception("Error cleaning expired pairing codes")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)