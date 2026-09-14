"""/status - the diagnostics command.

Forwarding failed silently: a source the account had been removed from, a
target it could no longer post to, an expired session. The only symptom
was that messages stopped arriving, and there was nothing a user could
run to find out why. /status walks the same chats the forwarder does and
names the ones that are broken.

These tests pin the report's shape: it must say which chat is broken, not
just that something is, and it must offer a way out.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

from test_hardening import DatabaseCase  # noqa: E402

from bot import handlers_status  # noqa: E402
from core import telegram_utils, user_sessions  # noqa: E402
from services import i18n_service  # noqa: E402


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.first_name = "Tester"


class _FakeMessage:
    def __init__(self, log):
        self.log = log

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("reply", text, reply_markup))
        return self

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("edit", text, reply_markup))
        return self


class _FakeUpdate:
    def __init__(self, log, uid):
        self.effective_user = _FakeUser(uid)
        self.message = _FakeMessage(log)


class StatusCommandTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):
    """DatabaseCase seeds user 1 with projects 10/11 and destinations
    100/101, and user 2 with project 20 and destination 200."""

    def setUp(self):
        super().setUp()
        i18n_service.invalidate_cache()

    async def _run(self, user_id, connected=True, get_chat=None):
        """Runs /status and returns the final report text."""
        log = []
        update = _FakeUpdate(log, user_id)

        async def _ok(reference, for_destination=False, user_id=None):
            return object()

        with mock.patch.object(user_sessions, "is_connected",
                               return_value=connected), \
             mock.patch.object(telegram_utils, "get_chat",
                               side_effect=get_chat or _ok):
            await handlers_status.status_cmd(update, None)

        edits = [entry for entry in log if entry[0] == "edit"]
        self.assertTrue(edits, f"no report was sent: {log}")
        return edits[-1][1], edits[-1][2]

    # ------------------------------------------------------------------

    async def test_no_projects_says_so(self):
        self.sql("INSERT INTO users(telegram_id) VALUES(3)")
        text, _ = await self._run(3)
        self.assertIn("no projects yet", text)

    async def test_not_connected_offers_reconnect(self):
        text, markup = await self._run(1, connected=False)
        self.assertIn("not connected", text)
        callbacks = [b.callback_data for row in markup.inline_keyboard
                     for b in row]
        self.assertIn("nav:connect", callbacks)

    async def test_not_connected_does_not_probe_every_chat(self):
        """Every probe would fail for the same reason; don't spam Telegram."""
        calls = []

        async def _count(reference, for_destination=False, user_id=None):
            calls.append(reference)
            return object()

        await self._run(1, connected=False, get_chat=_count)
        self.assertEqual([], calls)

    async def test_report_is_editable_and_offers_navigation(self):
        _, markup = await self._run(1)
        callbacks = [b.callback_data for row in markup.inline_keyboard
                     for b in row]
        self.assertIn("nav:projects", callbacks)
        self.assertIn("nav:home", callbacks)

    async def test_healthy_chats_are_reported_ok(self):
        text, _ = await self._run(1)
        self.assertIn("-10001", text)
        self.assertIn("*Targets* (2)", text)

    async def test_broken_chat_is_named_with_its_reason(self):
        async def _fail(reference, for_destination=False, user_id=None):
            if reference == "-10002":
                raise telegram_utils.ChatNoAccessError("kicked")
            return object()

        text, markup = await self._run(1, get_chat=_fail)
        # The report has to name the chat, not just say something is wrong.
        self.assertIn("-10002", text)
        self.assertIn("member", text)
        # ...and offer the fix.
        callbacks = [b.callback_data for row in markup.inline_keyboard
                     for b in row]
        self.assertIn("nav:connect", callbacks)

    async def test_disabled_chat_is_not_probed(self):
        self.sql("UPDATE destinations SET enabled=0 WHERE id=101")
        calls = []

        async def _count(reference, for_destination=False, user_id=None):
            calls.append(reference)
            return object()

        text, _ = await self._run(1, get_chat=_count)
        self.assertIn("disabled", text)
        self.assertNotIn("-10002", calls)

    async def test_sources_are_checked_too(self):
        self.sql("INSERT INTO sources(id,project_id,chat_id,title) "
                 "VALUES(300,10,'-10099','Deals')")
        text, _ = await self._run(1)
        self.assertIn("*Sources* (1)", text)
        self.assertIn("Deals", text)

    async def test_one_users_status_does_not_show_anothers_projects(self):
        text, _ = await self._run(2)
        self.assertNotIn("-10001", text)
        self.assertIn("-20001", text)

    async def test_channel_title_with_markdown_does_not_break_the_report(self):
        """Titles are user text; unescaped they raise and the reply dies."""
        self.sql("INSERT INTO sources(id,project_id,chat_id,title) "
                 "VALUES(301,10,'-10098','Hot *Deals* _now_')")
        text, _ = await self._run(1)
        self.assertIn("Hot", text)
        # The asterisks survive, escaped.
        self.assertIn(r"\*Deals\*", text)
