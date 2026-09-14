"""The /connect flow must always answer the phone number you send it.

Users reported sending a phone number and getting nothing back - no
"sending code", no OTP prompt, just silence. Three ways that happened:

1. start_connect() raised something that was not ConnectError - most
   often SessionEncryptionNotConfigured, because _key_bytes() checks
   SESSION_ENCRYPTION_KEY before anything else. The handler only caught
   ConnectError, so everything else escaped to the global error handler
   and the user saw a generic notice, or nothing.

2. Telegram never answered. client.connect() plus send_code_request()
   retries, so the user could sit at "Requesting..." indefinitely with
   no way to tell slow from broken. There is now a ceiling.

3. "+91 98765 43210" was rejected as invalid. It is the same number as
   "+919876543210" - people paste it with spaces.

The connect flow is also where a lot of the bot's Hindi lived, so this
checks the errors an English user sees are English.
"""

import os
import re
import unittest

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

import asyncio  # noqa: E402
from unittest import mock  # noqa: E402

from test_hardening import DatabaseCase  # noqa: E402

from bot import handlers_onboard  # noqa: E402
from core import user_sessions  # noqa: E402

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
ROMANISED = re.compile(
    r"\b(hai|karein|kiya|nahi|hua|aapka|dobara|pehle|galat|bhejein|"
    r"ho\s+saka|me\s+aaya)\b", re.I)


class _Msg:
    def __init__(self, log):
        self.log = log

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.log.append(str(text))
        return self

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.log.append(str(text))
        return self

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.log.append(str(text))
        return self


class ConnectFlowTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):

    async def _send_phone(self, phone, start_side_effect, patch_timeout=None):
        """Press the connect prompt, then send `phone`. Returns the replies."""
        log = []
        message = _Msg(log)

        async def _start(user_id, number):
            if start_side_effect is not None:
                await start_side_effect(number)

        with mock.patch.object(user_sessions, "start_connect",
                                        side_effect=_start):
            await handlers_onboard.initiate_phone_connect(message, 1, phone)
        return log

    async def test_sending_a_number_always_gets_a_reply(self):
        """The bug that was reported: number sent, nothing came back."""
        log = await self._send_phone("+919876543210", None)
        self.assertTrue(log, "sending a phone number produced silence")
        self.assertTrue(any("OTP" in m or "code" in m.lower() for m in log),
                        "no OTP prompt was shown after the number: %r" % log)

    async def test_number_with_spaces_is_accepted(self):
        """+91 98765 43210 is the same number as +919876543210."""
        seen = []

        async def _remember(user_id, number):
            seen.append(number)

        with mock.patch.object(user_sessions, "start_connect",
                                        side_effect=_remember):
            log = []
            await handlers_onboard.initiate_phone_connect(
                _Msg(log), 1, "+91 98765 43210")
        self.assertEqual(["+919876543210"], seen,
                         "the number was not normalised")

    async def test_non_connecterror_is_answered_not_raised(self):
        """SESSION_ENCRYPTION_KEY missing must not escape the handler."""

        async def _boom(number):
            raise RuntimeError("SESSION_ENCRYPTION_KEY is not set")

        log = await self._send_phone("+919876543210", _boom)
        self.assertTrue(log, "an unexpected error left the user with nothing")
        self.assertTrue(any("❌" in m for m in log),
                        "the failure was not explained: %r" % log)

    async def test_slow_telegram_is_answered_not_silent(self):
        """If Telegram never answers, say so instead of hanging."""

        async def _hang(number):
            await asyncio.sleep(3600)

        handlers_onboard.CONNECT_REQUEST_TIMEOUT = 0.01
        try:
            log = await self._send_phone("+919876543210", _hang)
        finally:
            handlers_onboard.CONNECT_REQUEST_TIMEOUT = 60
        self.assertTrue(log, "a stalled code request produced no message")
        self.assertTrue(any("didn't answer" in m for m in log),
                        "the timeout was not explained: %r" % log)

    async def test_connecterror_is_reported(self):

        async def _fail(number):
            raise user_sessions.ConnectError("Telegram has banned this number.")

        log = await self._send_phone("+919876543210", _fail)
        self.assertTrue(any("banned" in m for m in log),
                        "ConnectError was not surfaced: %r" % log)


class ConnectLanguageTests(unittest.TestCase):
    """Every error the connect flow can raise must be English."""

    def test_connect_errors_are_english(self):
        source = open(user_sessions.__file__, encoding="utf-8").read()
        bad = []
        for match in re.finditer(r'ConnectError\(\s*f?["\'](.+?)["\']', source):
            text = match.group(1)
            if DEVANAGARI.search(text) or ROMANISED.search(text):
                bad.append(text)
        self.assertEqual(
            [], bad,
            "Connect errors shown to an English user are in Hindi: %r" % bad)

    def test_otp_usage_hint_is_english(self):
        ok, hint = user_sessions.extract_otp_from_command("12345")
        self.assertFalse(ok)
        self.assertIsNone(DEVANAGARI.search(hint),
                          "OTP format hint is in Hindi: %r" % hint)
        self.assertIsNone(ROMANISED.search(hint),
                          "OTP format hint is romanised Hindi: %r" % hint)
