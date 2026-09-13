"""
ChannelFlow AI - Instagram Destination & Formatting Service
==============================================================

Two things live here, same lazy-row pattern as
services/settings_service.py:

    * CRUD for instagram_destinations (one project can have more than
      one, same as Telegram destinations).
    * instagram_format_settings + format_for_instagram(), the
      Instagram-SPECIFIC layer on top of the shared formatting engine
      (services/formatting_service.py): CTA, hashtags, link placement,
      source attribution, emoji toggle. Base prefix/suffix/remove/
      replace is delegated to formatting_service.apply_formatting()
      rather than reimplemented here - this table used to have its own
      prefix/suffix columns, which duplicated the same feature the
      shared engine now owns. Those columns are left in place (SQLite
      DROP COLUMN portability) but unused; see get_format_settings().

It never touches the affiliate URL itself, only what surrounds it, and
link placement only changes *where* the untouched URL appears in the
caption.
"""

from database.db import get_connection
from services import formatting_service

VALID_TARGET_TYPES = ("broadcast_channel", "feed")
VALID_LINK_PLACEMENTS = ("bottom", "inline")

FORMAT_DEFAULTS = {
    "cta": "",
    "hashtags": "",
    "emoji_enabled": 1,
    "source_attribution": 0,
    "link_placement": "bottom",
}


# ==========================================
# INSTAGRAM DESTINATIONS
# ==========================================

def add_instagram_destination(project_id, ig_user_id, username, title, target_type="broadcast_channel"):

    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"Invalid target_type: {target_type}")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO instagram_destinations
        (project_id, ig_user_id, username, title, target_type)
        VALUES (?, ?, ?, ?, ?)
        """,
        (project_id, ig_user_id, username, title, target_type),
    )

    conn.commit()
    destination_id = cur.lastrowid
    conn.close()

    return destination_id


def get_instagram_destinations(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM instagram_destinations WHERE project_id=? ORDER BY id DESC",
        (project_id,),
    )

    rows = cur.fetchall()
    conn.close()

    return rows


def get_instagram_destination(destination_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM instagram_destinations WHERE id=?", (destination_id,))
    row = cur.fetchone()

    conn.close()

    return row


def delete_instagram_destination(destination_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("DELETE FROM instagram_destinations WHERE id=?", (destination_id,))

    conn.commit()
    conn.close()


def set_destination_status(destination_id, status):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "UPDATE instagram_destinations SET status=? WHERE id=?",
        (status, destination_id),
    )

    conn.commit()
    conn.close()


# ==========================================
# FORMAT SETTINGS
# ==========================================

def get_format_settings(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM instagram_format_settings WHERE project_id=?",
        (project_id,),
    )

    row = cur.fetchone()

    if row is None:

        cur.execute(
            "INSERT INTO instagram_format_settings(project_id) VALUES(?)",
            (project_id,),
        )
        conn.commit()

        cur.execute(
            "SELECT * FROM instagram_format_settings WHERE project_id=?",
            (project_id,),
        )
        row = cur.fetchone()

    conn.close()

    return row


def update_format_settings(project_id, **fields):

    if not fields:
        return

    get_format_settings(project_id)

    allowed = set(FORMAT_DEFAULTS.keys())
    columns = []
    values = []

    for key, value in fields.items():

        if key not in allowed:
            raise ValueError(f"Unknown Instagram format setting: {key}")

        if key == "link_placement" and value not in VALID_LINK_PLACEMENTS:
            raise ValueError(f"Invalid link_placement: {value}")

        columns.append(f"{key}=?")
        values.append(value)

    values.append(project_id)

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        f"UPDATE instagram_format_settings SET {', '.join(columns)} WHERE project_id=?",
        values,
    )

    conn.commit()
    conn.close()


# ==========================================
# CAPTION FORMATTER
# ==========================================

def format_for_instagram(project_id, text_content, urls):
    """
    Builds the Instagram caption: shared prefix/suffix/remove/replace
    (services/formatting_service.py) first, then this project's
    Instagram-specific CTA/hashtags/link-placement layer on top.

    ``urls`` is the list of URLs already present in text_content
    (extracted, not modified - see services/processing_service.py).
    This function never rewrites a URL; link_placement only controls
    whether the caption's own copy of the text already contains them
    inline (placement="inline", text left as-is) or the URLs are
    additionally listed once more under a CTA line at the bottom
    (placement="bottom").
    """

    settings = get_format_settings(project_id)

    base = formatting_service.apply_formatting(project_id, text_content)

    parts = [base]

    if settings["link_placement"] == "bottom" and urls:

        cta = settings["cta"] or "Grab it here"
        link_lines = "\n".join(urls)
        parts.append(f"{cta}\n{link_lines}")

    if settings["hashtags"]:
        parts.append(settings["hashtags"])

    return "\n\n".join(p for p in parts if p and p.strip())
