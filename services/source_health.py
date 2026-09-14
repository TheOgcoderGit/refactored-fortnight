"""
ChannelFlow AI - Source Health Monitoring
===========================================

Monitors Telegram source health and notifies users when sources
become unhealthy.
"""

import logging
import time
from typing import Optional, List, Dict

from database.db import get_connection

logger = logging.getLogger(__name__)


class SourceHealth:
    """Source health status constants."""
    HEALTHY = "healthy"
    WARNING = "warning"
    DISCONNECTED = "disconnected"
    PERMISSION_REQUIRED = "permission_required"
    UNKNOWN = "unknown"


async def check_source_health(source_id: int) -> Dict:
    """
    Check the health of a Telegram source.
    
    Returns dict with:
    - status: healthy/warning/disconnected/permission_required
    - last_checked: timestamp
    - last_error: error message if any
    - last_message_id: last successfully read message ID
    """
    from services.source_service import get_source
    from core.client import client, ensure_started
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.errors import (
        ChannelPrivateError, UserNotParticipantError, FloodWaitError,
    )

    source = get_source(source_id)
    if not source:
        return {
            "status": SourceHealth.UNKNOWN,
            "error": "Source not found",
            "last_checked": int(time.time()),
        }

    # If no Telegram client is available (not connected), report unknown
    # rather than crashing the health-check loop.
    try:
        await ensure_started()
    except Exception:
        return {
            "status": SourceHealth.UNKNOWN,
            "last_checked": int(time.time()),
            "last_error": "Engine not connected",
        }

    try:
        # Resolve the chat
        chat_id = source["chat_id"]
        try:
            entity = await client.get_entity(int(chat_id))
        except (ValueError, TypeError):
            # Try as username
            entity = await client.get_entity(chat_id)

        # Check if we can access the chat
        full_chat = await client(GetFullChannelRequest(entity))

        # Check if we have read access (we can see messages)
        # Try to get recent messages
        messages = await client.get_messages(entity, limit=1)

        last_message_id = messages[0].id if messages else None

        return {
            "status": SourceHealth.HEALTHY,
            "last_checked": int(time.time()),
            "last_error": None,
            "last_message_id": last_message_id,
            "chat_title": getattr(entity, "title", None),
            "participants_count": getattr(full_chat.full_chat, "participants_count", None),
        }

    except ChannelPrivateError:
        return {
            "status": SourceHealth.PERMISSION_REQUIRED,
            "last_checked": int(time.time()),
            "last_error": "Source is private - bot/account needs to be added",
        }
    except UserNotParticipantError:
        return {
            "status": SourceHealth.DISCONNECTED,
            "last_checked": int(time.time()),
            "last_error": "Bot/account not a member of this source",
        }
    except FloodWaitError as e:
        return {
            "status": SourceHealth.WARNING,
            "last_checked": int(time.time()),
            "last_error": f"Rate limited: wait {e.seconds}s",
        }
    except Exception as e:
        logger.exception("Health check failed for source %s", source_id)
        return {
            "status": SourceHealth.DISCONNECTED,
            "last_checked": int(time.time()),
            "last_error": str(e),
        }


async def check_all_sources_health(user_id: Optional[int] = None) -> List[Dict]:
    """
    Check health of all sources (optionally filtered by user).
    
    Returns list of health check results with source info.
    """
    from services.source_service import get_sources
    from services.project_service import get_projects
    
    # Get all projects for user (or all if no user_id)
    if user_id:
        projects = get_projects(user_id)
    else:
        from database.db import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM projects WHERE status=1")
        projects = [dict(row) for row in cur.fetchall()]
        conn.close()
    
    results = []
    
    for project in projects:
        sources = get_sources(project["id"])
        for source in sources:
            if not source["enabled"]:
                continue
            
            health = await check_source_health(source["id"])
            results.append({
                "source_id": source["id"],
                "project_id": project["id"],
                "project_name": project["name"],
                "source_title": source["title"],
                "source_chat_id": source["chat_id"],
                "source_type": source["chat_type"],
                "health": health,
            })
    
    return results


async def notify_unhealthy_sources():
    """
    Check all sources and notify users of unhealthy sources.
    Runs as a scheduled job.
    """
    from bot.notifier import notify_user
    from database.db import get_connection
    
    results = await check_all_sources_health()
    
    # Group by user
    user_notifications = {}
    
    for result in results:
        health = result["health"]
        if health["status"] not in (SourceHealth.HEALTHY,):
            project_id = result["project_id"]
            
            # Get project owner
            from database.db import get_connection
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM projects WHERE id=?", (result["project_id"],))
            project = cur.fetchone()
            conn.close()
            
            if not project:
                continue
                
            user_id = project["user_id"]
            
            if user_id not in user_notifications:
                user_notifications[user_id] = []
            
            user_notifications[user_id].append(result)
    
    # Send notifications
    for user_id, unhealthy in user_notifications.items():
        lines = [
            "⚠️ Source Health Alert",
            "",
            f"Found {len(unhealthy)} unhealthy source(s):",
            "",
        ]
        
        for result in unhealthy:
            health = result["health"]
            lines.append(
                f"• {result['source_title']} ({result['source_chat_id']}) "
                f"in {result['project_name']}: {health['status'].upper()}"
            )
            if health.get("last_error"):
                lines.append(f"  Error: {health['last_error']}")
            lines.append("")
        
        lines.append("Check the project dashboard for details.")
        
        try:
            from bot.notifier import notify_user
            await notify_user(user_id, "\n".join(lines))
        except Exception:
            logger.exception(f"Failed to notify user {user_id} about unhealthy sources")


async def record_source_health(source_id: int, health_data: Dict):
    """Record health check result to database for history tracking."""
    from database.db import get_connection
    
    conn = get_connection()
    cur = conn.cursor()
    
    # Create health log table if not exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS source_health_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            last_message_id INTEGER,
            error TEXT,
            checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
        )
    """)
    
    cur.execute(
        """
        INSERT INTO source_health_log(source_id, status, last_message_id, error)
        VALUES (?, ?, ?, ?)
        """,
        (source_id, health_data.get("status"), health_data.get("last_message_id"), health_data.get("last_error")),
    )
    
    conn.commit()
    conn.close()


def get_source_health_history(source_id: int, limit: int = 100):
    """Get health check history for a source."""
    from database.db import get_connection
    
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute(
        """
        SELECT * FROM source_health_log
        WHERE source_id=?
        ORDER BY checked_at DESC
        LIMIT ?
        """,
        (source_id, limit),
    )
    
    rows = cur.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]