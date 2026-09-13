from database.db import get_connection


def register_user(telegram_id, username, first_name):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO users(telegram_id, username, first_name, wallet_balance_inr, wallet_balance_usd, wallet_currency, auto_renew, status)
        VALUES(?, ?, ?, 0, 0, 'INR', 0, 'active')
        ON CONFLICT(telegram_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (telegram_id, username, first_name))

    conn.commit()
    conn.close()
    from services.plan_service import start_trial
    start_trial(telegram_id)


def is_admin(telegram_id, admin_ids):
    """
    An account is admin if it's listed in the ADMIN_IDS env setting.
    Kept as a pure function (no DB read) since the admin list is a
    deploy-time config value, not user-editable state.
    """

    return telegram_id in admin_ids


def get_all_user_ids():

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT telegram_id FROM users")

    rows = [row["telegram_id"] for row in cur.fetchall()]

    conn.close()

    return rows


def count_users():

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users")

    total = cur.fetchone()[0]

    conn.close()

    return total
