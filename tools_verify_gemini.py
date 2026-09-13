#!/usr/bin/env python
"""Live smoke test for the Google AI Studio (Gemini) migration.

The offline test suite (tests/test_gemini_client.py, test_support_ai.py,
test_ai_rewrite_gemini.py) proves the request shape and the fallback logic
with a mocked transport. This script proves the *real* thing: that your
key works, which models it can reach, and that both AI callers - the
content rewriter and the support chatbot - get a real answer back.

Usage:
    # put your key in .env first:
    #   GEMINI_API_KEY=AIza...
    venv/bin/python tools_verify_gemini.py

It makes three real API calls. It never prints the key.
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _mask(key: str) -> str:
    if not key:
        return "(empty)"
    return f"{key[:4]}...{key[-4:]} (len={len(key)})"


async def main() -> int:
    from config import (
        GEMINI_API_KEY, GEMINI_MODEL, GEMINI_FALLBACK_MODELS, SUPPORT_AI_MODEL,
    )
    from services import gemini_client

    print("=" * 62)
    print("ChannelFlow - Google AI Studio (Gemini) verification")
    print("=" * 62)
    print(f"GEMINI_API_KEY        : {_mask(GEMINI_API_KEY)}")
    print(f"Rewriter model        : {GEMINI_MODEL}")
    print(f"Support model         : {SUPPORT_AI_MODEL}")
    print(f"Fallback models       : {', '.join(GEMINI_FALLBACK_MODELS) or '(none)'}")
    print()

    if not gemini_client.is_configured(GEMINI_API_KEY):
        print("FAIL: no API key configured.")
        print("  Add GEMINI_API_KEY=<your key> to .env and re-run.")
        print("  Get one at https://aistudio.google.com/apikey")
        return 1

    failures = 0

    # ------------------------------------------------------------------
    # 1. raw transport
    # ------------------------------------------------------------------
    print("[1/3] Raw transport: generate_content()")
    started = time.time()
    result = await gemini_client.generate_content(
        api_key=GEMINI_API_KEY,
        model=GEMINI_MODEL,
        system="Reply with a single short sentence.",
        messages=[{"role": "user", "content": "Say hello in exactly five words."}],
        temperature=0.3,
        max_output_tokens=64,
    )
    elapsed = int((time.time() - started) * 1000)
    if result.success:
        print(f"      OK  model={result.model} latency={elapsed}ms "
              f"tokens_in={result.tokens_in} tokens_out={result.tokens_out}")
        print(f"      reply: {result.text!r}")
    else:
        failures += 1
        print(f"      FAIL status={result.status_code} retryable={result.retryable}")
        print(f"      error: {result.error}")

    # ------------------------------------------------------------------
    # 2. every model in the fallback chain
    # ------------------------------------------------------------------
    print()
    print("[2/3] Model availability (primary + fallbacks)")
    for model in [GEMINI_MODEL, *GEMINI_FALLBACK_MODELS, SUPPORT_AI_MODEL]:
        probe = await gemini_client.generate_content(
            api_key=GEMINI_API_KEY,
            model=model,
            system="Reply with one word.",
            messages=[{"role": "user", "content": "ping"}],
            max_output_tokens=16,
        )
        if probe.success:
            print(f"      OK    {model:<28} -> {probe.text.strip()[:40]!r}")
        else:
            failures += 1
            print(f"      FAIL  {model:<28} -> {probe.error}")

    # ------------------------------------------------------------------
    # 3. both real callers
    # ------------------------------------------------------------------
    print()
    print("[3/3] Real service paths")

    from services import ai_service
    rewrite = await ai_service.rewrite_content(
        "FLASH SALE!! grab it now -> https://example.com/deal #deals",
        {"tone": "professional", "length_mode": "keep"},
        user_id=None,
        project_id=None,
    )
    if rewrite.success:
        print(f"      OK    ai_service.rewrite_content   "
              f"model={rewrite.model_used} fallback={rewrite.fallback_used}")
        print(f"      rewritten: {rewrite.text!r}")
    else:
        failures += 1
        print(f"      FAIL  ai_service.rewrite_content   -> {rewrite.error}")

    from services import support_ai_service
    support = await support_ai_service.get_support_ai_response(
        0, "How do I connect my Telegram account?"
    )
    if support.success:
        print(f"      OK    support_ai_service           "
              f"model={support.model_used} fallback={support.fallback_used}")
        print(f"      answer: {support.text[:120]!r}")
    else:
        failures += 1
        print(f"      FAIL  support_ai_service           -> {support.error}")

    print()
    print("=" * 62)
    if failures:
        print(f"RESULT: {failures} check(s) FAILED - see above.")
        return 1
    print("RESULT: all Gemini checks passed. Migration is live.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
