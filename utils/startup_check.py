"""Startup configuration check.

utils/preflight.py existed but nothing ever imported it, so a broken .env
was only discovered when a user stumbled onto the feature that needed it -
an unset OWNER_ID silently locked the owner console, and a missing
GEMINI_API_KEY only surfaced as "AI unavailable" deep in a flow.

This runs once at boot and logs what is missing, in plain language, without
ever printing a secret value.
"""

import logging

logger = logging.getLogger(__name__)


def run_startup_checks() -> list:
    """Returns a list of warnings; empty means everything is configured."""

    from config import (
        ADMIN_IDS, API_HASH, API_ID, BOT_TOKEN, GEMINI_API_KEY, OWNER_ID,
        OWNER_PASSWORD_HASH, UPI_ID,
    )
    from services import gemini_client

    warnings = []

    if not BOT_TOKEN:
        warnings.append("BOT_TOKEN is not set - the bot cannot start.")
    if not API_ID or not API_HASH:
        warnings.append("API_ID / API_HASH are not set - Telegram login will fail.")

    if OWNER_ID is None:
        if ADMIN_IDS:
            warnings.append(
                "OWNER_ID is not set. The owner console is falling back to "
                "ADMIN_IDS, which also makes every administrator an owner. "
                "Set OWNER_ID=<your telegram id> in .env.")
        else:
            warnings.append(
                "OWNER_ID is not set and ADMIN_IDS is empty - nobody can "
                "reach the owner console. Set OWNER_ID=<your telegram id>.")
    elif not OWNER_PASSWORD_HASH:
        warnings.append(
            "OWNER_ID is set but OWNER_PASSWORD_HASH is empty - the owner "
            "console will reject the credential step.")

    if not gemini_client.is_configured(GEMINI_API_KEY):
        warnings.append(
            "GEMINI_API_KEY is not set - AI rewriting and the AI support "
            "assistant will fall back to the original/unavailable. Get a key "
            "at https://aistudio.google.com/apikey")

    if not UPI_ID:
        warnings.append("UPI_ID is not set - UPI payments are unavailable.")

    return warnings


def log_startup_checks():
    try:
        warnings = run_startup_checks()
    except Exception as exc:        # never block startup on the check itself
        logger.warning("Startup configuration check failed: %s", exc)
        return

    if not warnings:
        logger.info("Startup configuration check passed.")
        return

    for message in warnings:
        logger.warning("CONFIG: %s", message)
    logger.warning(
        "Startup configuration check found %d issue(s) - the bot will run, "
        "but the features above will not work.", len(warnings))
