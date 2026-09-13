"""
ChannelFlow AI - Post Edit Synchronization Service
Implements PRD §16 (F153-F155): Mirrors edits from source channels to destinations.
"""
import logging
from database.db import get_connection

logger = logging.getLogger(__name__)

def record_mapping(project_id: int, source_chat_id: str, source_msg_id: int, target_chat_id: str, target_msg_id: int):
    """Records mapping between source and forwarded destination message."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO post_edit_mappings(project_id, source_chat_id, source_message_id, target_chat_id, target_message_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(project_id, source_chat_id, source_message_id, target_chat_id) 
        DO UPDATE SET target_message_id=excluded.target_message_id
    """, (project_id, str(source_chat_id), source_msg_id, str(target_chat_id), target_msg_id))
    conn.commit()
    conn.close()

def get_target_messages(project_id: int, source_chat_id: str, source_msg_id: int):
    """Retrieves all destination messages mapped to a given source message."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT target_chat_id, target_message_id 
        FROM post_edit_mappings
        WHERE project_id=? AND source_chat_id=? AND source_message_id=?
    """, (project_id, str(source_chat_id), source_msg_id))
    rows = cur.fetchall()
    conn.close()
    return rows

async def sync_message_edit(client, project_id: int, source_chat_id: str, source_msg_id: int, new_text: str):
    """Updates mapped messages across destination channels."""
    mappings = get_target_messages(project_id, source_chat_id, source_msg_id)
    if not mappings:
        return

    for row in mappings:
        target_chat = int(row["target_chat_id"])
        target_msg = int(row["target_message_id"])
        try:
            await client.edit_message(target_chat, target_msg, new_text)
            logger.info("Synchronized edit: Source %s:%s -> Target %s:%s", source_chat_id, source_msg_id, target_chat, target_msg)
        except Exception as e:
            logger.warning("Failed to edit target message %s in %s: %s", target_msg, target_chat, e)