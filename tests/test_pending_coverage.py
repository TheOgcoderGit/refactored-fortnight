"""Every pending-input dict must be reachable by /cancel.

cancel_pending() clears a hand-maintained list of module-level dicts. A
dict that is added but never listed is a trap: the prompt is shown, the
user walks away, and the bot keeps reading everything they type
afterwards as the answer to a question they abandoned. /cancel cannot
help, because it does not know the store exists.

Two were missing in production - WAITING_TEMPLATE_SOURCE (so abandoning
"➕ Add a source channel" meant every later message became a channel
handle) and WAITING_PAYMENT_SCREENSHOT (so /cancel could not get someone
out of a payment they had decided not to make).

This test finds every module-level dict in bot/ whose name marks it as
pending state and asserts cancel_pending() can clear it. Add a dict,
register it, or this fails.
"""

import ast
import os
import pathlib
import unittest

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("API_ID", "1234567")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")

from bot import error_actions  # noqa: E402

BOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "bot"

# Names that mark a module-level dict as "waiting for the user".
PENDING_PREFIXES = ("WAITING_", "PENDING_", "CURRENT_")

# PENDING_RETRY does not need registering: cancel_pending() clears it
# explicitly, because it is owned by this module rather than by a handler.
EXEMPT = {("bot.error_actions", "PENDING_RETRY")}


def _module_level_pending_dicts():
    """{(module, name) for every module-level dict that holds pending state}"""
    found = set()
    for path in sorted(BOT_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, SyntaxError):
            continue

        module = f"bot.{path.stem}"
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            # name = {} or name = dict()
            value = node.value
            is_dict = isinstance(value, ast.Dict) or (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "dict")
            if not is_dict:
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id.startswith(PENDING_PREFIXES):
                    found.add((module, target.id))
    return found - EXEMPT


class PendingCoverageTests(unittest.TestCase):

    def test_every_pending_dict_is_cancellable(self):
        stores = error_actions._pending_stores()
        by_id = {id(store): store for store in stores}

        # Resolve the live object for each module-level name.
        import importlib
        missing = []
        for module_name, attr in sorted(_module_level_pending_dicts()):
            try:
                module = importlib.import_module(module_name)
                obj = getattr(module, attr, None)
            except Exception:  # noqa: BLE001 - a broken import is not this test
                continue
            if not isinstance(obj, dict):
                continue
            if id(obj) not in by_id:
                missing.append(f"{module_name}.{attr}")

        self.assertEqual(
            [], missing,
            "These hold pending user input but cancel_pending() cannot clear "
            "them, so a half-finished flow keeps swallowing the user's "
            "messages. Add them to _pending_stores() in bot/error_actions.py:\n  "
            + "\n  ".join(missing))

    def test_cancel_pending_clears_what_it_knows(self):
        from bot import handlers_projects
        handlers_projects.WAITING_TEMPLATE_SOURCE[4242] = "deals"
        try:
            self.assertTrue(error_actions.cancel_pending(4242),
                            "cancel_pending ignored WAITING_TEMPLATE_SOURCE")
            self.assertNotIn(4242, handlers_projects.WAITING_TEMPLATE_SOURCE)
        finally:
            handlers_projects.WAITING_TEMPLATE_SOURCE.pop(4242, None)


if __name__ == "__main__":
    unittest.main()
