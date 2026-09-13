from database.db import get_connection


def add_source(
    project_id,
    chat_id,
    username,
    title,
    chat_type
):

    conn = get_connection()
    cur = conn.cursor()

    # Duplicate Check
    cur.execute(
        """
        SELECT id
        FROM sources
        WHERE project_id=?
        AND chat_id=?
        """,
        (project_id, chat_id)
    )

    if cur.fetchone():

        conn.close()

        return False

    cur.execute(
        """
        INSERT INTO sources
        (
            project_id,
            chat_id,
            username,
            title,
            chat_type
        )
        VALUES
        (
            ?, ?, ?, ?, ?
        )
        """,
        (
            project_id,
            chat_id,
            username,
            title,
            chat_type
        )
    )

    conn.commit()
    conn.close()

    return True


def get_sources(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM sources
        WHERE project_id=?
        ORDER BY id DESC
        """,
        (project_id,)
    )

    rows = cur.fetchall()

    conn.close()

    return rows


def get_source(source_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM sources
        WHERE id=?
        """,
        (source_id,)
    )

    row = cur.fetchone()

    conn.close()

    return row


def delete_source(source_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        DELETE FROM sources
        WHERE id=?
        """,
        (source_id,)
    )

    conn.commit()
    conn.close()


def get_source_chat_ids(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT chat_id
        FROM sources
        WHERE project_id=?
        """,
        (project_id,)
    )

    rows = [str(row["chat_id"]) for row in cur.fetchall()]

    conn.close()

    return rows


def toggle_source_enabled(source_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT enabled FROM sources WHERE id=?", (source_id,))
    row = cur.fetchone()

    if row is None:
        conn.close()
        return None

    new_value = 0 if row["enabled"] else 1

    cur.execute(
        "UPDATE sources SET enabled=? WHERE id=?",
        (new_value, source_id)
    )

    conn.commit()
    conn.close()

    return new_value


def get_enabled_source_chat_ids(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT chat_id
        FROM sources
        WHERE project_id=?
        AND enabled=1
        """,
        (project_id,)
    )

    rows = [str(row["chat_id"]) for row in cur.fetchall()]

    conn.close()

    return rows


def count_sources(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT COUNT(*)
        FROM sources
        WHERE project_id=?
        """,
        (project_id,)
    )

    total = cur.fetchone()[0]

    conn.close()

    return total