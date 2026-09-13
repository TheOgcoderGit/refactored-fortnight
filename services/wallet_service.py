"""
ChannelFlow AI - Wallet Service
===================================

Dual-currency wallet: users keep SEPARATE INR and USD balances
(users.wallet_balance_inr and users.wallet_balance_usd), never mixed.
Each user has a preferred wallet_currency ('INR' or 'USD').

Every balance change writes one immutable ledger row in wallet_transactions
(amount_inr + amount_usd columns both stored; the relevant one is the
denominated amount). debit() enforces non-negative balance atomically.

Idempotency: a UNIQUE index on wallet_transactions.reference makes
duplicate charges (same reference) impossible - credit/debit treat a
duplicate reference as a successful no-op. This is what makes per-forward
billing idempotent across retries.
"""

from database.db import get_connection
from config import INR_PER_USD


def _ensure_user(user_id):
    """Return the user row, creating a defaults row if missing."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id=?", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users(telegram_id, wallet_balance_inr, wallet_balance_usd, wallet_currency) "
            "VALUES (?, 0, 0, 'INR')",
            (user_id,),
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE telegram_id=?", (user_id,))
        row = cur.fetchone()
    conn.close()
    return row


def get_balance_inr(user_id) -> float:
    row = _ensure_user(user_id)
    return row["wallet_balance_inr"] or 0.0


def get_balance_usd(user_id) -> float:
    """Independent USD balance (NOT derived from INR)."""
    row = _ensure_user(user_id)
    return row["wallet_balance_usd"] or 0.0


def get_wallet_currency(user_id) -> str:
    row = _ensure_user(user_id)
    cur = row["wallet_currency"] or "INR"
    return cur if cur in ("INR", "USD") else "INR"


def set_wallet_currency(user_id, currency: str):
    if currency not in ("INR", "USD"):
        raise ValueError("Currency must be INR or USD")
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET wallet_currency=? WHERE telegram_id=?",
        (currency, user_id),
    )
    conn.commit()
    conn.close()


def credit(user_id, amount_inr, reference=None):
    """Backward-compatible INR credit."""
    return credit_currency(user_id, amount_inr, "INR", reference)


def debit(user_id, amount_inr, reference=None) -> bool:
    """Backward-compatible INR debit."""
    return debit_currency(user_id, amount_inr, "INR", reference)


def credit_currency(user_id, amount, currency="INR", reference=None):
    """Atomically credit the named currency balance and write a CREDIT
    ledger row. Duplicate `reference` is a no-op (idempotent)."""

    if amount <= 0:
        raise ValueError("Credit amount must be positive")

    conn = get_connection()
    try:
        cur = conn.cursor()
        conn.execute("BEGIN IMMEDIATE")

        if reference is not None:
            cur.execute(
                "SELECT 1 FROM wallet_transactions WHERE reference=?",
                (reference,),
            )
            if cur.fetchone():
                conn.commit()
                return _balance_of(user_id, currency)

        if currency == "USD":
            cur.execute(
                "UPDATE users SET wallet_balance_usd = wallet_balance_usd + ? WHERE telegram_id=?",
                (amount, user_id),
            )
        else:
            cur.execute(
                "UPDATE users SET wallet_balance_inr = wallet_balance_inr + ? WHERE telegram_id=?",
                (amount, user_id),
            )

        amount_usd = round(amount if currency == "USD" else amount / INR_PER_USD, 2)
        amount_inr = round(amount if currency == "INR" else amount * INR_PER_USD, 2)
        cur.execute(
            "INSERT INTO wallet_transactions(user_id, amount_usd, amount_inr, direction, reference) VALUES (?, ?, ?, 'CREDIT', ?)",
            (user_id, amount_usd, amount_inr, reference),
        )
        conn.commit()
        return _balance_of(user_id, currency)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def debit_currency(user_id, amount, currency="INR", reference=None) -> bool:
    """Atomically debit the named currency balance (never negative).
    Duplicate `reference` is a no-op returning True (idempotent)."""

    if amount <= 0:
        raise ValueError("Debit amount must be positive")

    conn = get_connection()
    try:
        cur = conn.cursor()
        conn.execute("BEGIN IMMEDIATE")

        if reference is not None:
            cur.execute(
                "SELECT 1 FROM wallet_transactions WHERE reference=?",
                (reference,),
            )
            if cur.fetchone():
                conn.commit()
                return True

        col = "wallet_balance_usd" if currency == "USD" else "wallet_balance_inr"
        cur.execute(f"SELECT {col} FROM users WHERE telegram_id=?", (user_id,))
        row = cur.fetchone()

        if row is None or row[col] < amount:
            conn.rollback()
            return False

        cur.execute(
            f"UPDATE users SET {col} = {col} - ? WHERE telegram_id=?",
            (amount, user_id),
        )

        amount_usd = round(amount if currency == "USD" else amount / INR_PER_USD, 2)
        amount_inr = round(amount if currency == "INR" else amount * INR_PER_USD, 2)
        cur.execute(
            "INSERT INTO wallet_transactions(user_id, amount_usd, amount_inr, direction, reference) VALUES (?, ?, ?, 'DEBIT', ?)",
            (user_id, amount_usd, amount_inr, reference),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _balance_of(user_id, currency):
    return get_balance_usd(user_id) if currency == "USD" else get_balance_inr(user_id)


def charge_forward(user_id, currency=None, reference=None) -> bool:
    """Legacy compatibility API. Per-forward billing is disabled.

    Forwarding is governed by plan daily limits and extra-forward credits;
    this function intentionally performs no wallet deduction.
    """
    return True

def reserve_forward_charge(user_id, dedup_claim_id, currency=None) -> bool:
    """Legacy compatibility API; per-forward reservations are disabled."""
    return True


def confirm_forward_charge(dedup_claim_id) -> bool:
    """Legacy compatibility API; no per-forward debit is recorded."""
    return True


def release_forward_charge(dedup_claim_id) -> bool:
    """Legacy compatibility API; no per-forward reservation exists."""
    return True

def get_transactions(user_id, limit=20):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM wallet_transactions WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    rows = cur.fetchall()

    conn.close()

    return rows


def set_auto_renew(user_id, enabled: bool):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "UPDATE users SET auto_renew=? WHERE telegram_id=?",
        (1 if enabled else 0, user_id),
    )

    conn.commit()
    conn.close()


def is_auto_renew_enabled(user_id) -> bool:

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT auto_renew FROM users WHERE telegram_id=?", (user_id,))
    row = cur.fetchone()

    conn.close()

    return bool(row and row["auto_renew"])
