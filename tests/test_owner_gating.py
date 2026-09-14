"""/owner must be owner-only, and the owner must be able to get in.

Two problems, both about identity:

1. /owner was routed to handlers_admin.owner_panel_cmd, which checked
   ADMIN_IDS. Every admin - not just the owner - could open the console
   that approves payments and lists the whole userbase, with no
   challenge. The properly secured flow in bot/owner_panel.py (OWNER_ID
   plus a 3-step challenge) existed but was never registered, so it was
   dead code.

2. That challenge cannot be completed if OWNER_USERNAME /
   OWNER_PASSWORD_HASH / OWNER_SECURITY_ANSWER_HASH are unset: every
   answer hashes to something that never equals "". /owner used to start
   it anyway, so the owner was locked out of their own console with no
   explanation.

These tests pin both: admins stay out, and the owner is told what is
missing instead of being handed an impossible prompt.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

from test_hardening import DatabaseCase  # noqa: E402

from bot import handlers_admin, owner_panel  # noqa: E402

OWNER = 700
ADMIN = 701
STRANGER = 702


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.first_name = "Tester"


class _FakeMessage:
    def __init__(self, log):
        self.log = log

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.log.append(text)
        return self


class _FakeUpdate:
    def __init__(self, log, uid, text=""):
        self.effective_user = _FakeUser(uid)
        self.message = _FakeMessage(log)
        self.message.text = text


def _patch_owner(owner_id=OWNER, username="boss", password="pw", answer="cat"):
    """Patches owner identity and challenge credentials on the module.

    owner_panel binds these at import time, so the module attributes -
    not config - are what the command actually reads.
    """
    return (
        mock.patch.object(owner_panel, "OWNER_ID", owner_id),
        mock.patch.object(owner_panel, "OWNER_USERNAME", username),
        mock.patch.object(owner_panel, "OWNER_PASSWORD_HASH",
                          owner_panel._make_hash(password) if password else ""),
        mock.patch.object(owner_panel, "OWNER_SECURITY_ANSWER_HASH",
                          owner_panel._make_hash(answer) if answer else ""),
    )


class OwnerRoutingTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        owner_panel._owner_sessions.clear()
        owner_panel._owner_auth_flow.clear()

    def tearDown(self):
        owner_panel._owner_sessions.clear()
        owner_panel._owner_auth_flow.clear()
        super().tearDown()

    async def _run(self, user_id, **creds):
        log = []
        patches = _patch_owner(**creds)
        for patcher in patches:
            patcher.start()
        try:
            await owner_panel.owner_command(_FakeUpdate(log, user_id), None)
        finally:
            for patcher in patches:
                patcher.stop()
        return log

    # ------------------------------------------------------------------

    async def test_non_owner_is_rejected(self):
        log = await self._run(STRANGER)
        self.assertTrue(log)
        self.assertIn("not authorized", log[0])

    async def test_admin_who_is_not_owner_is_rejected(self):
        """The regression: ADMIN_IDS used to open the owner console."""
        with mock.patch.object(handlers_admin, "ADMIN_IDS", {ADMIN}):
            log = await self._run(ADMIN)
        self.assertTrue(log)
        self.assertIn("not authorized", log[0])

    async def test_admin_shortcut_is_also_gated_on_owner(self):
        """Backstop: the old entry point must not be an admin back door."""
        with mock.patch.object(handlers_admin, "ADMIN_IDS", {ADMIN}), \
             mock.patch("config.OWNER_ID", OWNER):
            log = []
            await handlers_admin.owner_panel_cmd(_FakeUpdate(log, ADMIN), None)
        self.assertTrue(log)
        self.assertIn("Platform owner only", log[0])

    async def test_owner_id_unset_says_so_instead_of_crying_unauthorised(self):
        log = await self._run(OWNER, owner_id=None)
        self.assertTrue(log)
        self.assertIn("No owner is configured", log[0])

    async def test_missing_credentials_explain_themselves(self):
        """An unwinnable challenge is worse than no challenge."""
        log = await self._run(OWNER, username="", password="", answer="")
        self.assertTrue(log)
        self.assertIn("not configured", log[0])
        self.assertIn("OWNER_PASSWORD_HASH", log[0])
        # No challenge must have been started.
        self.assertEqual({}, owner_panel._owner_auth_flow)

    async def test_configured_owner_starts_the_challenge(self):
        log = await self._run(OWNER)
        self.assertTrue(log)
        self.assertIn("Step 1/3", log[0])
        self.assertIn(OWNER, owner_panel._owner_auth_flow)

    async def test_existing_session_skips_the_challenge(self):
        import time
        owner_panel._owner_sessions[OWNER] = {
            "expires_at": time.time() + 600, "authenticated": 1}
        log = await self._run(OWNER)
        self.assertTrue(log)
        self.assertIn("already authenticated", log[0])


class OwnerAuthRouterTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        owner_panel._owner_auth_flow.clear()

    def tearDown(self):
        owner_panel._owner_auth_flow.clear()
        super().tearDown()

    async def test_passes_through_when_no_challenge_is_running(self):
        from telegram.ext import ApplicationHandlerStop
        log = []
        # Must not raise, and must not reply: ordinary users' messages
        # belong to the menu router.
        await owner_panel.owner_auth_router(_FakeUpdate(log, STRANGER, "hi"), None)
        self.assertEqual([], log)

    async def test_consumes_the_answer_and_stops_the_update(self):
        """Otherwise the password is also read as a menu command."""
        from telegram.ext import ApplicationHandlerStop

        owner_panel._owner_auth_flow[OWNER] = {
            "step": "username", "attempts": 0, "started_at": __import__("time").time()}
        log = []
        with mock.patch.object(owner_panel, "OWNER_USERNAME", "boss"):
            with self.assertRaises(ApplicationHandlerStop):
                await owner_panel.owner_auth_router(
                    _FakeUpdate(log, OWNER, "boss"), None)
        self.assertTrue(log)
        self.assertIn("Step 2/3", log[0])


class OwnerChallengeFlowTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):
    """The three steps have to actually lead somewhere."""

    def setUp(self):
        super().setUp()
        owner_panel._owner_auth_flow.clear()
        owner_panel._owner_sessions.clear()

    def tearDown(self):
        owner_panel._owner_auth_flow.clear()
        owner_panel._owner_sessions.clear()
        super().tearDown()

    async def test_full_challenge_authenticates(self):
        import time
        owner_panel._owner_auth_flow[OWNER] = {
            "step": "username", "attempts": 0, "started_at": time.time()}
        log = []
        update = _FakeUpdate(log, OWNER)

        with mock.patch.object(owner_panel, "OWNER_USERNAME", "boss"), \
             mock.patch.object(owner_panel, "OWNER_PASSWORD_HASH",
                               owner_panel._make_hash("pw")), \
             mock.patch.object(owner_panel, "OWNER_SECURITY_ANSWER_HASH",
                               owner_panel._make_hash("cat")):
            update.message.text = "boss"
            await owner_panel.owner_auth_text_handler(update, None)
            update.message.text = "pw"
            await owner_panel.owner_auth_text_handler(update, None)
            update.message.text = "cat"
            await owner_panel.owner_auth_text_handler(update, None)

        self.assertTrue(owner_panel.is_owner_authenticated(OWNER))
        self.assertIn("authentication complete", log[-1])
        self.assertEqual({}, owner_panel._owner_auth_flow)

    async def test_cancel_escapes_the_challenge(self):
        import time
        owner_panel._owner_auth_flow[OWNER] = {
            "step": "username", "attempts": 0, "started_at": time.time()}
        log = []
        update = _FakeUpdate(log, OWNER)
        update.message.text = "/cancel"
        await owner_panel.owner_auth_text_handler(update, None)
        self.assertEqual({}, owner_panel._owner_auth_flow)
        self.assertIn("cancelled", log[0])
