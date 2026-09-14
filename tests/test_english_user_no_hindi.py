"""An English user must never be answered in Hindi.

The original complaint was that picking English still produced Hinglish.
Strings were moved behind a language branch to fix that, but fixing the
ones someone noticed is not the same as proving none remain - and a
hardcoded Hindi string is invisible to a test that only looks for
Devanagari, because romanised Hindi ("aapka account connected hai") is
just English letters.

So this walks every screen the bot can show with the user's language set
to English and fails on any Hindi that appears, in either script.

Two things are deliberately not leaks:
  * the language picker, which shows every language at once by design;
  * pressing lang:hi, which is *supposed* to switch the user to Hindi.
"""

import asyncio
import os
import unittest

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

import tools_language_audit  # noqa: E402


class EnglishUserSeesEnglish(unittest.TestCase):

    def test_no_hindi_reaches_an_english_user(self):
        exit_code = asyncio.run(tools_language_audit.run())
        self.assertEqual(
            0, exit_code,
            "Hindi was shown to a user whose language is English. Run "
            "tools_language_audit.py for the full list.")


if __name__ == "__main__":
    unittest.main()
