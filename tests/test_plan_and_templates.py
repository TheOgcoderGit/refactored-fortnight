"""Plan/trial countdown, template source editing, clone navigation and the
global cancel.

These cover the last items from the user's list:
  * "Plan ya trial jo h wo dikhe ki itna day baaki h" - plan_expiry was
    stored and enforced but never surfaced anywhere.
  * "template se project create ki jaha par pre configured sources hota h
    waha pe buttons do edit ... confirm, back or home" - the sources were
    baked into the template and applied silently on confirm.
  * "clone hote hi ek user ko message mile ... redirect buttons do" - the
    clone confirmation now names the copy and links back to Projects.
  * /cancel must clear pending state in every module, not just onboarding.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

from test_hardening import DatabaseCase  # noqa: E402

from services import plan_service  # noqa: E402

USER_ID = 801
PROJECT_ID = 810


class _FakeMessage:
    def __init__(self, log):
        self.log = log

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("reply", text, reply_markup))
        return self

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("edit", text, reply_markup))
        return self


class _FakeQuery:
    def __init__(self, log, data=""):
        self.log = log
        self.data = data
        self.message = _FakeMessage(log)
        self.from_user = type("U", (), {"id": USER_ID, "first_name": "Tester"})()
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("edit", text, reply_markup))


# ==========================================================================
# plan / trial countdown
# ==========================================================================


class PlanStatusTests(DatabaseCase, unittest.TestCase):

    def setUp(self):
        super().setUp()
        # DatabaseCase only seeds users 1 and 2; set_user_plan() is an
        # UPDATE, so the row has to exist.
        self.sql("INSERT INTO users(telegram_id) VALUES(?)", (USER_ID,))

    def _set_plan(self, plan, expiry):
        plan_service.set_user_plan(USER_ID, plan, expiry=expiry)

    def test_no_expiry_reports_no_countdown(self):
        status = plan_service.get_plan_status(USER_ID)
        self.assertEqual(status["plan"], "FREE")
        self.assertIsNone(status["days_left"])
        self.assertFalse(status["on_trial"])

    def test_trial_reports_days_left(self):
        """The 7-day Creator trial must be visible, not silent."""
        expiry = datetime.now(timezone.utc) + timedelta(days=7)
        self._set_plan("CREATOR", expiry.isoformat())

        status = plan_service.get_plan_status(USER_ID)
        self.assertEqual(status["plan"], "CREATOR")
        self.assertEqual(status["days_left"], 7)
        self.assertTrue(status["on_trial"])
        self.assertFalse(status["expired"])

    def test_expired_plan_reports_ended_and_downgrades(self):
        expiry = datetime.now(timezone.utc) - timedelta(days=1)
        self._set_plan("PRO", expiry.isoformat())

        status = plan_service.get_plan_status(USER_ID)
        self.assertTrue(status["expired"])
        self.assertEqual(status["plan"], "FREE")     # downgraded
        self.assertEqual(status["stored_plan"], "PRO")
        self.assertEqual(status["days_left"], 0)

    def test_days_left_counts_down(self):
        for days, expected in ((6, 6), (1, 1), (0, 0)):
            with self.subTest(days=days):
                expiry = datetime.now(timezone.utc) + timedelta(days=days)
                self._set_plan("PRO", expiry.isoformat())
                self.assertEqual(plan_service.get_plan_status(USER_ID)["days_left"],
                                 expected)

    def test_entitlements_include_the_countdown(self):
        expiry = datetime.now(timezone.utc) + timedelta(days=3)
        self._set_plan("CREATOR", expiry.isoformat())

        ent = plan_service.get_entitlements(USER_ID)
        self.assertEqual(ent["days_left"], 3)
        self.assertTrue(ent["on_trial"])
        self.assertEqual(ent["plan"], "CREATOR")

    def test_one_users_plan_does_not_leak_to_another(self):
        self._set_plan("CREATOR", (datetime.now(timezone.utc)
                                   + timedelta(days=7)).isoformat())
        other = plan_service.get_plan_status(USER_ID + 1)
        self.assertEqual(other["plan"], "FREE")
        self.assertIsNone(other["days_left"])


# ==========================================================================
# template sources
# ==========================================================================


class TemplateSourceEditTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        from bot import handlers_projects as hp
        self.hp = hp
        hp.TEMPLATE_SOURCE_DRAFT.clear()
        hp.WAITING_TEMPLATE_SOURCE.clear()
        self.log = []

    def tearDown(self):
        self.hp.TEMPLATE_SOURCE_DRAFT.clear()
        self.hp.WAITING_TEMPLATE_SOURCE.clear()
        super().tearDown()

    async def press(self, data):
        parts = data.split(":")
        query = _FakeQuery(self.log, data)
        await self.hp.handle_callbacks(query, USER_ID, parts[0], parts, None)
        return query

    async def test_template_screen_offers_edit_confirm_back_home(self):
        query = await self.press("proj:tpl_view:news")
        markup = self.log[-1][2]
        emitted = {b.callback_data for row in markup.inline_keyboard for b in row}
        self.assertIn("proj:tpl_sources:news", emitted)     # Edit
        self.assertIn("proj:tpl_confirm:news", emitted)     # Confirm
        self.assertIn("proj:show_templates", emitted)       # Back
        self.assertIn("nav:home", emitted)                  # Home

    async def test_defaults_come_from_the_catalog(self):
        sources = self.hp._template_sources(USER_ID, "news")
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0][0], "@TechNewsDailyLive")

    async def test_removing_a_source_updates_the_draft(self):
        await self.press("proj:tpl_srcdel:news:0")
        sources = self.hp._template_sources(USER_ID, "news")
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0][0], "@BreakingNewsGlobal")

    async def test_other_user_edits_do_not_leak(self):
        await self.press("proj:tpl_srcdel:news:0")
        other = self.hp._template_sources(USER_ID + 1, "news")
        self.assertEqual(len(other), 2)

    async def test_adding_a_source(self):
        self.hp.WAITING_TEMPLATE_SOURCE[USER_ID] = "news"
        message = _FakeMessage(self.log)
        await self.hp.handle_text(message, USER_ID, "@MyOwnChannel", None)

        sources = self.hp._template_sources(USER_ID, "news")
        self.assertIn("@MyOwnChannel", [h for h, _d in sources])

    async def test_typo_is_rejected_without_a_network_call(self):
        """'Hi' is not a channel reference - catch it before resolving."""
        self.hp.WAITING_TEMPLATE_SOURCE[USER_ID] = "news"
        message = _FakeMessage(self.log)
        await self.hp.handle_text(message, USER_ID, "Hi", None)

        sources = self.hp._template_sources(USER_ID, "news")
        self.assertEqual(len(sources), 2)   # nothing added
        self.assertIn("doesn't look like a channel", self.log[-1][1])

    async def test_reset_restores_defaults(self):
        await self.press("proj:tpl_srcdel:news:0")
        self.assertEqual(len(self.hp._template_sources(USER_ID, "news")), 1)

        await self.press("proj:tpl_srcreset:news")
        self.assertEqual(len(self.hp._template_sources(USER_ID, "news")), 2)


# ==========================================================================
# clone
# ==========================================================================


class CloneConfirmationTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):
    # DatabaseCase seeds project 10 for user 1. The FREE plan allows a
    # single project, so the limit has to be lifted for the clone to run.
    PID = 10

    def setUp(self):
        super().setUp()
        # The FREE plan allows a single project, so lift max_projects for
        # the clone to run (limits() only touches the forward counters).
        self.sql("UPDATE plan_configs SET max_projects=10 WHERE plan_name='FREE'")
        plan_service.invalidate_plan_configs_cache()

    async def test_clone_message_links_back_to_projects(self):
        from bot import handlers_projects as hp

        log = []
        query = _FakeQuery(log, f"proj:clone:{self.PID}")
        await hp.handle_callbacks(query, 1, "proj",
                                  ["proj", "clone", str(self.PID)], None)

        text = log[-1][1]
        markup = log[-1][2]
        emitted = {b.callback_data for row in markup.inline_keyboard for b in row}

        self.assertIn("cloned", text.lower())
        self.assertIn("Paused", text)          # copy does not auto-forward
        self.assertIn("nav:projects", emitted)  # back to the project list
        self.assertIn("nav:home", emitted)


# ==========================================================================
# global cancel
# ==========================================================================


class GlobalCancelTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):

    async def test_cancel_clears_pending_state_in_every_module(self):
        from bot import error_actions, handlers_projects as hp
        from bot import handlers_features, handlers_nav, handlers_admin

        hp.WAITING_SOURCE[USER_ID] = True
        hp.CURRENT_PROJECT[USER_ID] = PROJECT_ID
        handlers_features.PENDING_INPUT[USER_ID] = {"kind": "prefix"}
        handlers_nav.PENDING_INPUT[USER_ID] = {"kind": "coupon"}
        handlers_admin.WAITING_AI_SUPPORT[USER_ID] = True

        self.assertTrue(error_actions.cancel_pending(USER_ID))

        self.assertNotIn(USER_ID, hp.WAITING_SOURCE)
        self.assertNotIn(USER_ID, hp.CURRENT_PROJECT)
        self.assertNotIn(USER_ID, handlers_features.PENDING_INPUT)
        self.assertNotIn(USER_ID, handlers_nav.PENDING_INPUT)
        self.assertNotIn(USER_ID, handlers_admin.WAITING_AI_SUPPORT)

    async def test_cancel_reports_false_when_nothing_was_pending(self):
        from bot import error_actions
        self.assertFalse(error_actions.cancel_pending(USER_ID))

    async def test_slash_cancel_text_command_is_swallowed(self):
        """A stray /cancel must not be stored as a channel name."""
        from bot import handlers_projects as hp

        # A pending "add source" prompt: without the cancel branch this text
        # would be stored as the channel name.
        hp.WAITING_SOURCE[USER_ID] = True
        hp.CURRENT_PROJECT[USER_ID] = PROJECT_ID

        log = []
        message = _FakeMessage(log)
        handled = await hp.handle_text(message, USER_ID, "/cancel", None)

        self.assertTrue(handled)
        self.assertIn("Cancelled", log[-1][1])
        self.assertNotIn(USER_ID, hp.WAITING_SOURCE)


if __name__ == "__main__":
    unittest.main()
