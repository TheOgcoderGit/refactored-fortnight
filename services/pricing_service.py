"""
ChannelFlow AI - Pricing Service
==================================

Two independent price books (Prompt 3 section 21/22):

    * UPI / India: INR prices from plan_configs.monthly_price_inr
      (Beginner ₹199, Pro ₹399, Creator ₹799) with plan_durations
      discounts.

    * Crypto: USD prices from plan_configs.crypto_monthly_price_usd
      (Beginner $6.99, Pro $14.99, Creator $19.99). These are
      intentionally NOT an INR conversion - admin configures them
      separately. Duration discounts default to the same percentages
      unless crypto_discount_percent is set on a duration row.

All amounts are frozen onto the payment request at creation time
(price snapshot - Prompt 3 section 26), so later admin price changes
never affect in-flight payments.
"""

import math

from database.db import get_connection
from services import plan_service


def get_duration_options(plan):
    """Every active (plan, months) row, cheapest-first by months."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM plan_durations WHERE plan=? AND active=1 ORDER BY months ASC",
        (plan,),
    )
    rows = cur.fetchall()

    conn.close()

    return rows


# ==========================================
# UPI / INR PRICING
# ==========================================

def calculate_price(plan, months, discount_percent=None, price_inr_override=None):
    """INR pricing for a plan+duration."""

    base_monthly_inr = plan_service.get_plan_monthly_price_inr(plan)

    if base_monthly_inr == 0:
        return {
            "plan": plan,
            "months": months,
            "discount_percent": 0,
            "full_price_inr": 0,
            "price_inr": 0,
            "effective_monthly_inr": 0,
        }

    full_inr = base_monthly_inr * months

    if price_inr_override is not None:
        discounted_inr = price_inr_override
        if full_inr > 0:
            discount_percent = round((1 - discounted_inr / full_inr) * 100, 2)
        else:
            discount_percent = 0
    else:
        discount = discount_percent if discount_percent else 0

        if discount > 0:
            # Round DOWN to nearest 10 - customer never pays more than
            # the exact discounted amount.
            discounted_inr = math.floor(full_inr * (1 - discount / 100) / 10) * 10
        else:
            discounted_inr = full_inr

        discounted_inr = min(discounted_inr, full_inr)

    return {
        "plan": plan,
        "months": months,
        "discount_percent": discount_percent if discount_percent is not None else 0,
        "full_price_inr": full_inr,
        "price_inr": discounted_inr,
        "effective_monthly_inr": round(discounted_inr / months) if months > 0 else 0,
    }


def format_duration_label(pricing: dict) -> str:

    if pricing["price_inr"] == 0:
        return f"{pricing['months']} mo - Free"

    if pricing["discount_percent"] > 0:
        return (
            f"{pricing['months']} mo - ₹{pricing['price_inr']:.0f} "
            f"(₹{pricing['full_price_inr']:.0f}) - save {pricing['discount_percent']:.0f}%"
        )

    return f"{pricing['months']} mo - ₹{pricing['price_inr']:.0f}"


# ==========================================
# CRYPTO / USD PRICING
# ==========================================

def calculate_crypto_price(plan, months, discount_percent=None):
    """USD crypto pricing for a plan+duration. Crypto discounts fall
    back to the same percentage as INR durations unless a separate
    crypto_discount_percent was configured for that row."""

    base_monthly_usd = plan_service.get_plan_crypto_price_usd(plan)

    if base_monthly_usd == 0:
        return {
            "plan": plan,
            "months": months,
            "discount_percent": 0,
            "full_price_usd": 0,
            "price_usd": 0,
            "effective_monthly_usd": 0,
        }

    full_usd = round(base_monthly_usd * months, 2)
    effective_discount = discount_percent if discount_percent is not None else 0

    if effective_discount and effective_discount > 0:
        price_usd = round(full_usd * (1 - effective_discount / 100), 2)
    else:
        price_usd = full_usd

    # Never charge more than undiscounted
    price_usd = min(price_usd, full_usd)

    return {
        "plan": plan,
        "months": months,
        "discount_percent": effective_discount or 0,
        "full_price_usd": full_usd,
        "price_usd": price_usd,
        "effective_monthly_usd": round(price_usd / months, 2) if months > 0 else 0,
    }


def get_crypto_discount_for_duration(duration_row):
    """Returns the crypto-specific discount for a duration row, falling
    back to the standard discount when no override exists."""
    cd = duration_row["crypto_discount_percent"] if "crypto_discount_percent" in duration_row.keys() else None
    return cd if cd is not None else duration_row["discount_percent"]


def format_crypto_label(pricing: dict) -> str:

    if pricing["price_usd"] == 0:
        return f"{pricing['months']} mo - Free"

    if pricing["discount_percent"] > 0:
        return (
            f"{pricing['months']} mo - ${pricing['price_usd']:.2f} "
            f"(was ${pricing['full_price_usd']:.2f}) - save {pricing['discount_percent']:.0f}%"
        )

    return f"{pricing['months']} mo - ${pricing['price_usd']:.2f}"


def set_duration(plan, months, discount_percent, price_inr_override=None, active=True):
    """Admin-only, called from /setduration. Upserts by (plan, months)
    per the UNIQUE constraint on plan_durations."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO plan_durations(plan, months, discount_percent, price_inr_override, active)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(plan, months) DO UPDATE SET
            discount_percent=excluded.discount_percent,
            price_inr_override=excluded.price_inr_override,
            active=excluded.active
        """,
        (plan, months, discount_percent, price_inr_override, 1 if active else 0),
    )

    conn.commit()
    conn.close()


def get_plan_pricing_summary(plan):
    """Get all duration pricing options for a plan with calculated prices."""
    durations = get_duration_options(plan)
    base_price = plan_service.get_plan_monthly_price_inr(plan)
    base_usd = plan_service.get_plan_crypto_price_usd(plan)

    results = []
    for d in durations:
        pricing = calculate_price(
            plan,
            d["months"],
            d["discount_percent"],
            d["price_inr_override"]
        )
        crypto_pricing = calculate_crypto_price(
            plan,
            d["months"],
            get_crypto_discount_for_duration(d)
        )
        results.append({
            "months": d["months"],
            "discount_percent": d["discount_percent"],
            "price_inr_override": d["price_inr_override"],
            "active": d["active"],
            "label": format_duration_label(pricing),
            "price_inr": pricing["price_inr"],
            "full_price_inr": pricing["full_price_inr"],
            "effective_monthly_inr": pricing["effective_monthly_inr"],
            "price_usd": crypto_pricing["price_usd"],
            "full_price_usd": crypto_pricing["full_price_usd"],
            "label_crypto": format_crypto_label(crypto_pricing),
        })

    return {
        "plan": plan,
        "base_monthly_inr": base_price,
        "base_monthly_usd": base_usd,
        "durations": results
    }