"""
ChannelFlow AI - Bot-wide persisted configuration (key/value).

Holds settings that must survive a bot restart (unlike bot_data which
lives only in memory), e.g. maintenance mode. Written by owner/admin
actions and read on startup so a restart never silently drops the state.
"""

from database.db import get_connection

DEFAULTS = {
    "maintenance_mode": "0",
    "maintenance_note": "",
}


def _ensure_schema():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS app_config(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    for k, v in DEFAULTS.items():
        cur.execute("INSERT OR IGNORE INTO app_config(key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


def get(key, default=None):
    _ensure_schema()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_config WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return default
    return row["value"]


def get_bool(key, default=False) -> bool:
    val = get(key, None)
    if val is None:
        return default
    return str(val).lower() in ("1", "true", "yes", "on")


def set(key, value):
    _ensure_schema()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO app_config(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()
