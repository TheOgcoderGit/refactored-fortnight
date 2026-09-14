"""Integration tests for the AI support chatbot on Google AI Studio (Gemini).

The bot's support assistant used to talk to OpenRouter. It now goes through
``services/gemini_client``. These tests pin that behaviour by stubbing only
the HTTP boundary (``gemini_client.generate_content``) and exercising the
real service: rate limiting, session history, model fallback and the
graceful-degradation contract (never raise, never crash the user's chat).

No network access and no API key are required.
"""

import os
import time
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

from test_hardening import DatabaseCase  # noqa: E402

from services import gemini_client, support_ai_service as sai  # noqa: E402

USER_A = 501
USER_B = 502


def gemini_ok(text="Here is how you connect your account.", model="gemini-2.0-flash"):
    return gemini_client.GeminiResult(
        success=True, text=text, model=model,
        tokens_in=120, tokens_out=40, status_code=200,
        error=None, retryable=False, finish_reason="STOP",
    )


def gemini_fail(error="quota exceeded", retryable=True, status=429):
    return gemini_client.GeminiResult(
        success=False, text=None, model="gemini-2.0-flash",
        tokens_in=0, tokens_out=0, status_code=status,
        error=error, retryable=retryable, finish_reason=None,
    )


class SupportAITests(DatabaseCase, unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        # Sessions and usage counters live in module-level dicts.
        sai.clear_chat_history(USER_A)
        sai.clear_chat_history(USER_B)
        sai._support_ai_usage.pop(USER_A, None)
        sai._support_ai_usage.pop(USER_B, None)
        # _call_gemini short-circuits when no key is configured, so a key
        # must be present for the mocked transport to be reached at all.
        self.key_patch = patch.object(sai, "GEMINI_API_KEY", "test-gemini-key")
        self.key_patch.start()

    def tearDown(self):
        self.key_patch.stop()
        super().tearDown()

    # ------------------------------------------------------------------
    # happy path
    # ------------------------------------------------------------------

    async def test_answer_comes_from_gemini(self):
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_ok())) as mock_call:
            result = await sai.get_support_ai_response(USER_A, "How do I connect?")

        self.assertTrue(result.success)
        self.assertEqual(result.text, "Here is how you connect your account.")
        self.assertEqual(result.model_used, "gemini-2.0-flash")
        self.assertFalse(result.fallback_used)
        self.assertEqual(result.tokens_out, 40)
        mock_call.assert_awaited_once()

    async def test_system_prompt_is_passed_separately_not_as_a_message(self):
        """Gemini takes the system prompt in systemInstruction, and only
        accepts user/model roles in `contents`."""
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_ok())) as mock_call:
            await sai.get_support_ai_response(USER_A, "How do I connect?")

        kwargs = mock_call.await_args.kwargs
        self.assertIn("ChannelFlow", kwargs["system"])
        roles = {m["role"] for m in kwargs["messages"]}
        self.assertEqual(roles, {"user"}, "Gemini rejects a 'system' role in contents")
        self.assertEqual(kwargs["messages"][-1]["content"], "How do I connect?")

    async def test_provided_key_is_used(self):
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_ok())) as mock_call:
            await sai.get_support_ai_response(USER_A, "hello")

        self.assertEqual(mock_call.await_args.kwargs["api_key"], "test-gemini-key")

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------

    async def test_history_accumulates_across_turns(self):
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_ok("reply"))):
            await sai.get_support_ai_response(USER_A, "first question")
            await sai.get_support_ai_response(USER_A, "second question")

        history = sai.get_chat_history(USER_A)
        self.assertEqual([m.content for m in history],
                         ["first question", "reply", "second question", "reply"])
        self.assertEqual([m.role for m in history],
                         ["user", "assistant", "user", "assistant"])

    async def test_earlier_turns_are_sent_back_as_context(self):
        sent = []

        async def _capture(**kwargs):
            sent.append([m["content"] for m in kwargs["messages"]])
            return gemini_ok("reply")

        with patch.object(gemini_client, "generate_content", new=_capture):
            await sai.get_support_ai_response(USER_A, "first question")
            await sai.get_support_ai_response(USER_A, "second question")

        self.assertEqual(sent[0], ["first question"])
        self.assertEqual(sent[1], ["first question", "reply", "second question"])

    async def test_users_do_not_share_history(self):
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_ok("reply"))):
            await sai.get_support_ai_response(USER_A, "mine")
            await sai.get_support_ai_response(USER_B, "theirs")

        self.assertEqual([m.content for m in sai.get_chat_history(USER_A)],
                         ["mine", "reply"])
        self.assertEqual([m.content for m in sai.get_chat_history(USER_B)],
                         ["theirs", "reply"])

    # ------------------------------------------------------------------
    # degradation - the chatbot must never raise at the user
    # ------------------------------------------------------------------

    async def test_all_models_failing_returns_failure_not_exception(self):
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_fail())):
            result = await sai.get_support_ai_response(USER_A, "hello")

        self.assertFalse(result.success)
        self.assertIsNone(result.text)
        self.assertIn("quota exceeded", result.error)

    async def test_every_configured_model_is_tried_before_giving_up(self):
        tried = []

        async def _record(**kwargs):
            tried.append(kwargs["model"])
            return gemini_fail()

        with patch.object(gemini_client, "generate_content", new=_record):
            await sai.get_support_ai_response(USER_A, "hello")

        expected = [sai.SUPPORT_AI_MODEL] + sai.SUPPORT_AI_FALLBACK_MODELS
        self.assertEqual(tried, expected)

    async def test_fallback_success_is_flagged(self):
        attempted = []

        async def _flaky(**kwargs):
            attempted.append(kwargs["model"])
            if kwargs["model"] == sai.SUPPORT_AI_MODEL:
                return gemini_fail("primary down")
            return gemini_ok("from the fallback", model=kwargs["model"])

        with patch.object(gemini_client, "generate_content", new=_flaky):
            result = await sai.get_support_ai_response(USER_A, "hello")

        self.assertTrue(result.success)
        self.assertEqual(result.text, "from the fallback")
        self.assertTrue(result.fallback_used)

    async def test_transport_exception_is_contained(self):
        # Skip the real backoff sleeps so the suite stays fast.
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("asyncio.sleep", new=AsyncMock()):
            result = await sai.get_support_ai_response(USER_A, "hello")

        self.assertFalse(result.success)
        self.assertIn("boom", result.error)

    async def test_unconfigured_key_does_not_hit_the_network(self):
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(side_effect=AssertionError("must not be called"))), \
             patch.object(sai, "GEMINI_API_KEY", ""):
            result = await sai.get_support_ai_response(USER_A, "hello")

        self.assertFalse(result.success)

    # ------------------------------------------------------------------
    # input guards
    # ------------------------------------------------------------------

    async def test_oversized_message_rejected_before_any_call(self):
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(side_effect=AssertionError("must not be called"))):
            result = await sai.get_support_ai_response(
                USER_A, "x" * (sai.SUPPORT_AI_MAX_MESSAGE_LENGTH + 1))

        self.assertFalse(result.success)
        self.assertIn("too long", result.error.lower())

    async def test_daily_limit_is_enforced(self):
        sai._support_ai_usage[USER_A] = [time.time()] * sai.SUPPORT_AI_DAILY_LIMIT

        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(side_effect=AssertionError("must not be called"))):
            result = await sai.get_support_ai_response(USER_A, "hello")

        self.assertFalse(result.success)
        self.assertIn("limit", result.error.lower())

    async def test_successful_call_counts_towards_the_limit(self):
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_ok())):
            await sai.get_support_ai_response(USER_A, "hello")

        self.assertEqual(len(sai._support_ai_usage.get(USER_A, [])), 1)
        self.assertEqual(sai.get_usage_stats(USER_A)["requests_today"], 1)


if __name__ == "__main__":
    unittest.main()
