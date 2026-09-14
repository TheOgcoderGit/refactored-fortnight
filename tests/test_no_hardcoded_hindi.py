"""Guards against hardcoded Hindi in the bot's own code.

A user with English selected was being answered in Hinglish, because
reply strings were written inline instead of going through i18n_service.
Those particular strings have moved into i18n_service now; this test stops
the next ones from appearing.

Only Devanagari is detectable reliably — romanized Hindi is
indistinguishable from English by inspection — so this catches the clear
cases and the rest is down to review.

Exemptions
----------
bot/keyboards.py LANGUAGE_KEYBOARD: a language picker must show each
language in its own script ("हिंदी", "اردو", "العربية"), not translated.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BOT_DIR = ROOT / "bot"

DEVANAGARI = re.compile(r"[ऀ-ॿ]")

# (file, substring) pairs that are allowed to contain Devanagari.
ALLOWED = {
    ("bot/keyboards.py", "हिंदी"),
    ("bot/keyboards.py", "বাংলা"),
    ("bot/keyboards.py", "اردو"),
    ("bot/keyboards.py", "العربية"),
}


def _offenders():
    found = []
    for path in sorted(BOT_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(ROOT))
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            if not DEVANAGARI.search(line):
                continue
            if any(rel == f and token in line for f, token in ALLOWED):
                continue
            found.append(f"{rel}:{lineno}  {line.strip()[:100]}")
    return found


class NoHardcodedHindiTest(unittest.TestCase):

    def test_bot_code_has_no_hardcoded_devanagari(self):
        offenders = _offenders()
        self.assertEqual(
            [], offenders,
            "Found hardcoded Hindi in bot/. User-facing text belongs in "
            "services/i18n_service.py so it follows the language the user "
            "picked:\n  " + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
