"""Integration tests for the AI rewriting service on Google AI Studio.

``services/ai_service`` used to call OpenRouter over raw httpx. It now
shares ``services/gemini_client`` with the support chatbot. These tests
stub only that transport boundary and exercise the real rewrite path:
prompt construction, the model fallback chain, retry classification and
the "never block forwarding" contract (on total failure the ORIGINAL text
is returned rather than an exception).

No network access and no API key are required.
"""

import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

from test_hardening import DatabaseCase  # noqa: E402

from services import ai_service, gemini_client  # noqa: E402

USER_ID = 601
PROJECT_ID = 610

ORIGINAL = "Big sale today! Visit https://example.com/deal #offers"


def gemini_ok(text, model="gemini-2.0-flash"):
    return gemini_client.GeminiResult(
        success=True, text=text, model=model,
        tokens_in=80, tokens_out=30, status_code=200,
        error=None, retryable=False, finish_reason="STOP",
    )


def gemini_fail(error="upstream overloaded", retryable=True, status=429):
    return gemini_client.GeminiResult(
        success=False, text=None, model="gemini-2.0-flash",
        tokens_in=0, tokens_out=0, status_code=status,
        error=error, retryable=retryable, finish_reason=None,
    )


class RewriteTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        self.key_patch = patch.object(ai_service, "GEMINI_API_KEY", "test-gemini-key")
        self.key_patch.start()
        # Exponential backoff would stall the suite on the retry paths.
        self.sleep_patch = patch("asyncio.sleep", new=AsyncMock())
        self.sleep_patch.start()
        self.sql("DELETE FROM ai_usage")

    def tearDown(self):
        self.sleep_patch.stop()
        self.key_patch.stop()
        super().tearDown()

    # ------------------------------------------------------------------
    # happy path
    # ------------------------------------------------------------------

    async def test_rewrite_returns_model_output(self):
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_ok("Huge sale today!"))):
            result = await ai_service.rewrite_content(ORIGINAL, {}, user_id=USER_ID,
                                                      project_id=PROJECT_ID)

        self.assertTrue(result.success)
        self.assertEqual(result.text, "Huge sale today!")
        self.assertEqual(result.model_used, "gemini-2.0-flash")
        self.assertFalse(result.fallback_used)
        self.assertEqual(result.tokens_out, 30)

    async def test_gemini_request_shape(self):
        """Gemini takes the system prompt out of band and only accepts
        user/model turns."""
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_ok("ok"))) as mock_call:
            await ai_service.rewrite_content(ORIGINAL, {}, user_id=USER_ID)

        kwargs = mock_call.await_args.kwargs
        self.assertEqual(kwargs["api_key"], "test-gemini-key")
        self.assertEqual(kwargs["model"], ai_service.GEMINI_MODEL)
        self.assertTrue(kwargs["system"])
        self.assertEqual([m["role"] for m in kwargs["messages"]], ["user"])
        self.assertIn(ORIGINAL, kwargs["messages"][0]["content"])

    async def test_project_model_override_wins(self):
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_ok("ok"))) as mock_call:
            await ai_service.rewrite_content(
                ORIGINAL, {"model_override": "gemini-1.5-pro"}, user_id=USER_ID)

        self.assertEqual(mock_call.await_args.kwargs["model"], "gemini-1.5-pro")

    async def test_usage_is_logged(self):
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_ok("ok"))):
            await ai_service.rewrite_content(ORIGINAL, {}, user_id=USER_ID,
                                             project_id=PROJECT_ID)

        rows = self.sql("SELECT * FROM ai_usage")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], USER_ID)
        self.assertEqual(rows[0]["project_id"], PROJECT_ID)
        self.assertEqual(rows[0]["success"], 1)
        self.assertEqual(rows[0]["tokens_out"], 30)

    # ------------------------------------------------------------------
    # failure handling - forwarding must never be blocked by the AI
    # ------------------------------------------------------------------

    async def test_total_failure_returns_original_text(self):
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_fail())):
            result = await ai_service.rewrite_content(ORIGINAL, {}, user_id=USER_ID)

        self.assertFalse(result.success)
        self.assertEqual(result.text, ORIGINAL, "must fall back to the original post")
        self.assertIn("upstream overloaded", result.error)

    async def test_retryable_error_is_retried_then_moves_on(self):
        calls = []

        async def _count(**kwargs):
            calls.append(kwargs["model"])
            return gemini_fail()

        with patch.object(gemini_client, "generate_content", new=_count):
            await ai_service.rewrite_content(ORIGINAL, {}, user_id=USER_ID)

        # MAX_RETRIES+1 attempts per model, for every model in the chain.
        per_model = ai_service.MAX_RETRIES + 1
        expected_chain = [ai_service.GEMINI_MODEL] + ai_service.DEFAULT_FALLBACK_MODELS
        self.assertEqual(calls, [m for m in expected_chain for _ in range(per_model)])

    async def test_non_retryable_error_skips_the_retry_budget(self):
        calls = []

        async def _count(**kwargs):
            calls.append(kwargs["model"])
            return gemini_fail("bad api key", retryable=False, status=400)

        with patch.object(gemini_client, "generate_content", new=_count):
            await ai_service.rewrite_content(ORIGINAL, {}, user_id=USER_ID)

        expected_chain = [ai_service.GEMINI_MODEL] + ai_service.DEFAULT_FALLBACK_MODELS
        self.assertEqual(calls, expected_chain)

    async def test_fallback_success_is_flagged(self):
        attempted = []

        async def _flaky(**kwargs):
            attempted.append(kwargs["model"])
            if kwargs["model"] == ai_service.GEMINI_MODEL:
                return gemini_fail("primary down")
            return gemini_ok("from fallback", model=kwargs["model"])

        with patch.object(gemini_client, "generate_content", new=_flaky):
            result = await ai_service.rewrite_content(ORIGINAL, {}, user_id=USER_ID)

        self.assertTrue(result.success)
        self.assertEqual(result.text, "from fallback")
        self.assertTrue(result.fallback_used)

    # ------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------

    async def test_unconfigured_provider_short_circuits_without_network(self):
        with patch.object(ai_service, "GEMINI_API_KEY", ""), \
             patch.object(gemini_client, "generate_content",
                          new=AsyncMock(side_effect=AssertionError("must not be called"))):
            result = await ai_service.rewrite_content(ORIGINAL, {}, user_id=USER_ID)

        self.assertFalse(result.success)
        self.assertEqual(result.text, ORIGINAL)
        self.assertIn("GEMINI_API_KEY", result.error)

    async def test_empty_text_rejected(self):
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(side_effect=AssertionError("must not be called"))):
            result = await ai_service.rewrite_content("   ", {}, user_id=USER_ID)

        self.assertFalse(result.success)
        self.assertIn("Empty", result.error)

    async def test_oversized_text_rejected(self):
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(side_effect=AssertionError("must not be called"))):
            result = await ai_service.rewrite_content(
                "x" * (ai_service.MAX_INPUT_CHARS + 1), {}, user_id=USER_ID)

        self.assertFalse(result.success)
        self.assertIn("too long", result.error.lower())

    async def test_daily_rate_limit_blocks_the_call(self):
        insert = (
            "INSERT INTO ai_usage(user_id, project_id, model, tokens_in, tokens_out, "
            "latency_ms, success) VALUES(?, ?, 'm', 0, 0, 0, 1)"
        )
        # A small limit keeps the test fast; the counting logic is unchanged.
        with patch.object(ai_service, "DAILY_USER_LIMIT", 3):
            for _ in range(3):
                self.sql(insert, (USER_ID, PROJECT_ID))

            with patch.object(gemini_client, "generate_content",
                              new=AsyncMock(side_effect=AssertionError("must not be called"))):
                result = await ai_service.rewrite_content(ORIGINAL, {}, user_id=USER_ID)

        self.assertFalse(result.success)
        self.assertIn("limit", result.error.lower())

    async def test_rate_limit_is_per_user(self):
        insert = (
            "INSERT INTO ai_usage(user_id, project_id, model, tokens_in, tokens_out, "
            "latency_ms, success) VALUES(?, ?, 'm', 0, 0, 0, 1)"
        )
        with patch.object(ai_service, "DAILY_USER_LIMIT", 3):
            for _ in range(3):
                self.sql(insert, (USER_ID, PROJECT_ID))

            with patch.object(gemini_client, "generate_content",
                              new=AsyncMock(return_value=gemini_ok("ok for someone else"))):
                result = await ai_service.rewrite_content(ORIGINAL, {}, user_id=USER_ID + 1)

        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
