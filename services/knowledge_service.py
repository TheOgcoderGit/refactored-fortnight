"""
ChannelFlow AI - Knowledge Base Service
=======================================

Reads FAQ / guide articles from the `knowledge_articles` table (seeded in
database/db.py). The bot's Help > FAQ screen renders from here so the
content lives in one place and can be edited by owner/admins later.
"""

from database.db import get_connection


def list_articles(kind="faq", active_only=True):
    conn = get_connection()
    cur = conn.cursor()
    if active_only:
        cur.execute(
            "SELECT * FROM knowledge_articles WHERE kind=? AND active=1 ORDER BY id",
            (kind,),
        )
    else:
        cur.execute(
            "SELECT * FROM knowledge_articles WHERE kind=? ORDER BY id",
            (kind,),
        )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_article(article_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM knowledge_articles WHERE id=?", (article_id,))
    row = cur.fetchone()
    conn.close()
    return row
