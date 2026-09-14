"""ChannelFlow AI - Google AI Studio (Gemini) client
===================================================

Single, shared HTTP client for Google AI Studio's Gemini API, used by both
the AI rewriter (services/ai_service.py) and the support chatbot
(services/support_ai_service.py).

Why a shared module
-------------------
Both services previously talked to OpenRouter with their own copy of the
request/response handling. Duplicating that for Gemini would mean two places
to fix when the provider contract shifts, so the transport lives here and
the two callers only differ in *prompting*.

Contract
--------
    POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
    header: x-goog-api-key: <GEMINI_API_KEY>
    body: {
        "systemInstruction": {"parts": [{"text": ...}]},
        "contents": [{"role": "user"|"model", "parts": [{"text": ...}]}, ...],
        "generationConfig": {"temperature": ..., "maxOutputTokens": ...}
    }
    response: {
        "candidates": [{"content": {"parts": [{"text": ...}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": N, "candidatesTokenCount": M}
    }

Failure policy (PRD §16.3 / §59): the caller decides what to do. This module
never raises for a provider-side problem - it returns a result object with
``success=False`` and a ``retryable`` flag so the caller can run its own
fallback chain and always fall back to the original content rather than
blocking a forward.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# HTTP statuses that are worth retrying (rate limit / transient upstream).
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


@dataclass
class GeminiResult:
    success: bool
    text: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    status_code: int = 0
    error: Optional[str] = None
    retryable: bool = False
    finish_reason: str = ""


def _normalise_role(role: str) -> str:
    """Gemini only understands ``user`` and ``model``.

    OpenAI-style payloads elsewhere in the codebase use ``assistant``; accept
    both so callers don't have to remember which provider they're on.
    """
    role = (role or "user").strip().lower()
    if role in ("assistant", "ai", "bot"):
        return "model"
    if role == "system":
        return "user"
    return role if role in ("user", "model") else "user"


def is_configured(api_key: Optional[str]) -> bool:
    return bool(api_key and api_key.strip())


async def generate_content(
    *,
    api_key: str,
    model: str,
    system: str = "",
    messages: List[dict],
    temperature: float = 0.7,
    max_output_tokens: int = 1024,
    timeout: float = 30.0,
) -> GeminiResult:
    """One Gemini ``generateContent`` call.

    ``messages`` is a list of ``{"role": "user"|"model"|"assistant",
    "content": str}`` dicts, oldest first. The system instruction is passed
    separately because Gemini keeps it out of the conversation turns.
    """
    if not is_configured(api_key):
        return GeminiResult(success=False, error="Gemini API key not configured", retryable=False)

    contents = []
    for entry in messages:
        text = (entry.get("content") or "").strip()
        if not text:
            continue
        contents.append({"role": _normalise_role(entry.get("role")), "parts": [{"text": text}]})

    if not contents:
        return GeminiResult(success=False, error="Empty prompt", retryable=False)

    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
        },
    }
    system_text = (system or "").strip()
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}

    url = f"{GEMINI_API_BASE}/models/{model}:generateContent"
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            response = await http.post(url, headers=headers, json=payload)
    except httpx.TimeoutException:
        return GeminiResult(success=False, model=model, error="Timeout", retryable=True)
    except Exception as exc:  # network/DNS/TLS - transient by nature
        logger.warning("Gemini request error (model=%s): %s", model, exc)
        return GeminiResult(success=False, model=model, error=str(exc)[:200], retryable=True)

    status = response.status_code

    if status >= 400:
        detail = ""
        try:
            body = response.json()
            detail = (body.get("error") or {}).get("message", "") or ""
        except Exception:
            detail = response.text[:200]
        logger.warning("Gemini HTTP %s (model=%s): %s", status, model, detail[:200])
        return GeminiResult(
            success=False,
            model=model,
            status_code=status,
            error=detail or f"HTTP {status}",
            retryable=status in RETRYABLE_STATUSES,
        )

    try:
        data = response.json()
    except Exception:
        return GeminiResult(success=False, model=model, status_code=status,
                            error="Malformed JSON response", retryable=False)

    candidates = data.get("candidates") or []
    if not candidates:
        # A blocked/absent candidate is a policy outcome, not a transient one -
        # retrying the same prompt would just burn quota.
        feedback = (data.get("promptFeedback") or {}).get("blockReason", "")
        return GeminiResult(
            success=False, model=model, status_code=status,
            error=f"No candidates returned{f' ({feedback})' if feedback else ''}",
            retryable=False,
        )

    candidate = candidates[0]
    parts = ((candidate.get("content") or {}).get("parts") or [])
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()

    finish_reason = candidate.get("finishReason") or ""
    if not text:
        return GeminiResult(
            success=False, model=model, status_code=status,
            error=f"Empty completion (finishReason={finish_reason or 'unknown'})",
            retryable=False,
            finish_reason=finish_reason,
        )

    usage = data.get("usageMetadata") or {}
    return GeminiResult(
        success=True,
        text=text,
        model=model,
        tokens_in=int(usage.get("promptTokenCount") or 0),
        tokens_out=int(usage.get("candidatesTokenCount") or 0),
        status_code=status,
        finish_reason=finish_reason,
    )
