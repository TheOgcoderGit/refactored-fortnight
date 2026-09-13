"""
ChannelFlow AI - Auto Reaction Service
Implements PRD §16 (F156-F158): Idempotent automatic emoji reactions.
"""
import logging
from database.db import get_connection

logger = logging.getLogger(__name__)

def is_reaction_confirmed(project_id: int, chat_id: str, message_id: int, emoji: str) -> bool:
    """Verifies if this message was already reacted to."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM confirmed_reactions 
        WHERE project_id=? AND chat_id=? AND message_id=? AND emoji=?
    """, (project_id, str(chat_id), message_id, emoji))
    row = cur.fetchone()
    conn.close()
    return row is not None

def confirm_reaction(project_id: int, chat_id: str, message_id: int, emoji: str):
    """Persists reaction record to guarantee idempotency across restarts."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO confirmed_reactions(project_id, chat_id, message_id, emoji)
        VALUES(?, ?, ?, ?)
    """, (project_id, str(chat_id), message_id, emoji))
    conn.commit()
    conn.close()

def _send_reaction_request():
    """Imported lazily so a Telethon layout change - or a test double that
    does not model telethon.tl.functions - cannot break `import services`
    for every other feature in the app."""
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji
    return SendReactionRequest, ReactionEmoji


async def apply_auto_reaction(client, project_id: int, chat_id: int, message_id: int, emoji: str = "👍"):
    """Applies reaction safely without interrupting the message pipeline."""
    emoji = (emoji or "👍").strip()
    if not emoji:
        return
    if is_reaction_confirmed(project_id, str(chat_id), message_id, emoji):
        return

    try:
        SendReactionRequest, ReactionEmoji = _send_reaction_request()
        await client(SendReactionRequest(
            peer=chat_id,
            msg_id=message_id,
            reaction=[ReactionEmoji(emoticon=emoji)]
        ))
        confirm_reaction(project_id, str(chat_id), message_id, emoji)
        logger.info("Auto reaction %s applied to %s in chat %s", emoji, message_id, chat_id)
    except Exception as e:
        logger.warning("Auto reaction skipped (chat %s, msg %s): %s", chat_id, message_id, e)