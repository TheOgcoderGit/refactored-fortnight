"""
ChannelFlow AI - AI Content Rewriting Service
================================================

Rewrites/transforms content via Google AI Studio (Gemini).

Design principles (Prompt 1 section 30-31, Prompt 2 section 14-17):
    * Provider + model configurable via env vars - never hardcoded
    * Free models preferred, paid never mandatory
    * Fallback chain: primary model -> secondary model -> original content
    * AI failure NEVER blocks forwarding unless user explicitly set it mandatory
    * URLs and required hashtags preserved unless explicitly configured otherwise
    * Full usage tracking for cost control
    * Per-user/project rate limits

Per-project settings live in project_ai_settings; global defaults in env.

Provider: Google AI Studio (Gemini). Configure GEMINI_API_KEY (and optionally
GEMINI_MODEL / GEMINI_FALLBACK_MODELS) in .env. The HTTP transport is shared
with the support chatbot - see services/gemini_client.py.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from config import GEMINI_API_KEY, GEMINI_FALLBACK_MODELS, GEMINI_MODEL
from database.db import get_connection
from services import gemini_client

logger = logging.getLogger(__name__)

# Fallback chain when the primary model fails. Configurable via
# GEMINI_FALLBACK_MODELS; these are the Google AI Studio defaults.
DEFAULT_FALLBACK_MODELS = list(GEMINI_FALLBACK_MODELS) or [
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
]

# Cost control limits
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 2
MAX_TOKENS_OUTPUT = 1024
MAX_INPUT_CHARS = 8000  # prevent sending excessively large prompts

# Rate limiting: max AI requests per user per day (0 = unlimited)
DAILY_USER_LIMIT = 500


@dataclass
class AISettings:
    """Per-project AI configuration."""

    enabled: bool = False
    model_override: Optional[str] = None
    tone: str = "original"          # original | casual | professional | promotional | funny
    length_mode: str = "keep"       # keep | shorten | expand
    preserve_urls: bool = True      # never rewrite URLs by default
    preserve_hashtags: bool = True  # keep required hashtags
    custom_prompt: str = ""
    language: Optional[str] = None  # translate target language if set
    add_cta: bool = False
    remove_spam: bool = False
    mandatory: bool = False         # if True, forward fails when AI fails


@dataclass
class AIResult:
    """Result of an AI transformation."""

    success: bool
    text: Optional[str] = None     # transformed text (or original on failure)
    model_used: Optional[str] = None
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    fallback_used: bool = False
    error: Optional[str] = None


def _column_exists(cur, table, column):
    cur.execute(f"PRAGMA table_info({table})")
    columns = [row["name"] for row in cur.fetchall()]
    return column in columns


# ==========================================
# PER-PROJECT SETTINGS
# ==========================================

def ensure_ai_settings(project_id: int) -> dict:
    """Get or create AI settings row for a project."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM project_ai_settings WHERE project_id=?", (project_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute(
            "INSERT INTO project_ai_settings(project_id) VALUES(?)",
            (project_id,),
        )
        conn.commit()
        cur.execute("SELECT * FROM project_ai_settings WHERE project_id=?", (project_id,))
        row = cur.fetchone()

    conn.close()

    return {
        "enabled": bool(row["enabled"]),
        "model_override": row["model_override"],
        "tone": row["tone"] or "original",
        "length_mode": row["length_mode"] or "keep",
        "preserve_urls": bool(row["preserve_urls"]),
        "preserve_hashtags": bool(row["preserve_hashtags"]),
        "custom_prompt": row["custom_prompt"] or "",
        "language": row["language"],
        "add_cta": bool(row["add_cta"]),
        "remove_spam": bool(row["remove_spam"]),
        "mandatory": bool(row["mandatory"]),
    }


def update_ai_settings(project_id: int, **fields):
    """Update specific AI setting fields."""

    allowed = {
        "enabled", "model_override", "tone", "length_mode",
        "preserve_urls", "preserve_hashtags", "custom_prompt",
        "language", "add_cta", "remove_spam", "mandatory",
    }

    filtered = {k: v for k, v in fields.items() if k in allowed}
    if not filtered:
        return

    conn = get_connection()
    cur = conn.cursor()

    ensure_ai_settings(project_id)

    sets = ", ".join(f"{k}=?" for k in filtered)
    values = list(filtered.values()) + [project_id]

    cur.execute(
        f"UPDATE project_ai_settings SET {sets} WHERE project_id=?",
        values,
    )

    conn.commit()
    conn.close()


# ==========================================
# PROMPT BUILDING
# ==========================================

def _build_prompt(text: str, settings: dict, platform: str = "generic") -> str:
    """Builds the system+user prompt from per-project settings."""

    parts = ["You are a social media content rewriter."]

    # Tone instruction
    tone_instructions = {
        "original": "",
        "casual": "Use a casual, friendly tone.",
        "professional": "Use a professional tone.",
        "promotional": "Use a promotional marketing tone that highlights value.",
        "funny": "Add light humor where appropriate.",
    }
    parts.append(tone_instructions.get(settings.get("tone"), ""))

    # Length instruction
    length_map = {
        "keep": "",
        "shorten": "Make it more concise while keeping key information.",
        "expand": "Slightly expand with relevant detail.",
    }
    parts.append(length_map.get(settings.get("length_mode"), ""))

    # Language translation
    if settings.get("language"):
        lang = settings["language"]
        parts.append(f"Translate to {lang}.")

    # Platform-specific formatting hints
    platform_hints = {
        "telegram": "Format for Telegram: use short paragraphs, minimal markdown.",
        "whatsapp_channel": "Format for WhatsApp Channel: use emojis sparingly, plain text friendly.",
        "threads": "Format for Threads: casual short-form, up to 3 hashtags.",
    }
    hint = platform_hints.get(platform, "")
    if hint:
        parts.append(hint)

    # CTA
    if settings.get("add_cta"):
        parts.append("End with a short call-to-action.")

    # Spam cleanup
    if settings.get("remove_spam"):
        parts.append("Remove spam-like language, excessive caps, and excessive exclamation marks.")

    # URL preservation
    if settings.get("preserve_urls"):
        parts.append("CRITICAL: Preserve all URLs exactly as they appear. Never modify, shorten, or remove links.")

    # Hashtag preservation
    if settings.get("preserve_hashtags"):
        parts.append("Preserve any hashtags present unless translating (then translate hashtag text).")

    # Custom prompt override
    if settings.get("custom_prompt"):
        parts.append(f"Additional instructions: {settings['custom_prompt']}")

    system = " ".join(p for p in parts if p)
    user = f"Rewrite this post:\n\n{text}"

    return system, user


def _extract_urls(text: str) -> list:
    import re
    return re.findall(r"https?://[^\s<>\"]+", text or "")


def _restore_urls(original_text: str, rewritten: str) -> str:
    """If URL count differs between original and rewritten, restore original URLs."""

    orig_urls = _extract_urls(original_text)
    new_urls = _extract_urls(rewritten)

    if not orig_urls:
        return rewritten

    if len(orig_urls) != len(new_urls):
        # AI mangled URLs - replace them back one-by-one in order of appearance
        result = rewritten
        for i, url in enumerate(orig_urls):
            if i < len(new_urls):
                result = result.replace(new_urls[i], url, 1)
        return result

    return rewritten


def _validate_output(original: str, rewritten: str, settings: dict) -> str:
    """Post-process AI output to enforce preservation rules."""
    if not rewritten:
        return original

    if settings.get("preserve_urls"):
        rewritten = _restore_urls(original, rewritten)

    return rewritten


# ==========================================
# USAGE TRACKING & RATE LIMITING
# ==========================================

def _log_usage(user_id, project_id, model, tokens_in, tokens_out, latency_ms, success, fallback_used, error=None):

    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO ai_usage(
                user_id, project_id, model, tokens_in, tokens_out,
                latency_ms, success, fallback_used, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, project_id, model,
                tokens_in, tokens_out, latency_ms,
                1 if success else 0, 1 if fallback_used else 0, error,
            ),
        )

        conn.commit()
        conn.close()

    except Exception:
        logger.exception("Failed to log AI usage")


def check_user_rate_limit(user_id) -> tuple:
    """Returns (allowed, reason). Checks daily AI request limit per user."""

    if DAILY_USER_LIMIT <= 0:
        return True, None

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM ai_usage WHERE user_id=? AND created_at >= date('now')",
        (user_id,),
    )
    today_count = cur.fetchone()[0]
    conn.close()

    if today_count >= DAILY_USER_LIMIT:
        return False, f"Daily AI request limit reached ({DAILY_USER_LIMIT}). Try again tomorrow."

    return True, None


# ==========================================
# CORE REWRITE FUNCTION
# ==========================================

async def rewrite_content(
    text: str,
    settings: dict,
    user_id=None,
    project_id=None,
    platform: str = "telegram",
) -> AIResult:
    """Main entry point. Attempts primary model, then fallback chain.
    Returns original content on total failure (never raises)."""

    start_time = time.time()

    if not gemini_client.is_configured(GEMINI_API_KEY):
        return AIResult(
            success=False, text=text,
            error="AI provider not configured (set GEMINI_API_KEY)",
        )

    if not text or not text.strip():
        return AIResult(success=False, text=text, error="Empty text")

    if len(text) > MAX_INPUT_CHARS:
        return AIResult(success=False, text=text, error=f"Text too long ({len(text)} chars, max {MAX_INPUT_CHARS})")

    # Rate limit
    if user_id:
        allowed, reason = check_user_rate_limit(user_id)
        if not allowed:
            logger.warning("AI rate limit hit for user %s", user_id)
            return AIResult(success=False, text=text, error=reason)

    # Build prompt
    system, user_message = _build_prompt(text, settings, platform)

    # Model chain: project override > configured default > fallback models
    models_to_try = []
    primary = settings.get("model_override") or GEMINI_MODEL
    models_to_try.append(primary)
    models_to_try.extend(DEFAULT_FALLBACK_MODELS)

    # Dedupe while preserving order
    seen = set()
    models_to_try = [m for m in models_to_try if not (m in seen or seen.add(m))]

    last_error = None

    for attempt, model in enumerate(models_to_try):

        is_fallback = attempt > 0

        for retry in range(MAX_RETRIES + 1):

            result = await gemini_client.generate_content(
                api_key=GEMINI_API_KEY,
                model=model,
                system=system,
                messages=[{"role": "user", "content": user_message}],
                temperature=0.7,
                max_output_tokens=MAX_TOKENS_OUTPUT,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            latency_ms = int((time.time() - start_time) * 1000)

            if result.success:
                # Validate + enforce preservation rules
                final_text = _validate_output(text, result.text, settings)

                _log_usage(
                    user_id=user_id, project_id=project_id, model=model,
                    tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out,
                    latency_ms=latency_ms, success=True, fallback_used=is_fallback,
                )

                return AIResult(
                    success=True,
                    text=final_text,
                    model_used=model,
                    tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out,
                    latency_ms=latency_ms,
                    fallback_used=is_fallback,
                )

            last_error = result.error or "Unknown AI error"

            # 429/5xx are worth another try; a malformed or blocked response
            # will fail identically, so move on to the next model immediately.
            if not result.retryable or retry >= MAX_RETRIES:
                break
            await asyncio.sleep(2 ** retry)

    # Total failure - return original
    latency_ms = int((time.time() - start_time) * 1000)
    _log_usage(
        user_id=user_id, project_id=project_id, model=models_to_try[0],
        tokens_in=0, tokens_out=0, latency_ms=latency_ms,
        success=False, fallback_used=False, error=last_error,
    )

    return AIResult(success=False, text=text, error=last_error)


# ==========================================
# ADMIN STATS
# ==========================================

def get_ai_stats_today():
    """Today's AI stats for admin dashboard."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            COUNT(*) as requests,
            SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as successful,
            SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) as failed,
            SUM(CASE WHEN fallback_used=1 THEN 1 ELSE 0 END) as fallbacks,
            AVG(latency_ms) as avg_latency
        FROM ai_usage
        WHERE created_at >= date('now')
    """)
    row = cur.fetchone()
    conn.close()

    return {
        "requests": row["requests"] or 0,
        "successful": row["successful"] or 0,
        "failed": row["failed"] or 0,
        "fallbacks": row["fallbacks"] or 0,
        "avg_latency_ms": round(row["avg_latency"] or 0),
    }