"""
ChannelFlow AI - Threads Destination Adapter
===============================================

Publishes content to Threads using Meta's Threads API:
https://developers.facebook.com/docs/threads

Requires OAuth 2.0 flow:
1. User connects their Instagram Business/Creator account (linked to FB Page)
2. Meta App must have: threads_basic, threads_content_publish permissions
3. User grants permission via OAuth redirect
4. Store encrypted access_token + refresh_token
5. Auto-refresh tokens before expiry

This adapter uses the BaseDestination interface.
"""

import logging
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

from config import THREADS_APP_ID, THREADS_APP_SECRET
from destinations.base import BaseDestination, DeliveryContent, DeliveryResult
from database.db import get_connection

logger = logging.getLogger(__name__)

THREADS_API_BASE = "https://graph.threads.net/v1.0"
THREADS_AUTH_URL = "https://threads.net/oauth/authorize"
THREADS_TOKEN_URL = "https://graph.threads.net/oauth/access_token"
THREADS_REFRESH_URL = "https://graph.threads.net/refresh_access_token"


class ThreadsDestination(BaseDestination):
    """Sends content to Threads via Meta Threads API."""

    platform_id = "threads"

    def __init__(self, user_id: int, user_threads_id: str, label: Optional[str] = None):
        """
        Args:
            user_id: ChannelFlow user's telegram_id (for token lookup)
            user_threads_id: The Threads user ID (from me endpoint)
            label: Human-readable name
        """
        self.user_id = user_id
        self.user_threads_id = user_threads_id
        self.label = label or f"Threads ({user_threads_id})"
        self._access_token = None
        self._token_expires_at = 0

    def describe(self) -> str:
        return f"Threads: {self.label}"

    def _load_token(self) -> bool:
        """Load and validate token from database. Returns True if valid."""
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT access_token, refresh_token, expires_at
            FROM platform_accounts
            WHERE user_id = ? AND platform = 'threads' AND account_identifier = ?
            """,
            (self.user_id, self.user_threads_id),
        )
        row = cur.fetchone()
        conn.close()

        if not row:
            return False

        # Check if token is expired or near expiry (refresh 5 min early)
        if row["expires_at"] and row["expires_at"] < time.time() + 300:
            return self._refresh_token(row["refresh_token"])

        self._access_token = row["access_token"]
        self._token_expires_at = row["expires_at"] or 0
        return bool(self._access_token)

    def _refresh_token(self, refresh_token: str) -> bool:
        """Refresh the access token using the refresh token."""
        if not THREADS_APP_ID or not THREADS_APP_SECRET:
            logger.error("Threads app credentials not configured")
            return False

        try:
            with httpx.Client(timeout=30) as http:
                resp = http.post(
                    THREADS_REFRESH_URL,
                    data={
                        "grant_type": "th_refresh_token",
                        "client_id": THREADS_APP_ID,
                        "client_secret": THREADS_APP_SECRET,
                        "refresh_token": refresh_token,
                    },
                )

            data = resp.json()

            if resp.status_code >= 400:
                logger.error("Threads token refresh failed: %s", data)
                return False

            # Store new tokens
            access_token = data.get("access_token")
            new_refresh_token = data.get("refresh_token", refresh_token)
            expires_in = data.get("expires_in", 5184000)  # default 60 days
            expires_at = time.time() + expires_in

            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE platform_accounts
                SET access_token = ?, refresh_token = ?, expires_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND platform = 'threads' AND account_identifier = ?
                """,
                (access_token, new_refresh_token, expires_at, self.user_id, self.user_threads_id),
            )
            conn.commit()
            conn.close()

            self._access_token = access_token
            self._token_expires_at = expires_at
            logger.info("Threads token refreshed for user %s", self.user_id)
            return True

        except Exception as e:
            logger.exception("Threads token refresh error")
            return False

    def _ensure_token(self) -> bool:
        """Ensure we have a valid access token, refreshing if needed."""
        if self._access_token and self._token_expires_at > time.time() + 300:
            return True
        return self._load_token()

    async def send(self, content: DeliveryContent) -> DeliveryResult:
        if not self._ensure_token():
            return DeliveryResult(
                ok=False,
                detail="Threads not connected. Please reconnect in Settings."
            )

        if not THREADS_APP_ID or not THREADS_APP_SECRET:
            return DeliveryResult(ok=False, detail="Threads app not configured")

        # Step 1: Create media container (for media posts)
        # Step 2: Publish the container

        media_container_id = None

        try:
            async with httpx.AsyncClient(timeout=60) as http:
                headers = {
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                }

                if content.media_ref and content.media_type in ("photo", "video", "carousel"):
                    # Create media container
                    container_payload = {
                        "media_type": "IMAGE" if content.media_type == "photo" else "VIDEO",
                    }

                    if content.media_type == "carousel":
                        # For carousel, media_ref should be comma-separated URLs
                        container_payload["media_type"] = "CAROUSEL"
                        container_payload["children"] = content.media_ref.split(",")
                    else:
                        container_payload[content.media_type == "photo" and "image_url" or "video_url"] = content.media_ref

                    if content.text:
                        container_payload["text"] = content.text

                    container_resp = await http.post(
                        f"{THREADS_API_BASE}/{self.user_threads_id}/threads",
                        json=container_payload,
                        headers=headers,
                    )

                    if container_resp.status_code >= 400:
                        error_msg = container_resp.json().get("error", {}).get("message", str(container_resp.text))
                        logger.error("Threads container creation failed: %s", error_msg)
                        return DeliveryResult(ok=False, detail=error_msg)

                    container_data = container_resp.json()
                    media_container_id = container_data.get("id")

                    if not media_container_id:
                        return DeliveryResult(ok=False, detail="No container ID returned")

                    # Publish the container
                    publish_resp = await http.post(
                        f"{THREADS_API_BASE}/{self.user_threads_id}/threads_publish",
                        json={"creation_id": media_container_id},
                        headers=headers,
                    )

                    if publish_resp.status_code >= 400:
                        error_msg = publish_resp.json().get("error", {}).get("message", str(publish_resp.text))
                        logger.error("Threads publish failed: %s", error_msg)
                        return DeliveryResult(ok=False, detail=error_msg)

                    publish_data = publish_resp.json()
                    return DeliveryResult(ok=True, detail=f"Published: {publish_data.get('id', 'unknown')}")

                else:
                    # Text-only post
                    text_payload = {
                        "text": content.text or "",
                    }

                    resp = await http.post(
                        f"{THREADS_API_BASE}/{self.user_threads_id}/threads",
                        json=text_payload,
                        headers=headers,
                    )

                    if resp.status_code >= 400:
                        error_msg = resp.json().get("error", {}).get("message", str(resp.text))
                        logger.error("Threads text post failed: %s", error_msg)
                        return DeliveryResult(ok=False, detail=error_msg)

                    data = resp.json()
                    return DeliveryResult(ok=True, detail=f"Published: {data.get('id', 'unknown')}")

        except httpx.HTTPError as e:
            logger.error("Threads network error: %s", e)
            return DeliveryResult(ok=False, detail=f"Network error: {e}")
        except Exception as e:
            logger.exception("Unexpected error sending to Threads for user %s", self.user_id)
            return DeliveryResult(ok=False, detail=str(e))

    async def validate_connection(self) -> DeliveryResult:
        """Test if the Threads connection is valid by calling /me endpoint."""
        if not self._ensure_token():
            return DeliveryResult(ok=False, detail="Not connected or token invalid")

        try:
            async with httpx.AsyncClient(timeout=15) as http:
                headers = {"Authorization": f"Bearer {self._access_token}"}
                resp = await http.get(f"{THREADS_API_BASE}/me", headers=headers)

                if resp.status_code >= 400:
                    error_msg = resp.json().get("error", {}).get("message", "Invalid token")
                    return DeliveryResult(ok=False, detail=error_msg)

                data = resp.json()
                return DeliveryResult(ok=True, detail=f"Connected as @{data.get('username', 'unknown')}")

        except Exception as e:
            logger.error("Threads validation error: %s", e)
            return DeliveryResult(ok=False, detail=str(e))

    @staticmethod
    def get_oauth_url(redirect_uri: str, state: str = "") -> str:
        """Generate the OAuth authorization URL for connecting Threads."""
        if not THREADS_APP_ID:
            return ""

        params = {
            "client_id": THREADS_APP_ID,
            "redirect_uri": redirect_uri,
            "scope": "threads_basic,threads_content_publish",
            "response_type": "code",
            "state": state,
        }
        return f"{THREADS_AUTH_URL}?{urlencode(params)}"

    @staticmethod
    def exchange_code_for_token(code: str, redirect_uri: str) -> Optional[dict]:
        """Exchange OAuth authorization code for access + refresh tokens."""
        if not THREADS_APP_ID or not THREADS_APP_SECRET:
            return None

        try:
            with httpx.Client(timeout=30) as http:
                resp = http.post(
                    THREADS_TOKEN_URL,
                    data={
                        "client_id": THREADS_APP_ID,
                        "client_secret": THREADS_APP_SECRET,
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code",
                    },
                )

            data = resp.json()

            if resp.status_code >= 400:
                logger.error("Threads code exchange failed: %s", data)
                return None

            return {
                "access_token": data.get("access_token"),
                "refresh_token": data.get("refresh_token"),
                "expires_in": data.get("expires_in", 5184000),
                "user_id": data.get("user_id"),
            }

        except Exception as e:
            logger.exception("Threads code exchange error")
            return None

    @staticmethod
    def store_tokens(user_id: int, user_threads_id: str, token_data: dict) -> bool:
        """Store or update Threads tokens in platform_accounts."""
        try:
            conn = get_connection()
            cur = conn.cursor()
            expires_at = time.time() + token_data.get("expires_in", 5184000)
            cur.execute(
                """
                INSERT INTO platform_accounts (user_id, platform, account_identifier, access_token, refresh_token, expires_at, status)
                VALUES (?, 'threads', ?, ?, ?, ?, 'connected')
                ON CONFLICT(user_id, platform, account_identifier) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    expires_at = excluded.expires_at,
                    status = 'connected',
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, user_threads_id, token_data["access_token"], token_data.get("refresh_token"), expires_at),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.exception("Threads store_tokens error")
            return False


# Backwards-compatible factory
def create_threads_destination(user_id: int, user_threads_id: str, label: str = None) -> ThreadsDestination:
    return ThreadsDestination(user_id, user_threads_id, label)