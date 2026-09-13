from database.db import get_connection


def create_project(user_id, name, platform_type="telegram"):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO projects
        (
            user_id,
            name,
            status,
            platform_type,
            promo_enabled
        )
        VALUES
        (
            ?, ?, 0, ?, 1
        )
    """, (user_id, name, platform_type))

    conn.commit()

    project_id = cur.lastrowid

    conn.close()

    return project_id


def get_projects(user_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            id,
            name,
            status,
            platform_type,
            promo_enabled,
            created_at
        FROM projects
        WHERE user_id=?
        ORDER BY id DESC
    """, (user_id,))

    rows = cur.fetchall()

    conn.close()

    return rows


def get_all_projects():
    """Every project across every user - used by /admin and /broadcast
    targeting, never exposed to normal users."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            id,
            user_id,
            name,
            status,
            platform_type,
            promo_enabled,
            created_at
        FROM projects
        ORDER BY id DESC
    """)

    rows = cur.fetchall()

    conn.close()

    return rows


def get_project(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM projects
        WHERE id=?
    """, (project_id,))

    row = cur.fetchone()

    conn.close()

    return row


def rename_project(project_id, new_name):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        UPDATE projects
        SET name=?
        WHERE id=?
    """, (new_name, project_id))

    conn.commit()
    conn.close()


def update_status(project_id, status):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        UPDATE projects
        SET status=?
        WHERE id=?
    """, (status, project_id))

    conn.commit()
    conn.close()


def set_platform_type(project_id, platform_type):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "UPDATE projects SET platform_type=? WHERE id=?",
        (platform_type, project_id),
    )

    conn.commit()
    conn.close()


def set_processing_channel(project_id, chat_id, username, title):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE projects
        SET processing_chat_id=?, processing_username=?, processing_title=?
        WHERE id=?
        """,
        (chat_id, username, title, project_id),
    )

    conn.commit()
    conn.close()


def set_promo_enabled(project_id, enabled):
    """Admin/owner override only - normal users cannot call this per the
    mandatory-participation business rule (enforced in bot/handlers.py's
    admin check, not here, since this is a plain data-layer function)."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "UPDATE projects SET promo_enabled=? WHERE id=?",
        (1 if enabled else 0, project_id),
    )

    conn.commit()
    conn.close()


def delete_project(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM projects WHERE id=?",
        (project_id,)
    )

    conn.commit()
    conn.close()


def count_projects(user_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT COUNT(*) FROM projects WHERE user_id=?",
        (user_id,)
    )

    total = cur.fetchone()[0]

    conn.close()

    return total


def project_exists(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT id FROM projects WHERE id=?",
        (project_id,)
    )

    exists = cur.fetchone() is not None

    conn.close()

    return exists