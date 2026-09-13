#!/usr/bin/env python
"""Press every callback_data the UI can produce, through the real router.

The earlier AST audit only modelled two path segments and produced a lot of
noise. This tool is the real thing: it collects every ``callback_data``
literal in ``bot/``, turns the f-string placeholders into concrete values,
and presses each one through exactly the handler chain ``main.py`` uses.

A button is DEAD when the router neither handles it nor renders anything -
which is precisely the "dabao to kuch nahi hota" bug.

Usage:
    venv/bin/python tools_dead_buttons.py [path/to/bot]

Exit code is 1 when dead buttons remain.
"""

import asyncio
import os
import itertools
import re
import sys
import tempfile
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

from database import db  # noqa: E402

USER_ID = 1
PROJECT_ID = 10
DEST_ID = 100
SOURCE_ID = 1

CALLBACK_RE = re.compile(r"callback_data\s*=\s*(f?)(['\"])(.*?)\2", re.DOTALL)
# Inline conditionals inside an f-string, e.g. {'a' if flag else 'b'}.
COND_RE = re.compile(r"\{('[^']*')\s+if\s+[^}]*?\s+else\s+('[^']*')\}")


def _code_only(source: str) -> str:
    """Source with comments and string literals removed.

    A name mentioned only in a comment or a docstring is not a call. Without
    this, platform_selection_keyboard looked wired in because a comment in
    services/platform_registry.py mentions it.
    """
    import io
    import tokenize

    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    return " ".join(out)


# ----------------------------------------------------------------------
# collecting the buttons
# ----------------------------------------------------------------------

# Placeholders we cannot infer get tried against several plausible values.
# A button is only reported dead when none of the variants is handled.
_LAST_REPORT = ''

GENERIC_CANDIDATES = ("1", "10", "amazon", "upi", "CREATOR", "text", "0")


def _substitute(expr: str) -> str:
    """Pick a plausible concrete value for an f-string placeholder."""
    e = expr.strip().lower()
    if any(k in e for k in ("project_id", "proj_id", "pid", "project")):
        return str(PROJECT_ID)
    if "dest" in e:
        return str(DEST_ID)
    if "source" in e:
        return str(SOURCE_ID)
    if "page" in e or "offset" in e or "index" in e:
        return "0"
    if "limit" in e:
        return "10"
    if "amount" in e:
        return "500"
    if any(k in e for k in ("ticket", "req", "coupon", "admin", "user")) and "id" in e:
        return "1"
    if e.endswith("_id") or e == "id":
        return "1"
    # Unknown (loop variables, model keys, method names...): mark it so the
    # caller can generate one variant per candidate value.
    return "\x00"


def collect(root: pathlib.Path):
    """Return ([(file, enclosing_function, variants)], {file: {func_names}})."""
    found = {}
    defined = {}          # file -> {func_name: used_elsewhere?}
    for path in sorted(root.rglob("*.py")):
        # Orphan namespace dirs that shadow bot/handlers.py & bot/keyboards.py
        if path.parent.name in ("handlers", "keyboards") and path.parent.parent.name == "bot":
            continue
        if "venv" in path.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        rel = str(path.relative_to(root))
        lines = source.split("\n")
        # Enclosing function for a given line, so a button can be marked
        # unreachable when its builder is never called anywhere.
        owner = {}
        current = None
        for num, text in enumerate(lines, 1):
            m = re.match(r"def (\w+)", text)
            if m:
                current = m.group(1)
                defined.setdefault(rel, set()).add(current)
            owner[num] = current

        for is_f, _quote, body in CALLBACK_RE.findall(source):
            hit = source.find(body)
            line_no = source[:hit].count("\n") + 1 if hit >= 0 else 0
            func = owner.get(line_no)
            if is_f:
                # Expand inline conditionals into one literal per branch
                # before touching the ordinary {placeholder} substitutions.
                branches = []
                for _n in range(4):
                    match = COND_RE.search(body)
                    if not match:
                        break
                    body = COND_RE.sub("\x01", body, count=1)
                    branches.append([m.strip("'") for m in match.groups()])
                if branches:
                    bodies = []
                    for combo in itertools.product(*branches):
                        filled = body
                        for value in combo:
                            filled = filled.replace("\x01", value, 1)
                        bodies.append(filled)
                else:
                    bodies = [body]
            else:
                bodies = [body]
            for body in bodies:
                if is_f:
                    body = re.sub(r"\{([^{}]+)\}", lambda m: _substitute(m.group(1)), body)
                if "{" in body:        # nested/unsupported - skip
                    continue
                if not body.strip() or body.startswith("http"):
                    continue
                if "\x00" in body:
                    variants = [body.replace("\x00", c) for c in GENERIC_CANDIDATES]
                else:
                    variants = [body]
                found.setdefault((rel, func, body), variants)
    return (sorted((rel, func, variants) for (rel, func, _b), variants in found.items()),
            defined)


# ----------------------------------------------------------------------
# pressing them
# ----------------------------------------------------------------------

class FakeMessage:
    def __init__(self, log):
        self.log = log

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("reply", text))
        return self

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.log.append(("edit", text))
        return self

    async def delete(self):
        self.log.append(("delete", ""))


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
        self.log.append(("edit", text))

    async def edit_message_caption(self, caption=None, **kwargs):
        self.log.append(("edit", caption or ""))

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        self.log.append(("edit", "<markup>"))


async def press(data: str, router):
    """Run one callback through the same chain main.py uses."""
    log = []
    query = FakeQuery(log, data)
    parts = data.split(":")
    try:
        handled = await router(query, USER_ID, parts[0], parts, None)
    except Exception as exc:                      # noqa: BLE001
        return False, f"RAISED {type(exc).__name__}: {exc}", log
    if handled:
        return True, "", log
    if log:
        return True, "(rendered without claiming)", log
    return False, "", log


def build_router():
    """Use the bot's real dispatch table, not a re-implementation of it."""
    from bot.handlers import dispatch_callback

    async def router(query, user_id, action, parts, context):
        return await dispatch_callback(query, user_id, action, parts, context)

    return router


async def run(target: pathlib.Path, quiet: bool = False):
    tmp = tempfile.TemporaryDirectory()
    previous_db = db.DB_NAME
    db.DB_NAME = str(pathlib.Path(tmp.name) / "sweep.db")
    db.init_db()

    conn = db.get_connection()
    conn.executemany("INSERT INTO users(telegram_id) VALUES(?)", [(1,), (2,)])
    conn.executemany(
        "INSERT INTO projects(id,user_id,name,status) VALUES(?,?,?,1)",
        [(10, 1, "A"), (11, 1, "B"), (20, 2, "C")])
    conn.executemany(
        "INSERT INTO destinations(id,project_id,chat_id) VALUES(?,?,?)",
        [(100, 10, "-10001"), (101, 10, "-10002")])
    conn.executemany(
        "INSERT INTO sources(id,project_id,chat_id) VALUES(?,?,?)",
        [(1, 10, "-90001")])
    conn.commit()
    conn.close()

    router = build_router()
    buttons, defined = collect(target)

    # Which builder functions are never referenced outside their own file?
    # A builder is reachable only if its name is referenced from a file that
    # does not itself define that name. Comparing against every file would
    # make it look used inside its own module, and a same-named helper in
    # another module (bot/handlers_nav.py defines its own
    # platform_accounts_keyboard) would mask the real answer.
    definers = {}
    for rel, funcs in defined.items():
        for func in funcs:
            definers.setdefault(func, set()).add(rel)

    references = {}
    for path in sorted((target.parent).rglob("*.py")):
        if "venv" in path.parts:
            continue
        try:
            text = _code_only(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        try:
            rel = str(path.resolve().relative_to(target.resolve()))
        except (ValueError, OSError):
            rel = path.name
        for func in definers:
            if re.search(r"\b" + re.escape(func) + r"\b", text):
                references.setdefault(func, set()).add(rel)

    unused = {
        func
        for func, where in definers.items()
        if not (references.get(func, set()) - where)
    }

    dead, unreachable, raised, ok = [], [], [], 0
    for _file, func, variants in buttons:
        first = variants[0]
        crashed = None
        handled = False
        for index, data in enumerate(variants):
            ok_now, note, _log = await press(data, router)
            if ok_now:
                handled = True
                break
            # Only the most plausible value counts as a real crash; the
            # synthetic ones just probe whether the action exists at all.
            if note.startswith("RAISED") and index == 0:
                crashed = note
                break
        if handled:
            ok += 1
        elif crashed:
            raised.append((first, _file, crashed))
        elif func and func in unused:
            unreachable.append((first, _file, func))
        else:
            dead.append((first, _file))

    lines = []
    lines.append(f"Pressed {len(buttons)} distinct callback_data values "
                 f"as user {USER_ID} on project {PROJECT_ID}")

    if raised:
        lines.append("")
        lines.append(f"### CRASHED on press ({len(raised)}) - breaks at runtime:")
        for data, _file, note in raised:
            lines.append(f"  {data:<44} {note[:90]}  [{_file}]")

    if dead:
        lines.append("")
        lines.append(f"### DEAD - reachable but nothing handles them ({len(dead)}):")
        for data, _file in dead:
            lines.append(f"  {data:<44} [{_file}]")

    if unreachable:
        lines.append("")
        lines.append(f"### UNREACHABLE - built by keyboard builders that are "
                     f"never called ({len(unreachable)}):")
        for data, _file, func in unreachable:
            lines.append(f"  {data:<44} {func}()  [{_file}]")
        lines.append("  These cannot be pressed by a user; they are dead "
                     "code, not dead buttons.")

    lines.append("")
    lines.append(f"OK: {ok}   DEAD: {len(dead)}   UNREACHABLE: "
                 f"{len(unreachable)}   CRASHED: {len(raised)}")

    global _LAST_REPORT
    _LAST_REPORT = "\n".join(lines)
    if not quiet:
        print(_LAST_REPORT)

    db.DB_NAME = previous_db
    tmp.cleanup()
    return 1 if (dead or raised) else 0


if __name__ == "__main__":
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "bot"
    sys.exit(asyncio.run(run(target)))


def last_report() -> str:
    """Human-readable output of the most recent run()."""
    return _LAST_REPORT
