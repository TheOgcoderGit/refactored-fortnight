"""
ChannelFlow AI - Payment / Upgrade Service
=============================================

Two payment methods, two flows (Prompt 3):

    UPI (INR):
        method-first -> plan -> duration -> pay to static UPI id ->
        submit screenshot -> admin approves/rejects.

    Crypto (USD):
        method-first -> plan -> duration -> REAL provider invoice
        (services/crypto_provider.py, OXAPAY merchant API) ->
        user pays via the provider checkout URL -> status verified
        SERVER-SIDE by polling the provider inquiry endpoint ->
        subscription activated transactionally + idempotently.

Price snapshots: every request freezes plan/duration/currency/base/
discount/final amount at creation time - later admin price changes
never alter an in-flight payment.

Nothing auto-confirms from a user click alone: crypto activation
happens only when the PROVIDER reports PAID; UPI always requires an
admin to actually review the screenshot.
"""

import logging
from datetime import datetime, timedelta, timezone

from config import OXAPAY_API_KEY, UPI_ID, UPI_PAYEE_NAME
from database.db import get_connection
from services import plan_service, referral_service, pricing_service

logger = logging.getLogger(__name__)

# Purchasable plans (FREE is free)
PURCHASABLE_PLANS = ("BEGINNER", "PRO", "CREATOR")

# USD conversion rate used ONLY for legacy amount_usd bookkeeping on
# INR rows. Crypto payments use their own USD price book and never go
# through this conversion.
INR_PER_USD = 85

# Crypto invoice lifetime (Prompt 3 section 30) - configurable here,
# shown to the user as an expiry time on the payment card.
CRYPTO_LIFETIME_MINUTES = 15


def is_oxapay_configured() -> bool:
    return bool(OXAPAY_API_KEY)


def is_upi_configured() -> bool:
    return bool(UPI_ID)


def get_available_methods():
    """Which payment methods are usable in this deployment."""
    methods = []
    if is_oxapay_configured():
        methods.append("crypto")
    if is_upi_configured():
        methods.append("upi")
    return methods


async def create_oxapay_invoice(amount_usd: float, order_id: str, description: str = "ChannelFlow payment"):
    """Creates a REAL OXAPAY invoice for arbitrary amounts (wallet
    top-ups). Returns (pay_link, track_id).

    BUG-003 fix: this was accidentally removed during the crypto-provider
    refactor while the wallet top-up flow still called it. Delegates to
    the provider abstraction so credentials never leave server-side code.
    Raises RuntimeError with a user-safe message on failure."""
    raise RuntimeError("Payment processing is disabled in this partial hardening release; complete payment acceptance testing before enabling it.")

    from services import crypto_provider

    provider = crypto_provider.get_provider()

    try:
        creation = await provider.create_payment(
            amount_usd=amount_usd,
            order_id=order_id,
            description=description,
        )
        return creation.payment_url, creation.provider_payment_id

    except crypto_provider.CryptoPaymentError as e:
        raise RuntimeError(str(e))


def _column_exists(cur, table, column):
    cur.execute(f"PRAGMA table_info({table})")
    columns = [row["name"] for row in cur.fetchall()]
    return column in columns


# ==========================================
# CRYPTO PAYMENT CREATION (real provider)
# ==========================================

async def create_crypto_payment(user_id, plan, months):
    """Creates a REAL crypto invoice via the configured provider and a
    local snapshot row. Returns the updated request row.

    Raises services.crypto_provider.CryptoPaymentError with a
    user-safe message when the provider can't be reached/rejects."""
    raise RuntimeError("Payment processing is disabled in this partial hardening release; complete payment acceptance testing before enabling it.")

    from services import crypto_provider

    if plan not in PURCHASABLE_PLANS:
        raise ValueError(f"{plan} is not a purchasable plan")

    durations = {row["months"]: row for row in pricing_service.get_duration_options(plan)}
    if months not in durations:
        raise ValueError(f"Invalid duration: {months} months for plan {plan}")

    duration_row = durations[months]

    # USD crypto pricing - NOT an INR conversion
    crypto_pricing = pricing_service.calculate_crypto_price(
        plan, months, pricing_service.get_crypto_discount_for_duration(duration_row)
    )
    base_usd = crypto_pricing["full_price_usd"]
    final_usd = crypto_pricing["price_usd"]

    provider = crypto_provider.get_provider()

    # Local row first so we have an id for the provider order reference
    conn = get_connection()
    cur = conn.cursor()

    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=CRYPTO_LIFETIME_MINUTES)
    ).isoformat()

    cur.execute(
        """
        INSERT INTO payment_requests(
            user_id, plan, months, method, currency,
            base_amount, discount_percent, final_amount,
            amount_inr, amount_usd,
            status, purpose, expires_at
        )
        VALUES (?, ?, ?, 'crypto', 'USD', ?, ?, ?, ?, ?, 'PENDING_PAYMENT', 'plan', ?)
        """,
        (
            user_id, plan, months,
            base_usd, crypto_pricing["discount_percent"], final_usd,
            round(final_usd * INR_PER_USD, 2), final_usd,
            expires_at,
        ),
    )

    conn.commit()
    request_id = cur.lastrowid
    conn.close()

    creation = await provider.create_payment(
        amount_usd=final_usd,
        order_id=f"CF-u{user_id}-p{request_id}-{plan}-{months}mo",
        description=f"ChannelFlow {plan} {months}mo",
        lifetime_minutes=CRYPTO_LIFETIME_MINUTES,
    )

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE payment_requests
        SET provider_payment_id=?, payment_url=?, expires_at=?
        WHERE id=?
        """,
        (
            creation.provider_payment_id,
            creation.payment_url,
            creation.expires_at.isoformat(),
            request_id,
        ),
    )
    conn.commit()
    conn.close()

    return get_payment_request(request_id)


async def create_stars_payment(user_id, plan, months):
    """Create a Stars payment request for a plan purchase.

    Uses the configured stars_monthly_price from plan_configs.
    Returns the updated request row.

    Raises ValueError if the plan is not purchasable or the Stars
    price is not configured."""
    raise RuntimeError("Payment processing is disabled in this partial hardening release; complete payment acceptance testing before enabling it.")
    from services import plan_service

    if plan not in PURCHASABLE_PLANS:
        raise ValueError(f"{plan} is not a purchasable plan")

    stars_price = plan_service.get_plan_stars_price(plan)
    if stars_price <= 0:
        raise ValueError(f"Stars price not configured for plan {plan}")

    duration_row = {row["months"]: row for row in pricing_service.get_duration_options(plan)}[months]

    conn = get_connection()
    cur = conn.cursor()

    # Use the same expiry logic as crypto payments
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=CRYPTO_LIFETIME_MINUTES)
    ).isoformat()

    cur.execute(
        """
        INSERT INTO payment_requests(
            user_id, plan, months, method, currency,
            base_amount, discount_percent, final_amount,
            amount_inr, amount_usd,
            status, purpose, expires_at
        )
        VALUES (?, ?, ?, 'stars', 'STARS', ?, ?, ?, ?, ?, 'PENDING_PAYMENT', 'plan', ?)
        """,
        (
            user_id, plan, months,
            stars_price, 0,  # base_amount and discount are Stars quantities
            stars_price,  # final_amount in Stars
            0,  # amount_inr (not applicable)
            stars_price,  # amount_usd (not applicable)
            expires_at,
        ),
    )

    conn.commit()
    request_id = cur.lastrowid
    conn.close()

    return get_payment_request(request_id)


async def check_crypto_payment(request_id):
    """Polls the provider for the authoritative status of a crypto
    payment. Returns (request_row, normalized_status). When the
    provider says PAID, the subscription is activated transactionally
    and idempotently (double-poll / double-webhook safe)."""

    from services import crypto_provider

    request = get_payment_request(request_id)

    if request is None or request["method"] != "crypto":
        return request, None

    # Already finalized locally - don't re-activate
    if request["status"] in ("APPROVED", "REJECTED", "CANCELLED", "EXPIRED"):
        return request, request["status"]

    # Expired by our own clock?
    if request["expires_at"]:
        try:
            exp = datetime.fromisoformat(request["expires_at"])
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp and request["status"] == "PENDING_PAYMENT":
                _mark_status(request_id, "EXPIRED")
                return get_payment_request(request_id), "EXPIRED"
        except (TypeError, ValueError):
            pass

    provider = crypto_provider.get_provider()
    provider_payment_id = request["provider_payment_id"]

    if not provider_payment_id:
        return request, "WAITING"

    status = await provider.get_payment_status(provider_payment_id)

    if status == crypto_provider.STATUS_PAID:
        approved = approve_crypto_payment(request_id)
        return get_payment_request(request_id), ("PAID" if approved else "ALREADY_DONE")

    # Persist intermediate state for UI display without finalizing
    mapped_local = {
        crypto_provider.STATUS_WAITING: "PENDING_PAYMENT",
        crypto_provider.STATUS_DETECTING: "DETECTING",
        crypto_provider.STATUS_CONFIRMING: "CONFIRMING",
        crypto_provider.STATUS_FAILED: "FAILED",
        crypto_provider.STATUS_EXPIRED: "EXPIRED",
    }.get(status)

    if mapped_local and mapped_local != request["status"]:
        _mark_status(request_id, mapped_local)

    return get_payment_request(request_id), status


def _mark_status(request_id, status):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("UPDATE payment_requests SET status=? WHERE id=?", (status, request_id))
    conn.commit()
    conn.close()


# ==========================================
# ACTIVATION (shared by admin-approval AND
# verified crypto payments) - idempotent
# ==========================================

def _activate_plan_purchase(request, actor_label):
    """The single place a paid plan purchase becomes real: sets the
    plan, records last_purchase_* for the auto-renew scheduler, checks
    pending referral rewards. Guarded by status so it can only ever
    fire once per request."""
    raise RuntimeError("Payment processing is disabled in this partial hardening release; complete payment acceptance testing before enabling it.")

    from datetime import timedelta

    user_id = request["user_id"]
    plan = request["plan"]
    months = request["months"] or 1

    expiry = (datetime.now(timezone.utc) + timedelta(days=30 * months)).isoformat()

    plan_service.set_user_plan(user_id, plan, expiry=expiry)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET last_purchase_plan=?, last_purchase_months=? WHERE telegram_id=?",
        (plan, months, user_id),
    )
    conn.commit()
    conn.close()

    referral_service.grant_reward_if_pending(user_id, plan)

    logger.info(
        "Plan activated: request=%s user=%s plan=%s months=%s via %s",
        request["id"], user_id, plan, months, actor_label,
    )


def approve_crypto_payment(request_id):
    """Called when the PROVIDER confirms PAID. Idempotent: only flips
    PENDING/DETECTING/CONFIRMING -> APPROVED exactly once."""
    raise RuntimeError("Payment processing is disabled in this partial hardening release; complete payment acceptance testing before enabling it.")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE payment_requests
        SET status='APPROVED', decided_at=CURRENT_TIMESTAMP, decided_by=NULL
        WHERE id=? AND status IN ('PENDING_PAYMENT','DETECTING','CONFIRMING')
        """,
        (request_id,),
    )

    changed = cur.rowcount > 0
    conn.commit()
    conn.close()

    if not changed:
        return None  # already decided - duplicate webhook/poll guard

    request = get_payment_request(request_id)

    if request["purpose"] == "wallet_topup":
        from services import wallet_service
        wallet_service.credit(
            request["user_id"],
            request["amount_inr"],
            reference=f"crypto_topup:{request_id}",
        )
    else:
        _activate_plan_purchase(request, actor_label="crypto_verified")

    return request


# ==========================================
# LEGACY / UPI REQUESTS
# ==========================================

def create_payment_request(user_id, plan, months, method, payment_reference=None):
    """UPI (admin-reviewed) plan purchase with a full price snapshot."""
    raise RuntimeError("Payment processing is disabled in this partial hardening release; complete payment acceptance testing before enabling it.")

    if plan not in PURCHASABLE_PLANS:
        raise ValueError(f"{plan} is not a purchasable plan")

    durations = {row["months"]: row for row in pricing_service.get_duration_options(plan)}
    if months not in durations:
        raise ValueError(f"Invalid duration: {months} months for plan {plan}")

    duration_row = durations[months]
    pricing = pricing_service.calculate_price(
        plan,
        months,
        duration_row["discount_percent"],
        duration_row["price_inr_override"]
    )
    amount_inr = pricing["price_inr"]

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO payment_requests(
            user_id, plan, months, method, currency,
            base_amount, discount_percent, final_amount,
            amount_inr, amount_usd,
            payment_reference, status, purpose
        )
        VALUES (?, ?, ?, ?, 'INR', ?, ?, ?, ?, ?, ?, 'PENDING_PAYMENT', 'plan')
        """,
        (
            user_id, plan, months, method,
            pricing["full_price_inr"], pricing["discount_percent"], amount_inr,
            amount_inr, round(amount_inr / INR_PER_USD, 2),
            payment_reference,
        ),
    )

    conn.commit()
    request_id = cur.lastrowid
    conn.close()

    return request_id


def create_wallet_topup_request(user_id, amount_inr, method, payment_reference=None):
    """Wallet top-up: same lifecycle as a plan purchase but routed to
    wallet credit on approval."""
    raise RuntimeError("Payment processing is disabled in this partial hardening release; complete payment acceptance testing before enabling it.")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO payment_requests(
            user_id, plan, amount_usd, amount_inr, method, currency,
            base_amount, discount_percent, final_amount,
            payment_reference, status, purpose
        )
        VALUES (?, 'WALLET_TOPUP', ?, ?, ?, 'INR', ?, 0, ?, ?, 'PENDING_PAYMENT', 'wallet_topup')
        """,
        (
            user_id, round(amount_inr / INR_PER_USD, 2), amount_inr, method,
            amount_inr, amount_inr, payment_reference,
        ),
    )

    conn.commit()
    request_id = cur.lastrowid
    conn.close()

    return request_id


def get_payment_request(request_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM payment_requests WHERE id=?", (request_id,))
    row = cur.fetchone()

    conn.close()

    return row


def submit_screenshot(request_id, file_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "UPDATE payment_requests SET screenshot_file_id=?, status='SUBMITTED' WHERE id=? AND status='PENDING_PAYMENT'",
        (file_id, request_id),
    )

    conn.commit()
    updated = cur.rowcount > 0
    conn.close()

    return updated


def cancel_payment_request(request_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "UPDATE payment_requests SET status='CANCELLED' WHERE id=? AND status IN ('PENDING_PAYMENT','SUBMITTED')",
        (request_id,),
    )

    conn.commit()
    conn.close()


def approve_payment(request_id, admin_id):
    """Admin approval (UPI screenshots + any manual path). Branches on
    purpose: wallet top-ups credit the wallet, plan purchases activate
    the plan via the shared _activate_plan_purchase."""
def approve_payment(request_id, admin_id):
    """Admin approval: activates subscription, sets expiry, triggers referral reward."""
    request = get_payment_request(request_id)
    if request is None or request["status"] not in ("SUBMITTED", "PENDING_PAYMENT"):
        return None

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE payment_requests SET status='APPROVED', decided_at=CURRENT_TIMESTAMP, decided_by=? WHERE id=?",
        (admin_id, request_id),
    )
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()

    if not changed:
        return None

    # Handle purpose
    if request["purpose"] == "wallet_topup":
        from services import wallet_service
        wallet_service.credit(request["user_id"], request["amount_inr"], reference=f"payment_request:{request_id}")
    elif request["purpose"] == "credits":
        from services.forward_credit_service import adjust as adjust_credits
        try:
            units = int(str(request["plan"]).split()[0].replace(",", ""))
        except Exception:
            units = 500
        adjust_credits(request["user_id"], units, f"upi_credits:{request_id}", "UPI Credits Purchase", actor_id=admin_id)
    else:
        # Plan activation
        user_id = request["user_id"]
        plan = request["plan"]
        months = request["months"] or 1
        from datetime import datetime, timedelta, timezone
        expiry = (datetime.now(timezone.utc) + timedelta(days=30 * months)).isoformat()
        plan_service.set_user_plan(user_id, plan, expiry=expiry)

        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET last_purchase_plan=?, last_purchase_months=? WHERE telegram_id=?",
            (plan, months, user_id),
        )
        conn.commit()
        conn.close()

        referral_service.grant_reward_if_pending(user_id, plan)

    return get_payment_request(request_id)


def approve_crypto_payment(request_id):
    """Activates crypto payment automatically when Oxapay confirms paid."""
    request = get_payment_request(request_id)
    if request is None:
        return None

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE payment_requests SET status='APPROVED', decided_at=CURRENT_TIMESTAMP WHERE id=?",
        (request_id,),
    )
    conn.commit()
    conn.close()

    return approve_payment(request_id, admin_id=0)
    
STALE_HOURS = 48

def expire_stale_requests():
    """Auto-expires stale payment requests past STALE_HOURS."""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT * FROM payment_requests
        WHERE status IN ('PENDING_PAYMENT', 'SUBMITTED')
          AND created_at <= datetime('now', ?)
          AND (method != 'crypto' OR method IS NULL)
        """,
        (f"-{STALE_HOURS} hours",),
    )
    stale = list(cur.fetchall())

    if stale:
        cur.execute(
            """
            UPDATE payment_requests SET status='EXPIRED'
            WHERE status IN ('PENDING_PAYMENT', 'SUBMITTED')
              AND created_at <= datetime('now', ?)
              AND (method != 'crypto' OR method IS NULL)
            """,
            (f"-{STALE_HOURS} hours",),
        )

    conn.commit()
    conn.close()

    return stale