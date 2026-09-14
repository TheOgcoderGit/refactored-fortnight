"""
Per-project log lines (forward / error / retry / info), plus a
lightweight retention trim so the table never grows unbounded on a
long-running deployment.
"""

from database.db import get_connection

MAX_LOGS_PER_PROJECT = 500


def add_log(project_id, level, message):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "INSERT INTO logs(project_id, level, message) VALUES(?, ?, ?)",
        (project_id, level, message)
    )

    # Trim old rows beyond the retention cap for this project.
    cur.execute(
        """
        DELETE FROM logs
        WHERE project_id=?
        AND id NOT IN (
            SELECT id FROM logs
            WHERE project_id=?
            ORDER BY id DESC
            LIMIT ?
        )
        """,
        (project_id, project_id, MAX_LOGS_PER_PROJECT)
    )

    conn.commit()
    conn.close()


def get_logs(project_id, limit=20):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT * FROM logs
        WHERE project_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (project_id, limit)
    )

    rows = cur.fetchall()

    conn.close()

    return rows


def clear_logs(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("DELETE FROM logs WHERE project_id=?", (project_id,))

    conn.commit()
    conn.close()
