#!/usr/bin/env python
"""Find prompts that ask the user to type something but remember nothing.

The bug this looks for: the bot says "📝 Send a name for your project",
the user types a name, and nothing happens - because the screen never
recorded that it was waiting for one. The prompt is an orphan: it asks,
but no branch is listening for the answer.

From the user's side this is indistinguishable from "the feature is
broken". It is also invisible to the dead-button sweep, which only checks
that a button *renders* - a prompt renders beautifully and then drops the
reply on the floor.

So: press every callback, look at what it said, and when it asked for
input check that some pending store remembers the user. Anything that
asks without remembering is reported.

Usage:
    venv/bin/python tools_orphan_prompts.py
"""

import asyncio
import os
import pathlib
import re
import sys
import tempfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

from database import db  # noqa: E402

USER_ID = 1

# A prompt is an instruction, not a mention. "Send the username" asks for
# input; "we send the posts to your target" does not. So only a line that
# *opens* with an imperative verb counts - emoji, markdown and bullets
# stripped first.
_LEAD_RE = re.compile(r"^[\s\W_]*"
                      r"(send|enter|type|reply with|paste|provide|bhejein|bhej|darj)"
                      r"\b", re.IGNORECASE)

# Lines that open with a verb but are not asking the user for anything:
# they explain a button, or mention a command.
_NOT_A_PROMPT = re.compile(
    r"(send /|/connect|send it in chat|send your phone number with country"
    r"|send the code|press|tap|use the)",
    re.IGNORECASE)

# A settings screen lists its fields as "• Type: text" / "• Size: 24px".
# Those open with a verb but describe state, they do not ask for input.
_FIELD_LABEL_RE = re.compile(r"^[\s\W_]*\w+\s*:", re.IGNORECASE)



class FakeMessage:
    def __init__(self, log):
        self.log = log

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("reply", text or ""))
        return self

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("edit", text or ""))
        return self


class FakeQuery:
    def __init__(self, log, data=""):
        self.log = log
        self.data = data
        self.message = FakeMessage(log)
        self.from_user = type("U", (), {"id": USER_ID, "first_name": "Tester"})()
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("edit", text or ""))

    async def edit_message_caption(self, caption=None, **kwargs):
        self.log.append(("edit", caption or ""))

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        self.log.append(("edit", "<markup>"))


def _pending_for(user_id) -> list:
    """Names of the pending stores that currently hold this user."""
    from bot import error_actions
    holding = []
    for store in error_actions._pending_stores():
        try:
            if user_id in store:
                holding.append(store)
        except TypeError:
            continue
    return holding


def _clear_pending(user_id) -> None:
    from bot import error_actions
    for store in error_actions._pending_stores():
        try:
            store.pop(user_id, None)
        except TypeError:
            continue


def _looks_like_prompt(text: str) -> bool:
    """True when the bot asked the user to type something."""
    for line in (text or "").splitlines():
        if not _LEAD_RE.match(line):
            continue
        if _NOT_A_PROMPT.search(line):
            continue
        if _FIELD_LABEL_RE.match(line):
            continue  # "• Type: text" - a field readout, not a request
        return True
    return False


async def run() -> int:
    tmp = tempfile.TemporaryDirectory()
    previous_db = db.DB_NAME
    db.DB_NAME = str(pathlib.Path(tmp.name) / "prompts.db")
    db.init_db()

    conn = db.get_connection()
    conn.executemany("INSERT INTO users(telegram_id) VALUES(?)", [(1,), (2,)])
    conn.executemany(
        "INSERT INTO projects(id,user_id,name,status) VALUES(?,?,?,1)",
        [(10, 1, "A"), (11, 1, "B")])
    conn.executemany(
        "INSERT INTO destinations(id,project_id,chat_id) VALUES(?,?,?)",
        [(100, 10, "-10001")])
    conn.executemany(
        "INSERT INTO sources(id,project_id,chat_id) VALUES(?,?,?)",
        [(1, 10, "-90001")])
    conn.commit()
    conn.close()

    from bot.handlers import dispatch_callback
    from core import user_sessions
    from tools_dead_buttons import collect

    buttons, _defined = collect(ROOT / "bot")

    orphans = []
    checked = 0

    for state_name, connected in (("disconnected", False), ("connected", True)):
        with mock.patch.object(user_sessions, "is_connected",
                               return_value=connected):
            for _file, _func, variants in buttons:
                data = variants[0]
                _clear_pending(USER_ID)
                log = []
                query = FakeQuery(log, data)
                parts = data.split(":")
                try:
                    await dispatch_callback(query, USER_ID, parts[0], parts, None)
                except Exception:
                    continue

                # One message at a time: joining them would glue a prompt
                # onto the end of an unrelated paragraph and hide which
                # message actually asked.
                asked = None
                for _kind, text in log:
                    if _looks_like_prompt(text):
                        asked = text
                        break
                if asked is None:
                    continue

                checked += 1
                if not _pending_for(USER_ID):
                    orphans.append((data, state_name, asked[:130], _file))
                _clear_pending(USER_ID)

    lines = [f"Checked {checked} callbacks whose reply asks the user to type "
             f"something, in both connection states."]
    if orphans:
        lines.append("")
        lines.append(f"### ORPHAN PROMPTS ({len(orphans)}) - ask for input but "
                     f"record nothing, so the answer is dropped:")
        for data, state, said, _file in orphans:
            lines.append(f"  {data:<40} [{state}]")
            lines.append(f"      {said}")
    else:
        lines.append("")
        lines.append("No orphan prompts: every one records what it waits for.")

    report = "\n".join(lines)
    print(report)

    db.DB_NAME = previous_db
    tmp.cleanup()
    return 1 if orphans else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
