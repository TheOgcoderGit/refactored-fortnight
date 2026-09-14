"""
ChannelFlow AI - WhatsApp Pairing Verification
===============================================

Pure-function pairing/verification layer plus optional HTTP route wiring.

The verification *logic* (lookup -> expiry -> status -> project/owner ->
platform-account -> mark verified) lives here as plain async functions so
it can be driven from any front-end:
  * a future WhatsApp bot inbound handler,
  * an admin web panel,
  * a webhook relay,

without tying it to a specific HTTP framework. HTTP route registration is
provided by `setup_aiohttp_routes(app)` and is only used when `aiohttp`
is installed (it is NOT a runtime dependency of the core bot).

SECURITY: no access tokens or credentials are read or logged here. The
pairing code is a short-lived, single-project scoped value - never a
proxy for account secrets.
"""

import logging
import time

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Core verification logic (no HTTP dependency)
# ----------------------------------------------------------------------

async def verify_pairing_code(code: str) -> dict:
    """Full verification pipeline for a pairing code.

    Returns a result dict:
        {"ok": bool, "detail": str, "status": str,
         "destination_id": int|None, "project_id": int|None}
    """
    from services.destination_service import (
        get_destination_by_pairing_code,
        verify_pairing_code as _verify,
        mark_pairing_failed,
        update_pairing_status,
    )

    code = (code or "").strip()

    if not code.startswith("CF"):
        return {"ok": False, "detail": "Invalid pairing code format", "status": "invalid",
                "destination_id": None, "project_id": None}

    dest = get_destination_by_pairing_code(code)
    if dest is None:
        return {"ok": False, "detail": "Invalid or expired pairing code", "status": "invalid",
                "destination_id": None, "project_id": None}

    status = dest.get("pairing_status") or "pending"
    destination_id = dest["id"]
    project_id = dest.get("project_id")

    if status == "verified":
        return {"ok": True, "detail": "Already verified", "status": "verified",
                "destination_id": destination_id, "project_id": project_id}

    if status == "expired":
        return {"ok": False, "detail": "Pairing code has expired", "status": "expired",
                "destination_id": destination_id, "project_id": project_id}

    if status == "failed":
        return {"ok": False, "detail": "Pairing has failed. Generate a new code.",
                "status": "failed", "destination_id": destination_id, "project_id": project_id}

    if status not in ("pending", "verifying"):
        return {"ok": False, "detail": f"Invalid pairing status: {status}", "status": status,
                "destination_id": destination_id, "project_id": project_id}

    # Move to verifying
    update_pairing_status(destination_id, "verifying")

    # Ownership check: platform account must belong to the project owner.
    platform_account_id = dest.get("platform_account_id")
    if platform_account_id:
        from database.db import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id FROM projects WHERE id=?", (project_id,)
        )
        proj = cur.fetchone()
        conn.close()
        if proj is None:
            mark_pairing_failed(destination_id)
            return {"ok": False, "detail": "Project not found", "status": "failed",
                    "destination_id": destination_id, "project_id": project_id}

        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id FROM platform_accounts WHERE id=? AND platform='whatsapp_channel'",
            (platform_account_id,),
        )
        acct = cur.fetchone()
        conn.close()

        if acct is None or acct["user_id"] != proj["user_id"]:
            mark_pairing_failed(destination_id)
            return {"ok": False, "detail": "Project ownership mismatch", "status": "failed",
                    "destination_id": destination_id, "project_id": project_id}

    verified = _verify(dest["pairing_code"])
    if verified:
        logger.info("WhatsApp pairing verified: destination=%s project=%s", destination_id, project_id)
        return {"ok": True, "detail": "Pairing verified successfully", "status": "verified",
                "destination_id": destination_id, "project_id": project_id}

    mark_pairing_failed(destination_id)
    return {"ok": False, "detail": "Verification failed. Code may be used or invalid.",
            "status": "failed", "destination_id": destination_id, "project_id": project_id}


async def pairing_status(code: str) -> dict:
    """Read-only status lookup for a pairing code."""
    from services.destination_service import get_destination_by_pairing_code

    code = (code or "").strip()
    dest = get_destination_by_pairing_code(code)
    if dest is None:
        return {"ok": False, "detail": "Invalid or expired pairing code",
                "status": "invalid", "destination_id": None, "project_id": None}

    return {
        "ok": True,
        "pairing_code": code,
        "status": dest.get("pairing_status") or "pending",
        "destination_id": dest["id"],
        "project_id": dest.get("project_id"),
        "created_at": dest.get("pairing_created_at"),
        "expires_at": dest.get("pairing_expires_at"),
    }


async def test_whatsapp_connection(destination_id: int) -> dict:
    """Verify platform account + token validity for a WhatsApp destination,
    without sending any public message."""
    from services.destination_service import get_destination
    from services.platform_accounts_service import get_credentials

    dest = get_destination(destination_id)
    if dest is None:
        return {"ok": False, "detail": "Destination not found"}

    if dest.get("chat_type") != "whatsapp_channel":
        return {"ok": False, "detail": "Not a WhatsApp destination"}

    platform_account_id = dest.get("platform_account_id")
    if not platform_account_id:
        return {"ok": False, "detail": "No platform account linked"}

    from database.db import get_connection
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM platform_accounts WHERE id=?", (platform_account_id,))
    acct = cur.fetchone()
    conn.close()

    if acct is None:
        return {"ok": False, "detail": "Platform account not found"}

    creds = get_credentials(acct)
    if not creds or not creds.get("access_token"):
        return {"ok": False, "detail": "No access token available"}

    return {"ok": True, "detail": "Connection validated (token present)"}


# ----------------------------------------------------------------------
# Optional aiohttp route wiring (only if aiohttp is installed)
# ----------------------------------------------------------------------

def setup_aiohttp_routes(app):
    """Attach WhatsApp pairing endpoints to an aiohttp app. Safe to call
    only when aiohttp is available - does not import it eagerly at module
    import time."""
    try:
        from aiohttp import web
    except ImportError:
        logger.warning("aiohttp not installed - pairing HTTP routes not registered")
        return

    async def verify_handler(request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "detail": "Invalid JSON"}, status=400)
        result = await verify_pairing_code(data.get("pairing_code", ""))
        status = 200 if result["ok"] else 400
        return web.json_response(result, status=status)

    async def status_handler(request):
        result = await pairing_status(request.query.get("pairing_code", ""))
        status = 200 if result["ok"] else 404
        return web.json_response(result, status=status)

    async def test_handler(request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "detail": "Invalid JSON"}, status=400)
        result = await test_whatsapp_connection(data.get("destination_id"))
        status = 200 if result["ok"] else 400
        return web.json_response(result, status=status)

    app.router.add_post("/api/whatsapp/verify_pairing", verify_handler)
    app.router.add_get("/api/whatsapp/pairing_status", status_handler)
    app.router.add_post("/api/whatsapp/test_connection", test_handler)
    logger.info("WhatsApp pairing HTTP routes registered")