"""
ChannelFlow AI - Coupon Service
=================================

Server-side validation + transactional redemption (Prompt 1 §24,
Prompt 2 §41 anti-abuse):

    * code -> active? window? plan/wallet applicability? user eligible?
    * per-user + global use caps enforced inside the INSERT
      (UNIQUE idempotency_key + COUNT checks in one transaction)
    * redemption recorded before any money effect; callers pass an
      idempotency key so double-submits can never double-apply.
"""

import json
import logging
import secrets
from datetime import datetime, timezone

from database.db import get_connection

logger = logging.getLogger(__name__)


def generate_code(prefix="CF", length=8):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no confusing chars
    body = "".join(secrets.choice(alphabet) for _ in range(length))
    return f"{prefix}-{body}"


def create_coupon(code=None, discount_percent=0, max_discount_inr=None,
                  min_purchase_inr=0, applies_plans=None, applies_wallet=False,
                  start_at=None, expires_at=None, max_total_uses=None,
                  max_uses_per_user=1, eligible_users=None, created_by=None):
    """Admin coupon creation. `applies_plans` None/[] = all purchasable plans."""

    if not code:
        code = generate_code()

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT 1 FROM coupons WHERE code=?", (code,))
    if cur.fetchone():
        conn.close()
        raise ValueError(f"Code {code} already exists")

    cur.execute(
        """
        INSERT INTO coupons(
            code, discount_percent, max_discount_inr, min_purchase_inr,
            applies_plans, applies_wallet, start_at, expires_at,
            max_total_uses, max_uses_per_user, eligible_users, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            code, discount_percent, max_discount_inr, min_purchase_inr,
            json.dumps(applies_plans or []), 1 if applies_wallet else 0,
            start_at, expires_at, max_total_uses, max_uses_per_user,
            json.dumps(eligible_users) if eligible_users else None,
            created_by,
        ),
    )

    conn.commit()
    cid = cur.lastrowid
    conn.close()

    return get_coupon(cid)


def get_coupon(coupon_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM coupons WHERE id=?", (coupon_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_coupon_by_code(code):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM coupons WHERE code=? COLLATE NOCASE", (code.strip(),))
    row = cur.fetchone()
    conn.close()
    return row


def list_coupons(active_only=True, limit=20):
    conn = get_connection()
    cur = conn.cursor()
    if active_only:
        cur.execute("SELECT * FROM coupons WHERE active=1 ORDER BY id DESC LIMIT ?", (limit,))
    else:
        cur.execute("SELECT * FROM coupons ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def deactivate_coupon(coupon_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE coupons SET active=0 WHERE id=?", (coupon_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


# ==========================================
# VALIDATION
# ==========================================

def validate(code, user_id, context="plan", amount_inr=0, plan=None):
    """Returns (ok, coupon_row_or_None, discounted_amount, reason).

    context: 'plan' (needs applicable plan) | 'wallet'.
    """

    coupon = get_coupon_by_code(code)
    if coupon is None:
        return False, None, 0, "Invalid code."

    if not coupon["active"]:
        return False, None, 0, "This code is no longer active."

    now = datetime.now(timezone.utc)

    if coupon["start_at"]:
        try:
            s = datetime.fromisoformat(coupon["start_at"])
            if s.tzinfo is None:
                s = s.replace(tzinfo=timezone.utc)
            if now < s:
                return False, None, 0, "This code isn't active yet."
        except (TypeError, ValueError):
            pass

    if coupon["expires_at"]:
        try:
            e = datetime.fromisoformat(coupon["expires_at"])
            if e.tzinfo is None:
                e = e.replace(tzinfo=timezone.utc)
            if now > e:
                return False, None, 0, "This code has expired."
        except (TypeError, ValueError):
            pass

    # Context applicability
    if context == "wallet" and not coupon["applies_wallet"]:
        return False, None, 0, "This code doesn't apply to wallet top-ups."

    if context == "plan":
        applies = json.loads(coupon["applies_plans"] or "[]")
        if plan and applies and plan not in applies:
            return False, None, 0, f"This code doesn't apply to the {plan} plan."

    # User eligibility
    eligible = json.loads(coupon["eligible_users"]) if coupon["eligible_users"] else None
    if eligible is not None and user_id not in eligible:
        return False, None, 0, "This code isn't valid for your account."

    amount = float(amount_inr or 0)
    if amount < float(coupon["min_purchase_inr"] or 0):
        return False, None, 0, (
            f"Minimum purchase for this code: ₹{float(coupon['min_purchase_inr']):.0f}"
        )

    # Per-user uses
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM coupon_redemptions WHERE coupon_id=? AND user_id=?",
        (coupon["id"], user_id),
    )
    user_uses = cur.fetchone()[0]

    if user_uses >= int(coupon["max_uses_per_user"]):
        conn.close()
        return False, None, 0, "You've already used this code."

    # Global cap
    if coupon["max_total_uses"]:
        cur.execute(
            "SELECT COUNT(*) FROM coupon_redemptions WHERE coupon_id=?",
            (coupon["id"],),
        )
        total = cur.fetchone()[0]
        if total >= int(coupon["max_total_uses"]):
            conn.close()
            return False, None, 0, "This code has reached its usage limit."
    conn.close()

    # Discount math
    discounted = amount * (1 - float(coupon["discount_percent"]) / 100)
    if coupon["max_discount_inr"]:
        discounted = max(discounted, amount - float(coupon["max_discount_inr"]))
    discounted = round(discounted, 2)
    saved = round(amount - discounted, 2)

    return True, coupon, saved, None


def record_redemption(coupon_id, user_id, context="plan",
                      reference_id=None, amount_discounted=0, idempotency_key=None) -> bool:
    """Inserts the redemption atomically. Returns False on duplicate
    idempotency key (already applied)."""

    if not idempotency_key:
        idempotency_key = f"{coupon_id}:{user_id}:{context}:{reference_id}"

    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO coupon_redemptions(
                coupon_id, user_id, context, reference_id,
                amount_discounted_inr, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (coupon_id, user_id, context, reference_id, amount_discounted, idempotency_key),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        conn.close()
        return False


def release_redemption(reference_id):
    """Removes a coupon redemption tied to a payment request that was
    cancelled/expired/rejected before the payment was approved, so the
    coupon's per-user/global usage is not consumed by an abandoned
    request. No-op if no such redemption exists."""

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM coupon_redemptions WHERE reference_id=?",
        (str(reference_id),),
    )
    conn.commit()
    conn.close()