"""The two ways to create a project, walked end to end.

Both were reported broken: the bot asked for the project name (or the
target channel) and then answered nothing, so no project was ever
created. The cause was in dispatch_callback, which dropped redirects - see
tests/test_callback_redirects.py.

These tests walk the flows the way a user does: tap buttons, then type.
They exist so "create a project" can never silently stop working again.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

from test_hardening import DatabaseCase  # noqa: E402

from bot import handlers, handlers_projects  # noqa: E402
from core import telegram_utils, user_sessions  # noqa: E402
from services import plan_service  # noqa: E402

UID = 901
PID = 910


def _target_chat():
    return {"chat_id": "-100999", "username": "mytarget",
            "title": "My Target", "type": "channel"}


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


class _FakeUpdate:
    def __init__(self, log, text):
        self.log = log
        self.effective_user = _User(UID)
        self.effective_message = _FakeMessage(log)
        self.effective_message.text = text
        self.message = self.effective_message


class ProjectCreationFlowTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        self.sql("INSERT INTO users(telegram_id) VALUES(?)", (UID,))
        plan_service.invalidate_plan_configs_cache()
        self._connected = mock.patch.object(user_sessions, "is_connected",
                                            return_value=True)
        self._connected.start()

    def tearDown(self):
        self._connected.stop()
        for store in (handlers_projects.WAITING_PROJECT_NAME,
                      handlers_projects.PENDING_TEMPLATE_CHOICE,
                      handlers_projects.WAITING_TEMPLATE_TARGET,
                      handlers_projects.TEMPLATE_SOURCE_DRAFT,
                      handlers_projects.WAITING_TEMPLATE_SOURCE):
            store.pop(UID, None)
        super().tearDown()

    # ------------------------------------------------------------------

    async def _tap(self, data):
        log = []
        parts = data.split(":")
        handled = await handlers.dispatch_callback(
            _FakeQuery(log, data), UID, parts[0], parts, None)
        return handled, log

    async def _type(self, text):
        log = []
        await handlers.menu_handler(_FakeUpdate(log, text), None)
        return log

    def _projects(self):
        return self.sql("SELECT id, name FROM projects WHERE user_id=?", (UID,))

    # ------------------------------------------------------------------

    async def test_manual_creation_from_the_how_it_works_button(self):
        """The exact path reported: ➕ Create Project -> Manual -> name."""
        handled, log = await self._tap("onboard:create_gate")
        self.assertTrue(handled, "create button did nothing")
        self.assertTrue(log, "create button produced no message")

        handled, log = await self._tap("proj:manual")
        self.assertTrue(handled)
        self.assertTrue(log, "no name prompt was shown")

        log = await self._type("My Deals Flow")
        self.assertTrue(log, "typing the name produced no reply at all")

        projects = self._projects()
        self.assertEqual(1, len(projects), f"project not created: {projects}")
        self.assertEqual("My Deals Flow", projects[0]["name"])

    async def test_manual_creation_from_the_projects_screen(self):
        await self._tap("newproj")
        await self._tap("proj:manual")
        log = await self._type("Second Project")
        self.assertTrue(log, "typing the name produced no reply")
        self.assertEqual(1, len(self._projects()))

    async def test_template_creation_creates_the_project(self):
        """Pick a template, give a target, and the project must appear."""
        handled, log = await self._tap("proj:show_templates")
        self.assertTrue(handled)

        handled, log = await self._tap("proj:tpl_view:deals")
        self.assertTrue(handled)
        self.assertTrue(log, "template preview was empty")

        handled, log = await self._tap("proj:tpl_confirm:deals")
        self.assertTrue(handled)
        self.assertTrue(log, "no target prompt was shown")
        self.assertEqual("deals", handlers_projects.WAITING_TEMPLATE_TARGET.get(UID))

        async def _resolve(reference, for_destination=False, user_id=None):
            return _target_chat()

        with mock.patch.object(telegram_utils, "get_chat", side_effect=_resolve), \
             mock.patch.object(handlers_projects, "get_chat", side_effect=_resolve):
            log = await self._type("@mytarget")

        self.assertTrue(log, "typing the target produced no reply at all")

        projects = self._projects()
        self.assertEqual(1, len(projects), f"project not created: {projects}")

        dests = self.sql(
            "SELECT chat_id FROM destinations WHERE project_id=?",
            (projects[0]["id"],))
        self.assertEqual(["-100999"], [d["chat_id"] for d in dests])

    async def test_template_flow_reports_a_failure_instead_of_going_quiet(self):
        """A target that cannot be resolved must still answer the user."""
        await self._tap("proj:tpl_confirm:deals")

        async def _boom(reference, for_destination=False, user_id=None):
            raise telegram_utils.ChatNotFoundError("no such channel")

        with mock.patch.object(telegram_utils, "get_chat", side_effect=_boom), \
             mock.patch.object(handlers_projects, "get_chat", side_effect=_boom):
            log = await self._type("@doesnotexist")

        self.assertTrue(log, "an unresolvable target produced silence")
        self.assertEqual(0, len(self._projects()))
