"""Every failure that offers "Try again" must be able to reopen itself.

When a source or target could not be resolved, the failure screen offers
🔁 Try again. That only means something if the bot remembers which prompt
to reopen - and remember_retry() records a kind that _reopen_prompt()
must know how to rebuild.

A kind that is recorded but not reopened leaves the user on a screen
whose main button does nothing, which is worse than no button at all:
they press it, nothing happens, and they assume the feature is broken.

The reverse matters just as much, so this checks both directions -
every kind the code records must be re-openable.
"""

import os
import re
import pathlib
import unittest

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

import asyncio  # noqa: E402

from test_hardening import DatabaseCase  # noqa: E402

from bot import handlers_nav  # noqa: E402

BOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "bot"

# Kinds recorded by remember_retry() across the bot.
RECORDED_RE = re.compile(r'remember_retry\(\s*user_id\s*,\s*"([^"]+)"')
# Kinds _reopen_prompt() knows how to rebuild.
REOPENED_RE = re.compile(r'if\s+kind\s*==\s*"([^"]+)"')


def _recorded_kinds():
    found = set()
    for path in BOT_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        found.update(RECORDED_RE.findall(text))
    return found


def _reopenable_kinds():
    source = (BOT_DIR / "handlers_nav.py").read_text(encoding="utf-8",
                                                     errors="ignore")
    start = source.index("async def _reopen_prompt")
    end = source.index("async def _render_faq", start)
    return set(REOPENED_RE.findall(source[start:end]))


class _Msg:
    def __init__(self, log):
        self.log = log

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.log.append(text)
        return self

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.log.append(text)
        return self

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.log.append(text)
        return self


class _Query:
    def __init__(self, log):
        self.log = log
        self.data = "act:retry"
        self.message = _Msg(log)
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append(text)


class RetryKindTests(DatabaseCase, unittest.IsolatedAsyncioTestCase):

    def test_every_recorded_kind_can_be_reopened(self):
        recorded = _recorded_kinds()
        self.assertTrue(recorded, "no remember_retry() calls found at all")
        missing = sorted(recorded - _reopenable_kinds())
        self.assertEqual(
            [], missing,
            "These are recorded by remember_retry() but _reopen_prompt() "
            "cannot rebuild them, so \"Try again\" does nothing on the "
            "screens that use them: " + ", ".join(missing))

    async def test_retry_reopens_the_prompt_and_sets_state(self):
        from bot import error_actions, handlers_projects
        from bot.handlers_nav import _reopen_prompt

        error_actions.remember_retry(1, "source", pid=10)
        log = []
        handled = await _reopen_prompt(_Query(log), 1,
                                       error_actions.take_retry(1))
        self.assertTrue(handled)
        self.assertTrue(log, "Try again produced no prompt")
        self.assertTrue(handlers_projects.WAITING_SOURCE.get(1),
                        "Try again did not restore the pending state")
        self.assertEqual(10, handlers_projects.CURRENT_PROJECT.get(1))
        handlers_projects.WAITING_SOURCE.pop(1, None)
        handlers_projects.CURRENT_PROJECT.pop(1, None)

    async def test_retry_with_nothing_remembered_says_so(self):
        """Press 🔁 Try again with no retry pending - the real user path.

        This happens with an old failure screen still on screen after the
        state was consumed. It must explain itself, not crash.
        """
        from bot import handlers

        query = _Query([])
        handled = await handlers.dispatch_callback(
            query, 1, "act", ["act", "retry"], None)
        self.assertTrue(handled)
        self.assertTrue(query.answers,
                        "the user pressed Try again and got no explanation")
