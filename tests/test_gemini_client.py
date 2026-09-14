"""Offline tests for the Google AI Studio (Gemini) transport.

No network and no API key: httpx.AsyncClient is patched with an in-process
transport so we assert on the exact request shape Gemini expects and on how
the client classifies provider outcomes (success / retryable / fatal).
"""

import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from services import gemini_client


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _install(monkey_response, capture):
    """Patch httpx.AsyncClient so the Gemini call resolves locally."""

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            # gemini_client passes timeout=; the fake ignores it.
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            capture["url"] = url
            capture["headers"] = headers
            capture["body"] = json
            if isinstance(monkey_response, Exception):
                raise monkey_response
            return monkey_response

    return patch.object(httpx, "AsyncClient", _FakeClient)


class GeminiClientTests(unittest.IsolatedAsyncioTestCase):

    def run_async(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    async def _call(self, **overrides):
        kwargs = dict(
            api_key="test-key",
            model="gemini-2.0-flash",
            system="Be brief.",
            messages=[{"role": "user", "content": "hello"}],
        )
        kwargs.update(overrides)
        return await gemini_client.generate_content(**kwargs)

    async def test_no_key_short_circuits_without_http(self):
        result = await self._call(api_key="")
        self.assertFalse(result.success)
        self.assertIn("not configured", result.error or "")
        self.assertFalse(result.retryable)

    async def test_empty_prompt_short_circuits(self):
        result = await self._call(messages=[{"role": "user", "content": "   "}])
        self.assertFalse(result.success)
        self.assertIn("Empty prompt", result.error or "")

    async def test_request_shape(self):
        captured = {}
        ok = _FakeResponse(200, {
            "candidates": [{"content": {"parts": [{"text": "Hi there"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 4},
        })
        with _install(ok, captured):
            result = await self._call()

        self.assertTrue(result.success)
        self.assertEqual(result.text, "Hi there")
        self.assertEqual(result.tokens_in, 11)
        self.assertEqual(result.tokens_out, 4)
        self.assertEqual(
            captured["url"],
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
        )
        self.assertEqual(captured["headers"]["x-goog-api-key"], "test-key")
        self.assertEqual(captured["body"]["contents"],
                         [{"role": "user", "parts": [{"text": "hello"}]}])
        self.assertEqual(captured["body"]["systemInstruction"],
                         {"parts": [{"text": "Be brief."}]})
        self.assertIn("generationConfig", captured["body"])

    async def test_assistant_role_is_translated_to_model(self):
        captured = {}
        ok = _FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
        with _install(ok, captured):
            await self._call(messages=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "help"},
            ])
        roles = [c["role"] for c in captured["body"]["contents"]]
        self.assertEqual(roles, ["user", "model", "user"])

    async def test_system_instruction_omitted_when_blank(self):
        captured = {}
        ok = _FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
        with _install(ok, captured):
            await self._call(system="   ")
        self.assertNotIn("systemInstruction", captured["body"])

    async def test_rate_limit_is_retryable(self):
        captured = {}
        resp = _FakeResponse(429, {"error": {"code": 429, "message": "Quota exceeded"}})
        with _install(resp, captured):
            result = await self._call()
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)
        self.assertIn("Quota exceeded", result.error or "")

    async def test_bad_request_is_not_retryable(self):
        captured = {}
        resp = _FakeResponse(400, {"error": {"code": 400, "message": "API key not valid"}})
        with _install(resp, captured):
            result = await self._call()
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertIn("API key not valid", result.error or "")

    async def test_timeout_is_retryable(self):
        captured = {}
        with _install(httpx.TimeoutException("timed out"), captured):
            result = await self._call()
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)
        self.assertEqual(result.error, "Timeout")

    async def test_network_error_is_retryable(self):
        captured = {}
        with _install(httpx.ConnectError("dns boom"), captured):
            result = await self._call()
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)

    async def test_blocked_prompt_is_not_retryable(self):
        captured = {}
        resp = _FakeResponse(200, {"promptFeedback": {"blockReason": "SAFETY"}})
        with _install(resp, captured):
            result = await self._call()
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertIn("SAFETY", result.error or "")

    async def test_multi_part_candidate_is_concatenated(self):
        captured = {}
        resp = _FakeResponse(200, {"candidates": [
            {"content": {"parts": [{"text": "one "}, {"text": "two"}]}}
        ]})
        with _install(resp, captured):
            result = await self._call()
        self.assertEqual(result.text, "one two")

    async def test_malformed_json_is_not_retryable(self):
        captured = {}
        resp = _FakeResponse(200, None, text="<html>not json</html>")
        with _install(resp, captured):
            result = await self._call()
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)


class GeminiRoleNormalisationTests(unittest.TestCase):
    def test_roles(self):
        self.assertEqual(gemini_client._normalise_role("user"), "user")
        self.assertEqual(gemini_client._normalise_role("model"), "model")
        self.assertEqual(gemini_client._normalise_role("assistant"), "model")
        self.assertEqual(gemini_client._normalise_role("bot"), "model")
        self.assertEqual(gemini_client._normalise_role("system"), "user")
        self.assertEqual(gemini_client._normalise_role(""), "user")
        self.assertEqual(gemini_client._normalise_role(None), "user")

    def test_is_configured(self):
        self.assertFalse(gemini_client.is_configured(None))
        self.assertFalse(gemini_client.is_configured("   "))
        self.assertTrue(gemini_client.is_configured("abc"))


if __name__ == "__main__":
    unittest.main()
