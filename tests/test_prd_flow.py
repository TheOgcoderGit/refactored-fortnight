"""
ChannelFlow AI - PRD Implementation Verification Test Suite
Runs offline against an isolated SQLite test database.
"""
import os
import unittest
import tempfile
import pathlib

# config.py refuses to import without credentials, and core.user_sessions
# imports it at module scope. The suite must run on a clean checkout with no
# .env, so seed harmless placeholders first. A real .env still wins because
# config.py only applies its values with os.environ.setdefault().
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

from database import db, hardening
from services import feature_explorer_service as explorer
from services import post_edit_sync_service as edit_sync
from services import auto_reaction_service as reactions
from core.user_sessions import extract_otp_from_command

class PRDFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_db = db.DB_NAME
        db.DB_NAME = str(pathlib.Path(self.tmp.name) / "test_channelflow.db")
        db.init_db()
        conn = db.get_connection()
        hardening.migrate(conn.cursor())
        # post_edit_mappings/confirmed_reactions etc. have a FOREIGN KEY on
        # project_id -> projects(id); seed the user/project the tests
        # reference (id=1) so those inserts don't fail the FK check.
        conn.execute("INSERT INTO users(telegram_id) VALUES(1)")
        conn.execute("INSERT INTO projects(id,user_id,name,status) VALUES(1,1,'Test Project',1)")
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_NAME = self.orig_db
        self.tmp.cleanup()

    def test_feature_explorer_plan_pagination(self):
        """Validates PRD §5.4: Plan-by-plan feature catalog and pagination."""
        items, page, total_pages = explorer.get_plan_page("FREE", 0)
        self.assertGreater(len(items), 0)
        self.assertLessEqual(len(items), 8)
        self.assertEqual(page, 0)
        
        # Verify Pro contains cumulative or specific pro tools
        pro_items, _, _ = explorer.get_plan_page("PRO", 0)
        item_names = [i["name"] for i in pro_items]
        self.assertTrue(any("Auto Forwarding" in name for name in item_names))

    def test_flow_otp_parsing(self):
        """Validates PRD §6.3: Strict FLOW<OTP> command enforcement."""
        # Valid inputs
        ok, code = extract_otp_from_command("FLOW12345")
        self.assertTrue(ok)
        self.assertEqual(code, "12345")

        ok, code = extract_otp_from_command("FLOW 98765")
        self.assertTrue(ok)
        self.assertEqual(code, "98765")

        # Invalid / Bare Digits rejected
        ok, err = extract_otp_from_command("12345")
        self.assertFalse(ok)
        self.assertIn("FLOW", err)

        ok, err = extract_otp_from_command("FLOW")
        self.assertFalse(ok)

    def test_post_edit_sync_mapping(self):
        """Validates PRD §16, F153: Post Edit Sync record and lookup."""
        edit_sync.record_mapping(1, "-1001", 100, "-1002", 200)
        mappings = edit_sync.get_target_messages(1, "-1001", 100)
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]["target_chat_id"], "-1002")
        self.assertEqual(mappings[0]["target_message_id"], 200)

    def test_auto_reaction_idempotency(self):
        """Validates PRD §16, F156-F158: Auto reaction idempotency tracking."""
        self.assertFalse(reactions.is_reaction_confirmed(1, "-1001", 50, "👍"))
        reactions.confirm_reaction(1, "-1001", 50, "👍")
        self.assertTrue(reactions.is_reaction_confirmed(1, "-1001", 50, "👍"))

if __name__ == "__main__":
    unittest.main()