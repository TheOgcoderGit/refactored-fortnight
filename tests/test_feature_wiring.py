"""End-to-end wiring test for the project feature hubs and navigation.

These tests exist because the original defect was not a crash - it was
*silence*. Every feature button existed in the UI, the callback reached the
router, and the router quietly fell through because nothing claimed it. A
unit test of a single service would never have caught that, so these tests
drive the real callback router with a fake Telegram query and assert that
each screen actually renders something.

No Telegram API calls: the query/message objects are in-process fakes that
record what the handler tried to send.
"""

import os
import unittest
from unittest.mock import AsyncMock

# config.py raises at import time when these are missing, and importing the
# bot handlers pulls in config. Seed them so this module runs with or without
# a .env file present.
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

from test_hardening import DatabaseCase  # noqa: E402

from bot import handlers_features, handlers_nav, handlers_projects

USER_ID = 1
PROJECT_ID = 10


class FakeMessage:
    def __init__(self, recorder):
        self._recorder = recorder

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self._recorder.append(("reply", text, reply_markup))
        return self

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self._recorder.append(("edit", text, reply_markup))
        return self

    async def delete(self):
        self._recorder.append(("delete", "", None))


class FakeQuery:
    """Minimal stand-in for telegram.CallbackQuery.

    ``answer`` is an AsyncMock so tests can assert on alert/notice calls.
    """

    def __init__(self, recorder, user_id=USER_ID):
        self.recorder = recorder
        self.message = FakeMessage(recorder)
        self.data = ""
        self.from_user = type("U", (), {"id": user_id})()
        self.answer = AsyncMock()

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.recorder.append(("edit", text, reply_markup))

    @property
    def text(self):
        """Last text written to this query."""
        for kind, body, _markup in reversed(self.recorder):
            if kind in ("edit", "reply"):
                return body
        return ""


class FeatureWiringTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        self.rendered = []
        # DatabaseCase seeds projects 10/11 (user 1) and 20 (user 2) plus
        # destinations 100/101 for project 10, but no sources - several
        # screens and the stale-button regressions need one.
        if not self.sql("SELECT id FROM sources WHERE project_id=?", (PROJECT_ID,)):
            self.sql("INSERT INTO sources(id, project_id, chat_id, title) "
                     "VALUES(1, ?, '-90001', 'Test Source')", (PROJECT_ID,))

    def new_query(self, data=""):
        q = FakeQuery(self.rendered)
        q.data = data
        return q

    async def press(self, data: str):
        """Run one callback through the same router main.py uses."""
        query = self.new_query(data)
        parts = data.split(":")
        handled = await handlers_projects.handle_callbacks(
            query, USER_ID, parts[0], parts, context=None
        )
        return handled, query

    async def press_nav(self, data: str):
        query = self.new_query(data)
        parts = data.split(":")
        handled = await handlers_nav.handle_callbacks(
            query, USER_ID, parts[0], parts, context=None
        )
        return handled, query

    def assertRendered(self, query, needle=None):
        self.assertTrue(
            self.rendered,
            f"callback {query.data!r} produced no output at all (dead button)",
        )
        if needle:
            self.assertIn(needle, query.text,
                          f"callback {query.data!r} did not render {needle!r}")

    # ------------------------------------------------------------------
    # every hub reachable from the project dashboard
    # ------------------------------------------------------------------

    async def test_forwarding_hub_renders(self):
        handled, query = await self.press(f"fwd:settings:{PROJECT_ID}")
        self.assertTrue(handled)
        self.assertRendered(query, "Forwarding")

    async def test_filters_hub_renders(self):
        handled, query = await self.press(f"flt:hub:{PROJECT_ID}")
        self.assertTrue(handled)
        self.assertRendered(query, "Filters")

    async def test_formatting_hub_renders(self):
        handled, query = await self.press(f"fmt:hub:{PROJECT_ID}")
        self.assertTrue(handled)
        self.assertRendered(query, "Formatting")

    async def test_watermark_hub_renders(self):
        handled, query = await self.press(f"wm:hub:{PROJECT_ID}")
        self.assertTrue(handled)
        self.assertRendered(query, "Watermark")

    async def test_ai_hub_renders(self):
        handled, query = await self.press(f"ai:hub:{PROJECT_ID}")
        self.assertTrue(handled)
        # Free plan has no ai_rewrite entitlement -> honest upgrade gate.
        self.assertRendered(query)

    async def test_affiliate_hub_renders(self):
        handled, query = await self.press(f"aff:hub:{PROJECT_ID}")
        self.assertTrue(handled)
        self.assertRendered(query, "Affiliate")

    async def test_auto_and_edits_hub_renders(self):
        handled, query = await self.press(f"auto:hub:{PROJECT_ID}")
        self.assertTrue(handled)
        self.assertRendered(query, "Post Edit Sync")

    async def test_pacing_hub_renders(self):
        handled, query = await self.press(f"spd:hub:{PROJECT_ID}")
        self.assertTrue(handled)
        self.assertRendered(query, "Pacing")

    async def test_activity_renders(self):
        handled, query = await self.press(f"act:view:{PROJECT_ID}")
        self.assertTrue(handled)
        self.assertRendered(query, "Activity")

    async def test_stats_renders(self):
        handled, query = await self.press(f"stats:{PROJECT_ID}")
        self.assertTrue(handled)
        self.assertRendered(query, "Analytics")

    async def test_edit_project_root_renders(self):
        handled, query = await self.press(f"editproj:{PROJECT_ID}")
        self.assertTrue(handled)
        self.assertRendered(query, "Edit Project")

    async def test_dryrun_prompts_for_sample(self):
        handled, query = await self.press(f"proj:dryrun:{PROJECT_ID}")
        self.assertTrue(handled)
        self.assertRendered(query, "Dry-Run")

    # ------------------------------------------------------------------
    # mutations actually reach the services/database
    # ------------------------------------------------------------------

    async def test_mode_toggle_persists(self):
        await self.press(f"togglemode:{PROJECT_ID}")
        rows = self.sql("SELECT mode FROM project_settings WHERE project_id=?", (PROJECT_ID,))
        self.assertEqual(rows[0]["mode"], "copy")

    async def test_silent_toggle_persists(self):
        await self.press(f"togglesilent:{PROJECT_ID}")
        rows = self.sql("SELECT silent FROM project_settings WHERE project_id=?", (PROJECT_ID,))
        self.assertEqual(rows[0]["silent"], 1)

    async def test_media_filter_persists(self):
        await self.press(f"mediafilterset:{PROJECT_ID}:photo")
        rows = self.sql(
            "SELECT media_filter FROM project_settings WHERE project_id=?", (PROJECT_ID,))
        self.assertEqual(rows[0]["media_filter"], "photo")

    async def test_prefix_input_persists(self):
        query = self.new_query(f"fmtfield:{PROJECT_ID}:prefix")
        await handlers_features.handle_callbacks(
            query, USER_ID, "fmtfield", ["fmtfield", str(PROJECT_ID), "prefix"], None)

        message = FakeMessage(self.rendered)
        handled = await handlers_features.handle_text(message, USER_ID, "DEALS", None)
        self.assertTrue(handled)
        rows = self.sql("SELECT prefix FROM formatting_rules WHERE project_id=?", (PROJECT_ID,))
        self.assertEqual(rows[0]["prefix"], "DEALS")

    async def test_watermark_toggle_persists(self):
        from services import watermark_service
        # Watermark is a Pro/Creator feature; the free plan must be refused
        # rather than silently enabled.
        await self.press(f"wmtoggle:{PROJECT_ID}")
        settings = watermark_service.ensure_watermark_settings(PROJECT_ID)
        self.assertFalse(settings["enabled"])

    async def test_post_edit_sync_toggle_persists(self):
        from services import settings_service
        await self.press(f"auto:pestoggle:{PROJECT_ID}")
        self.assertEqual(settings_service.get_settings(PROJECT_ID)["post_edit_sync"], 1)
        await self.press(f"auto:pestoggle:{PROJECT_ID}")
        self.assertEqual(settings_service.get_settings(PROJECT_ID)["post_edit_sync"], 0)

    async def test_auto_reaction_row_created_and_toggled(self):
        await self.press(f"auto:hub:{PROJECT_ID}")
        rows = self.sql("SELECT * FROM auto_reactions WHERE project_id=?", (PROJECT_ID,))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["enabled"], 1)

        await self.press(f"auto:rtoggle:{PROJECT_ID}")
        rows = self.sql("SELECT enabled FROM auto_reactions WHERE project_id=?", (PROJECT_ID,))
        self.assertEqual(rows[0]["enabled"], 0)

    async def test_forwarder_reaction_helper_respects_disabled_flag(self):
        """The forwarder must not react when the rule is switched off."""
        from core import forwarder
        forwarder._REACTION_CACHE.clear()

        await self.press(f"auto:hub:{PROJECT_ID}")
        forwarder._REACTION_CACHE.clear()
        self.assertEqual(forwarder._reaction_for(PROJECT_ID, "destination"), "👍")

        await self.press(f"auto:rtoggle:{PROJECT_ID}")
        forwarder._REACTION_CACHE.clear()
        self.assertIsNone(forwarder._reaction_for(PROJECT_ID, "destination"))

    # ------------------------------------------------------------------
    # ownership
    # ------------------------------------------------------------------

    async def test_other_users_project_is_ignored(self):
        query = self.new_query(f"fmt:hub:{PROJECT_ID}")
        handled = await handlers_projects.handle_callbacks(
            query, 999, "fmt", ["fmt", str(PROJECT_ID)], None)
        self.assertFalse(handled)
        self.assertEqual(self.rendered, [])

    # ------------------------------------------------------------------
    # navigation / account / settings / help
    # ------------------------------------------------------------------

    async def test_settings_screen_renders(self):
        handled, query = await self.press_nav("nav:settings")
        self.assertTrue(handled)
        self.assertRendered(query, "Settings")

    async def test_help_screen_renders(self):
        handled, query = await self.press_nav("nav:help")
        self.assertTrue(handled)
        self.assertRendered(query, "Help")

    async def test_faq_renders(self):
        handled, query = await self.press_nav("help:faq")
        self.assertTrue(handled)
        self.assertRendered(query, "asked questions")

    async def test_guide_renders(self):
        handled, query = await self.press_nav("help:guide")
        self.assertTrue(handled)
        self.assertRendered(query, "Quick start")

    async def test_tour_renders(self):
        handled, query = await self.press_nav("help:tour")
        self.assertTrue(handled)
        self.assertRendered(query, "Bot tour")

    async def test_wallet_screen_renders(self):
        handled, query = await self.press_nav("acct:wallet")
        self.assertTrue(handled)
        self.assertRendered(query, "Wallet")

    async def test_payment_history_renders(self):
        handled, query = await self.press_nav("acct:payhistory")
        self.assertTrue(handled)
        self.assertRendered(query, "Payment history")

    async def test_wallet_history_renders(self):
        handled, query = await self.press_nav("acct:wallethistory")
        self.assertTrue(handled)
        self.assertRendered(query, "transactions")

    async def test_connections_renders(self):
        handled, query = await self.press_nav("acct:connections")
        self.assertTrue(handled)
        self.assertRendered(query, "Connected Accounts")

    async def test_notifications_renders(self):
        handled, query = await self.press_nav("settings:notifications")
        self.assertTrue(handled)
        self.assertRendered(query, "Notifications")

    async def test_system_status_renders(self):
        handled, query = await self.press_nav("settings:systatus")
        self.assertTrue(handled)
        self.assertRendered(query, "System status")

    async def test_language_picker_renders(self):
        handled, query = await self.press_nav("settings:language")
        self.assertTrue(handled)
        self.assertRendered(query, "language")

    async def test_coupon_redemption_prompt(self):
        handled, query = await self.press_nav("wallet:redeem")
        self.assertTrue(handled)
        self.assertRendered(query, "coupon")

    # ------------------------------------------------------------------
    # regressions found by tools_dead_buttons.py
    # ------------------------------------------------------------------

    async def test_language_button_applies_the_language(self):
        from services import i18n_service
        handled, query = await self.press_nav("lang:hi")
        self.assertTrue(handled)
        self.assertEqual(i18n_service.get_user_language(USER_ID), "hi")

        await self.press_nav("lang:en")
        self.assertEqual(i18n_service.get_user_language(USER_ID), "en")

    async def test_unknown_language_is_rejected_not_silently_applied(self):
        from services import i18n_service
        before = i18n_service.get_user_language(USER_ID)
        await self.press_nav("lang:klingon")
        self.assertEqual(i18n_service.get_user_language(USER_ID), before)

    async def test_support_ai_button_arms_the_assistant(self):
        from bot import handlers_admin
        query = self.new_query("sup:ai_start")
        handled = await handlers_admin.handle_callbacks(
            query, USER_ID, "sup", ["sup", "ai_start"], None)
        self.assertTrue(handled)
        self.assertTrue(handlers_admin.WAITING_AI_SUPPORT.get(USER_ID))
        self.assertRendered(query, "Ask me anything")
        handlers_admin.WAITING_AI_SUPPORT.pop(USER_ID, None)

    async def test_support_faq_button_renders(self):
        from bot import handlers_admin
        query = self.new_query("sup:faq")
        handled = await handlers_admin.handle_callbacks(
            query, USER_ID, "sup", ["sup", "faq"], None)
        self.assertTrue(handled)
        self.assertRendered(query)

    async def test_help_screen_tickets_use_live_actions(self):
        """The Help screen used to emit support:list / support:new, which no
        handler owns. It must point at the live sup:* ticket actions."""
        handled, query = await self.press_nav("nav:help")
        self.assertTrue(handled)
        markup = self.rendered[-1][2]
        emitted = {b.callback_data for row in markup.inline_keyboard for b in row}
        self.assertIn("sup:tickets", emitted)
        self.assertIn("sup:new_start", emitted)
        self.assertNotIn("support:list", emitted)

    async def test_locked_ai_screen_routes_to_plans(self):
        """'View plans' on the plan-gated AI screen was pay:method, which
        nothing handles - Free-plan users hit a dead button."""
        from bot import handlers_features
        from core.forwarder import force_refresh_routes
        query = self.new_query(f"ai:hub:{PROJECT_ID}")
        await handlers_features.handle_callbacks(
            query, USER_ID, "ai", ["ai", "hub", str(PROJECT_ID)], None)

        markup = self.rendered[-1][2]
        emitted = {b.callback_data for row in markup.inline_keyboard for b in row}
        if "💳 View plans" in str(self.rendered[-1][1]):
            self.assertNotIn("pay:method", emitted)
            self.assertIn("nav:plans", emitted)

    async def test_toggling_a_deleted_source_does_not_crash(self):
        """A stale Enable/Disable button on an old message used to raise
        TypeError on None instead of telling the user it is gone."""
        from services import source_service
        source_id = self.sql("SELECT id FROM sources WHERE project_id=?",
                             (PROJECT_ID,))[0]["id"]
        source_service.delete_source(source_id)

        query = self.new_query(f"src:toggle:{source_id}:{PROJECT_ID}")
        handled = await handlers_projects.handle_callbacks(
            query, USER_ID, "src",
            ["src", "toggle", str(source_id), str(PROJECT_ID)], None)
        self.assertTrue(handled)
        self.assertTrue(query.answer.await_count >= 1)

    async def test_other_users_source_toggle_is_ignored(self):
        source_id = self.sql("SELECT id FROM sources WHERE project_id=?",
                             (PROJECT_ID,))[0]["id"]
        before = self.sql("SELECT enabled FROM sources WHERE id=?", (source_id,))[0]["enabled"]
        query = self.new_query(f"src:toggle:{source_id}:{PROJECT_ID}")
        await handlers_projects.handle_callbacks(
            query, 999, "src",
            ["src", "toggle", str(source_id), str(PROJECT_ID)], None)

        # Handlers claim the callback (True) but must not act, and must not
        # render another user's project data.
        self.assertEqual(self.rendered, [])
        after = self.sql("SELECT enabled FROM sources WHERE id=?", (source_id,))[0]["enabled"]
        self.assertEqual(before, after)

    # ------------------------------------------------------------------
    # reply-keyboard menu labels
    # ------------------------------------------------------------------

    async def test_menu_labels_are_routed(self):
        for label in ("📁 Projects", "💳 Subscription", "🎁 Rewards", "👤 Account",
                      "🆘 Support", "⚙️ Settings", "📖 Guide", "🧭 Tour"):
            with self.subTest(label=label):
                self.rendered.clear()
                message = FakeMessage(self.rendered)
                handled = await handlers_nav.handle_text(message, USER_ID, label, None)
                self.assertTrue(handled, f"menu label {label!r} was not routed")
                self.assertTrue(self.rendered, f"menu label {label!r} produced no screen")

    async def test_unknown_text_is_not_swallowed(self):
        message = FakeMessage(self.rendered)
        handled = await handlers_nav.handle_text(message, USER_ID, "random chatter", None)
        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
