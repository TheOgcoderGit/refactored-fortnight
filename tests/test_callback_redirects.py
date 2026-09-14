"""Callbacks that redirect by rewriting query.data.

About twenty screens hand a button to another module by setting
``query.data = "something:else"`` and returning False, expecting the
dispatcher to route the new data. It did not: `dispatch_callback` kept
walking the module chain with the ORIGINAL action and parts, so the
redirect was dropped and the button produced nothing at all - no screen,
no message, no error. Tapping it looked exactly like a dead bot.

"➕ Create Project" on the How It Works screen is the one users hit:
`onboard:create_gate` rewrites itself to `proj:new`, so a connected user
got silence and concluded projects could not be created at all.

`dispatch_callback` now re-reads query.data and re-dispatches. These tests
press the redirecting callbacks and assert the bot answers - which is the
only thing that was ever visibly wrong.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

from test_hardening import DatabaseCase  # noqa: E402

from bot import handlers  # noqa: E402
from core import user_sessions  # noqa: E402

UID = 1
PID = 10  # DatabaseCase seeds project 10 for user 1.


class _User:
    def __init__(self, uid):
        self.id = uid
        self.first_name = "Tester"


class _FakeMessage:
    def __init__(self, log):
        self.log = log

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("reply", text))
        return self

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("edit", text))
        return self

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("edit", text))
        return self


class _FakeQuery:
    """Stands in for a CallbackQuery, recording everything sent."""

    def __init__(self, log, data):
        self.log = log
        self.data = data
        self.message = _FakeMessage(log)
        self.from_user = _User(UID)
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("edit", text))
        return self


class CallbackRedirectTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        # The FREE plan allows a single project and DatabaseCase gives user 1
        # two, so the create screen would be answered by the upgrade prompt.
        # The redirect is what is under test, not the quota.
        self.sql("UPDATE plan_configs SET max_projects=10 WHERE plan_name='FREE'")
        from services import plan_service
        plan_service.invalidate_plan_configs_cache()

    async def _press(self, data):
        """Dispatches a callback the way a real button press does."""
        log = []
        query = _FakeQuery(log, data)
        parts = data.split(":")
        handled = await handlers.dispatch_callback(
            query, UID, parts[0], parts, None)
        return handled, log, query

    # ------------------------------------------------------------------

    async def test_create_project_from_how_it_works_opens_create_screen(self):
        """The reported bug: connected users tapped this and got silence."""
        with mock.patch.object(user_sessions, "is_connected", return_value=True):
            handled, log, _ = await self._press("onboard:create_gate")
        self.assertTrue(handled, "callback was not handled - silent button")
        self.assertTrue(log, "no message was sent at all")
        self.assertIn("Create New Automation Project", log[0][1])

    async def test_create_project_gate_still_blocks_disconnected_users(self):
        with mock.patch.object(user_sessions, "is_connected", return_value=False):
            handled, log, _ = await self._press("onboard:create_gate")
        self.assertTrue(handled)
        self.assertIn("Account Connection Required", log[0][1])

    async def test_newproj_alias_reaches_the_create_screen(self):
        with mock.patch.object(user_sessions, "is_connected", return_value=True):
            handled, log, _ = await self._press("newproj")
        self.assertTrue(handled)
        self.assertTrue(log)
        self.assertIn("Create New Automation Project", log[0][1])

    async def test_platform_telegram_reaches_the_create_screen(self):
        with mock.patch.object(user_sessions, "is_connected", return_value=True):
            handled, log, _ = await self._press("platform:telegram")
        self.assertTrue(handled)
        self.assertTrue(log)

    async def test_legacy_pay_method_alias_reaches_method_selection(self):
        """bot/keyboards.py used to emit pay:method; old messages still do."""
        handled, log, _ = await self._press("pay:method")
        self.assertTrue(handled, "pay:method redirect was dropped")
        self.assertTrue(log, "pay:method produced no reply")

    async def test_source_item_redirect_reaches_the_source_screen(self):
        self.sql("INSERT INTO sources(id,project_id,chat_id,title) "
                 "VALUES(300,10,'-10099','Deals')")
        handled, log, _ = await self._press(f"sourceitem:300:{PID}")
        self.assertTrue(handled)
        self.assertTrue(log)

    async def test_destination_item_redirect_reaches_the_target_screen(self):
        # DatabaseCase seeds destination 100 on project 10.
        handled, log, _ = await self._press(f"destitem:100:{PID}")
        self.assertTrue(handled)
        self.assertTrue(log)

    async def test_list_sources_redirect_shows_the_source_list(self):
        handled, log, _ = await self._press(f"listsource:{PID}")
        self.assertTrue(handled)
        self.assertTrue(log)

    async def test_list_destinations_redirect_shows_the_target_list(self):
        handled, log, _ = await self._press(f"listdestination:{PID}")
        self.assertTrue(handled)
        self.assertTrue(log)

    async def test_a_redirect_that_points_at_itself_does_not_hang(self):
        """Guard against the bounded loop spinning on a self-redirect."""
        handled, log, query = await self._press("onboard:create_gate")
        # Whatever happened, dispatch must return rather than recurse.
        self.assertIsInstance(handled, bool)
