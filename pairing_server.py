"""
ChannelFlow AI - WhatsApp Pairing HTTP Server (standalone)
===========================================================

Standalone aiohttp server for the WhatsApp pairing verification endpoint.
Run separately from the Telegram bot:

    python pairing_server.py

Listens on PAIRING_SERVER_HOST:PAIRING_SERVER_PORT (default 0.0.0.0:8081).

This is where the WhatsApp bot (separate service) POSTs pairing codes the
user submits on the WhatsApp side. The Telegram bot itself does NOT need
to run this server; the two are independent processes sharing the same
SQLite database via WAL mode.

Endpoints:
    POST /api/whatsapp/verify_pairing   {"pairing_code": "CFXXXXXXXX"}
    GET  /api/whatsapp/pairing_status   ?pairing_code=CFXXXXXXXX
    POST /api/whatsapp/test_connection  {"destination_id": 123}

SECURITY: no WhatsApp access tokens or account credentials are readable
via these endpoints. Pairing codes are single-use, project-scoped,
short-lived values - never account secrets.
"""

import logging
import os

from database.db import init_db

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("channelflow.pairing")

HOST = os.getenv("PAIRING_SERVER_HOST", "0.0.0.0")
PORT = int(os.getenv("PAIRING_SERVER_PORT", "8081"))


def main():
    try:
        from aiohttp import web
    except ImportError:
        logger.error("aiohttp is not installed. Run: pip install aiohttp")
        raise SystemExit(1)

    init_db()
    logger.info("Database initialized")

    from core.pairing_endpoint import setup_aiohttp_routes

    app = web.Application()
    setup_aiohttp_routes(app)

    logger.info("Pairing server listening on %s:%s", HOST, PORT)
    web.run_app(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()