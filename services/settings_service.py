# ChannelFlow AI - Per-Project Settings Service
# =============================================

from database.db import get_connection

VALID_MODES = ("forward", "copy")
VALID_MEDIA_FILTERS = (
    "all", "text", "photo", "video", "audio", "document",
    "voice", "sticker", "poll", "animation", "video_note",
)

DEFAULTS = {
    "mode": "forward",
    "silent": 0,
    "protect_content": 0,
    "keep_media_groups": 1,
    "delay_min": 0.0,
    "delay_max": 0.0,
    "media_filter": "all",
    "keyword_whitelist": "",
    "keyword_blacklist": "",
    "regex_filter": "",
    # PRD §14.1 Post Edit Sync. Added by database/hardening.py as an
    # additive column, so old databases keep working. Off by default:
    # editing someone else's channel posts is a surprising side effect
    # and must be an explicit opt-in.
    "post_edit_sync": 0,
}


def get_settings(project_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM project_settings WHERE project_id=?", (project_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute("INSERT INTO project_settings(project_id) VALUES(?)", (project_id,))
        conn.commit()
        cur.execute("SELECT * FROM project_settings WHERE project_id=?", (project_id,))
        row = cur.fetchone()

    conn.close()
    return dict(row) if row else dict(DEFAULTS)


def update_settings(project_id, **fields):
    if not fields:
        return
    get_settings(project_id)

    allowed = set(DEFAULTS.keys())
    columns = []
    values = []

    for key, value in fields.items():
        if key not in allowed:
            continue
        columns.append(f"{key}=?")
        values.append(value)

    if not columns:
        return

    values.append(project_id)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"UPDATE project_settings SET {', '.join(columns)} WHERE project_id=?", values)
    conn.commit()
    conn.close()


def toggle_mode(project_id):
    settings = get_settings(project_id)
    new_mode = "copy" if settings["mode"] == "forward" else "forward"
    update_settings(project_id, mode=new_mode)
    return new_mode


def toggle_flag(project_id, field):
    if field not in ("silent", "protect_content", "keep_media_groups"):
        return 0
    settings = get_settings(project_id)
    new_value = 0 if settings.get(field, 0) else 1
    update_settings(project_id, **{field: new_value})
    return new_value


def set_delay(project_id, delay_min, delay_max):
    delay_min = max(0.0, float(delay_min))
    delay_max = max(delay_min, float(delay_max))
    update_settings(project_id, delay_min=delay_min, delay_max=delay_max)


def set_media_filter(project_id, media_filter):
    if media_filter not in VALID_MEDIA_FILTERS:
        media_filter = "all"
    update_settings(project_id, media_filter=media_filter)


def set_keyword_whitelist(project_id, text):
    update_settings(project_id, keyword_whitelist=(text or "").strip())


def set_keyword_blacklist(project_id, text):
    update_settings(project_id, keyword_blacklist=(text or "").strip())


def set_regex_filter(project_id, pattern):
    update_settings(project_id, regex_filter=(pattern or "").strip())


def set_post_edit_sync(project_id, enabled):
    """PRD §14.1 — mirror source-message edits onto the forwarded copy.

    Stored on project_settings so core/forwarder.py can check it without a
    second query when an edit event arrives.
    """
    update_settings(project_id, post_edit_sync=1 if enabled else 0)