"""Every button the UI can produce must actually do something.

This is the regression test behind tools_dead_buttons.py. The original
defect was silence, not a crash: a button rendered, the callback arrived,
and no handler claimed it. Unit tests of individual services never caught
that, so this test presses every ``callback_data`` literal in ``bot/``
through the bot's real dispatch table.

It fails when:
  * a reachable callback is claimed by nobody (dead button), or
  * pressing it raises (stale-state crash).
"""

import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

import tools_dead_buttons as sweep  # noqa: E402

BOT_DIR = ROOT / "bot"


class NoDeadButtonsTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_reachable_button_is_handled_without_crashing(self):
        code = await sweep.run(BOT_DIR, quiet=True)
        self.assertEqual(code, 0, sweep.last_report())


if __name__ == "__main__":
    unittest.main()
