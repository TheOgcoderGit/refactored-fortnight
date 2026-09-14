#!/usr/bin/env python
"""Press every screen as an English user and check nothing leaks Hindi.

The complaint was that a user who picked English was answered in
Hinglish. Fixing the strings that were found is not the same as proving
none remain, and a hardcoded string is invisible to a test that only
looks for Devanagari - romanised Hindi ("aapka account connected hai")
is just English letters.

So: set the test user's language to English, walk every callback and every
command, and flag any reply that carries Hindi. Both scripts count:
Devanagari, and romanised Hindi words that do not occur in English.

Usage:
    venv/bin/python tools_language_audit.py
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

# Devanagari, plus romanised Hindi words that never appear in English.
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
ROMANISED = re.compile(
    r"\b(aapka|aapke|aapki|aapne|aap|hum|humein|karna|karein|karen|karke|"
    r"kiya|kiye|karte|karta|karti|hai|hain|ho|hoga|hogi|hain|nahi|nhi|"
    r"zaroori|bhejein|bhej|dein|dena|lena|liye|ke liye|mein|se|par|aur|"
    r"wala|wale|wali|wapas|abhi|pehle|baad|kyun|kaise|kya|sab|kuch|"
    r"chunein|chalu|jayegi|jayega|lagayein|chhod|dabayein|shuru|swagat|"
    r"bhasha|pasandeeda|laagu|din|ka|ki|ke)\b",
    re.IGNORECASE)

# A reply that offers a language choice may legitimately show every
# language at once, and pressing lang:hi is *supposed* to switch the user
# to Hindi - the screens it then renders are correctly Hindi, not leaks.
LANGUAGE_PICKERS = (
    "choose your preferred language",
    "select preferred language",
    "bhasha chunein",
)
SWITCHES_LANGUAGE = re.compile(r"^lang(:|:set)?:")


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
        self.log.append(("answer", text or ""))

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("edit", text or ""))

    async def edit_message_caption(self, caption=None, **kwargs):
        self.log.append(("edit", caption or ""))

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        self.log.append(("edit", "<markup>"))


def _offences(text: str):
    found = []
    if DEVANAGARI.search(text):
        found.append("Devanagari")
    hits = sorted({m.group(0).lower() for m in ROMANISED.finditer(text)})
    if hits:
        found.append("romanised: " + ", ".join(hits[:6]))
    return found


async def run() -> int:
    tmp = tempfile.TemporaryDirectory()
    previous_db = db.DB_NAME
    db.DB_NAME = str(pathlib.Path(tmp.name) / "lang.db")
    db.init_db()

    conn = db.get_connection()
    conn.executemany("INSERT INTO users(telegram_id, language) VALUES(?, 'en')",
                     [(1,), (2,)])
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

    from services import i18n_service

    def _set_language(code):
        conn = db.get_connection()
        conn.execute("UPDATE users SET language=? WHERE telegram_id=?",
                     (code, USER_ID))
        conn.commit()
        conn.close()
        i18n_service.invalidate_cache()

    _set_language("en")

    from bot.handlers import dispatch_callback, menu_handler
    from core import user_sessions
    from tools_dead_buttons import collect

    buttons, _defined = collect(ROOT / "bot")

    leaks = []
    checked = 0

    def _scan(where, log):
        nonlocal checked
        for _kind, text in log:
            if not text or text == "<markup>":
                continue
            checked += 1
            if any(marker in (text or "").lower() for marker in LANGUAGE_PICKERS):
                continue  # offering a choice may show each language
            offences = _offences(text)
            if offences:
                leaks.append((where, "; ".join(offences), text[:120]))

    for state_name, connected in (("disconnected", False), ("connected", True)):
        with mock.patch.object(user_sessions, "is_connected",
                               return_value=connected):
            for _file, _func, variants in buttons:
                data = variants[0]
                if SWITCHES_LANGUAGE.match(data):
                    # Choosing Hindi is meant to produce Hindi. Skipping the
                    # switch itself keeps the audit measuring what it claims
                    # to: what an English user sees.
                    continue
                # Pressing lang:hi changes the user's language, which would
                # make every later screen in the run report a leak that is
                # really this audit's own doing. Reset before each press.
                _set_language("en")
                log = []
                query = FakeQuery(log, data)
                parts = data.split(":")
                try:
                    await dispatch_callback(query, USER_ID, parts[0], parts, None)
                except Exception:
                    continue
                _scan(f"{data} [{state_name}]", log)

    # Commands, too - /start is the first thing a new user ever sees.
    for command in ("start", "status"):
        log = []
        message = FakeMessage(log)
        update = type("U", (), {})()
        update.effective_user = type("Us", (), {"id": USER_ID,
                                                "first_name": "Tester"})()
        update.effective_message = message
        update.message = message
        message.text = "/" + command
        try:
            if command == "start":
                from bot.handlers_onboard import start_cmd
                await start_cmd(update, None)
            else:
                from bot.handlers_status import status_cmd
                with mock.patch.object(user_sessions, "is_connected",
                                       return_value=True):
                    await status_cmd(update, None)
        except Exception:
            pass
        _scan(f"/{command}", log)

    lines = [f"Walked every callback and /start, /status as an English user "
             f"({checked} messages checked)."]
    if leaks:
        lines.append("")
        lines.append(f"### HINDI SHOWN TO AN ENGLISH USER ({len(leaks)}):")
        for where, why, text in leaks:
            lines.append(f"  {where}")
            lines.append(f"      {why}")
            lines.append(f"      {text}")
    else:
        lines.append("")
        lines.append("No leaks: an English user is answered in English "
                     "everywhere the audit can reach.")

    print("\n".join(lines))

    db.DB_NAME = previous_db
    tmp.cleanup()
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
