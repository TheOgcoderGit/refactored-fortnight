"""Regressions for the owner/admin split, support-chat language and the
chat-resolution error taxonomy.

Each test here pins a bug a real user hit:
  * an administrator reaching the owner console because "owner" was
    defined as "anyone in ADMIN_IDS";
  * an OWNER_ID that parsed to None because of a space or inline comment,
    locking the owner out with no error;
  * the support assistant answering from canned Hinglish paragraphs because
    keyword matching ran before Gemini;
  * every chat-resolution failure collapsing into one unhelpful string.
"""

import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

from test_hardening import DatabaseCase  # noqa: E402

from services import audit_service, gemini_client  # noqa: E402
from services import support_ai_service as sai  # noqa: E402
from services import i18n_service  # noqa: E402

USER_ID = 701
OTHER_ID = 702


def gemini_ok(text="Here is the answer.", model="gemini-2.0-flash"):
    return gemini_client.GeminiResult(
        success=True, text=text, model=model, tokens_in=10, tokens_out=5,
        status_code=200, error=None, retryable=False, finish_reason="STOP",
    )


def gemini_fail(error="quota exceeded"):
    return gemini_client.GeminiResult(
        success=False, text=None, model="gemini-2.0-flash", tokens_in=0,
        tokens_out=0, status_code=429, error=error, retryable=True,
        finish_reason=None,
    )


# ==========================================================================
# owner / admin
# ==========================================================================


class OwnerIdentityTests(DatabaseCase, unittest.TestCase):

    def setUp(self):
        super().setUp()
        audit_service.invalidate_cache() if hasattr(audit_service, "invalidate_cache") else None

    def test_owner_id_wins_over_admin_ids(self):
        with patch.object(audit_service, "ADMIN_IDS", {OTHER_ID}), \
             patch("config.OWNER_ID", USER_ID):
            self.assertTrue(audit_service.is_owner(USER_ID))
            self.assertFalse(audit_service.is_owner(OTHER_ID))

    def test_admin_is_not_owner(self):
        """An administrator must NOT inherit the owner-only console."""
        # A real admin row, so is_admin() answers on its own merits rather
        # than because the id happens to sit in ADMIN_IDS.
        self.sql("INSERT INTO users(telegram_id) VALUES(?)", (OTHER_ID,))
        self.sql("INSERT INTO admins(telegram_id, is_active) VALUES(?, 1)", (OTHER_ID,))

        with patch.object(audit_service, "ADMIN_IDS", set()), \
             patch("config.OWNER_ID", USER_ID):
            self.assertTrue(audit_service.is_admin(OTHER_ID))   # still an admin
            self.assertFalse(audit_service.is_owner(OTHER_ID))  # but not owner

    def test_admin_ids_is_only_a_fallback_when_owner_id_is_unset(self):
        with patch.object(audit_service, "ADMIN_IDS", {OTHER_ID}), \
             patch("config.OWNER_ID", None):
            self.assertTrue(audit_service.is_owner(OTHER_ID))

    def test_owner_gets_every_permission_admin_does_not(self):
        with patch.object(audit_service, "ADMIN_IDS", {OTHER_ID}), \
             patch("config.OWNER_ID", USER_ID):
            self.assertEqual(audit_service.get_permissions(USER_ID),
                             set(audit_service.PERMISSIONS))
            self.assertNotEqual(audit_service.get_permissions(OTHER_ID),
                                set(audit_service.PERMISSIONS))


class OwnerIdParsingTests(unittest.TestCase):
    """.env values are hand-edited; whitespace and inline comments are normal."""

    def _parse(self, raw):
        import importlib
        import config
        with patch.dict(os.environ, {"OWNER_ID": raw}):
            importlib.reload(config)
            return config.OWNER_ID

    def test_plain_number(self):
        self.assertEqual(self._parse("123456"), 123456)

    def test_surrounding_whitespace(self):
        self.assertEqual(self._parse("  123456  "), 123456)

    def test_inline_comment(self):
        self.assertEqual(self._parse("123456 # my account"), 123456)

    def test_quoted_with_comment(self):
        self.assertEqual(self._parse('"123456"  # my account'), 123456)

    def test_empty_is_none_not_zero(self):
        self.assertIsNone(self._parse(""))

    def test_garbage_is_none(self):
        self.assertIsNone(self._parse("my-account"))


# ==========================================================================
# support assistant
# ==========================================================================


class SupportLanguageTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        sai.clear_chat_history(USER_ID)
        sai._support_ai_usage.pop(USER_ID, None)
        i18n_service.invalidate_cache(USER_ID)
        i18n_service.set_user_language(USER_ID, "en")
        self.key_patch = patch.object(sai, "GEMINI_API_KEY", "test-gemini-key")
        self.key_patch.start()

    def tearDown(self):
        self.key_patch.stop()
        super().tearDown()

    # -- Gemini must be tried first ----------------------------------------

    async def test_gemini_is_called_even_for_keyword_questions(self):
        """'source' used to short-circuit to a canned paragraph, so the
        model was never consulted and every question got the same answer."""
        from bot import handlers_admin
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_ok("A tailored answer."))):
            answer = handlers_admin._canned_support_answer(USER_ID, "how do I add a source?")
            # _canned_support_answer is only reached when Gemini failed, so
            # the real assertion is on the full path below.
            self.assertIn("Sources", answer)

            handlers_admin.WAITING_AI_SUPPORT[USER_ID] = True
            message = _FakeMessage()
            await handlers_admin.handle_text(message, USER_ID,
                                             "how do I add a source?", None)

        self.assertIn("A tailored answer.", message.sent)
        self.assertNotIn("Sources", message.sent)

    async def test_different_questions_get_different_answers(self):
        from bot import handlers_admin
        answers = []
        for question in ("how do I add a source?", "what does the Pro plan cost?"):
            handlers_admin.WAITING_AI_SUPPORT[USER_ID] = True
            message = _FakeMessage()
            with patch.object(gemini_client, "generate_content",
                              new=AsyncMock(return_value=gemini_ok(f"Answer to: {question}"))):
                await handlers_admin.handle_text(message, USER_ID, question, None)
            answers.append(message.sent)
        self.assertNotEqual(*answers)

    # -- fallback language --------------------------------------------------

    async def test_english_user_gets_english_fallback(self):
        from bot import handlers_admin
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_fail())):
            answer = handlers_admin._canned_support_answer(USER_ID, "how do I add a source?")
        self.assertFalse(_has_devanagari(answer),
                         f"English user got a Hinglish reply: {answer!r}")

    async def test_hindi_user_gets_hindi_fallback(self):
        from bot import handlers_admin
        i18n_service.set_user_language(USER_ID, "hi")
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_fail())):
            answer = handlers_admin._canned_support_answer(USER_ID, "source kaise add karein?")
        self.assertTrue(_has_devanagari(answer),
                        f"Hindi user got an English reply: {answer!r}")

    async def test_fallback_is_used_when_gemini_is_unavailable(self):
        from bot import handlers_admin
        handlers_admin.WAITING_AI_SUPPORT[USER_ID] = True
        message = _FakeMessage()
        with patch.object(gemini_client, "generate_content",
                          new=AsyncMock(return_value=gemini_fail())):
            await handlers_admin.handle_text(message, USER_ID, "how do I add a source?", None)
        self.assertTrue(message.sent)
        self.assertFalse(_has_devanagari(message.sent))


# ==========================================================================
# chat resolution errors
# ==========================================================================


class ChatErrorTaxonomyTests(unittest.TestCase):

    def test_unregistered_auth_key_is_a_session_problem(self):
        """This exact Telethon message is a dead session, not a bad handle."""
        from core import telegram_utils as tg
        exc = RuntimeError("The key is not registered in the system "
                           "(caused by ResolveUsernameRequest)")
        self.assertEqual(_classify(exc), "session_expired")

    def test_cannot_find_entity_is_not_found(self):
        from core import telegram_utils as tg
        exc = RuntimeError("Cannot find any entity corresponding to \"@Hi\"")
        self.assertEqual(_classify(exc), "not_found")

    def test_missing_session_is_not_connected(self):
        from core import telegram_utils as tg
        self.assertEqual(_classify(tg.AccountNotConnectedError()), "not_connected")

    def test_every_kind_has_copy_and_recovery_buttons(self):
        from bot import error_actions
        from core import telegram_utils as tg
        for kind in tg.ERROR_COPY:
            with self.subTest(kind=kind):
                exc = tg.ChatResolveError()
                exc.kind = kind
                text = error_actions.failure_text(exc)
                markup = error_actions.failure_keyboard(exc)
                self.assertTrue(text.strip())
                self.assertTrue(markup.inline_keyboard)
                self.assertLessEqual(len(markup.inline_keyboard[0]), 4)

    def test_session_problems_offer_reconnect(self):
        from bot import error_actions
        from core import telegram_utils as tg
        markup = error_actions.failure_keyboard(tg.SessionExpiredError())
        labels = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("nav:connect", labels)


# ==========================================================================
# helpers
# ==========================================================================


class _FakeMessage:
    def __init__(self):
        self.sent = ""

    async def reply_text(self, text, **kwargs):
        self.sent = text
        return _FakeStatus(self)


class _FakeStatus:
    def __init__(self, owner):
        self.owner = owner

    async def edit_text(self, text, **kwargs):
        self.owner.sent = text


def _has_devanagari(text: str) -> bool:
    return any("ऀ" <= ch <= "ॿ" for ch in text or "")


def _classify(exc):
    from bot import error_actions
    return error_actions.classify(exc)


if __name__ == "__main__":
    unittest.main()
