"""
ChannelFlow AI - User Notification Preferences
=============================================

Per-user toggles for the Settings > Notifications screen. Defaults to all
enabled; users can opt out of non-essential categories. Essential account
messages (payment approval/rejection, security) are always delivered
regardless of these settings.
"""

from database.db import get_connection

NOTIFICATION_TYPES = [
    ("marketing", "📣 Promotions & giveaways"),
    ("product_updates", "🆕 Product updates & new features"),
    ("referral_rewards", "🎁 Referral & reward notices"),
    ("weekly_digest", "📊 Weekly activity digest"),
]


def _ensure(user_id):
    conn = get_connection()
    cur = conn.cursor()
    for key, _ in NOTIFICATION_TYPES:
        cur.execute(
            "INSERT OR IGNORE INTO user_notification_prefs(user_id, pref_key, enabled) VALUES (?, ?, 1)",
            (user_id, key),
        )
    conn.commit()
    conn.close()


def get_prefs(user_id):
    _ensure(user_id)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT pref_key, enabled FROM user_notification_prefs WHERE user_id=?",
        (user_id,),
    )
    rows = {r["pref_key"]: bool(r["enabled"]) for r in cur.fetchall()}
    conn.close()
    return rows


def set_pref(user_id, key, enabled: bool):
    _ensure(user_id)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE user_notification_prefs SET enabled=? WHERE user_id=? AND pref_key=?",
        (1 if enabled else 0, user_id, key),
    )
    conn.commit()
    conn.close()


def is_enabled(user_id, key) -> bool:
    prefs = get_prefs(user_id)
    return prefs.get(key, True)
