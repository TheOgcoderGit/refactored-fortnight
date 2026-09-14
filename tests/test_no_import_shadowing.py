"""Guards against function-local imports shadowing module-level ones.

This bug has bitten twice and both times it took out a large slice of the
bot at once:

    def handle_callbacks(...):
        ...                              # uses InlineKeyboardButton here
        if action == "connect":
            from telegram import InlineKeyboardButton   # <-- makes the name
                                                        #     local to the WHOLE
                                                        #     function body

Python decides a name is local to a function if it is *assigned anywhere*
in that function - and an import is an assignment. So the first use, which
runs before the import, raises UnboundLocalError. Nothing warns at import
time; it only surfaces when that code path is executed.

The rule enforced here: if a name is imported at module level, do not
import it again inside a function in the same file.
"""

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BOT_DIR = ROOT / "bot"


def _module_level_names(tree: ast.Module) -> set:
    names = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _shadowed_imports(path: pathlib.Path):
    """Yields (lineno, name) for function-local imports of module-level names."""
    source = path.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(source, filename=str(path))
    module_names = _module_level_names(tree)

    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if child is node:
                continue
            if isinstance(child, ast.ImportFrom):
                aliases = child.names
            elif isinstance(child, ast.Import):
                aliases = child.names
            else:
                continue
            for alias in aliases:
                name = alias.asname or alias.name.split(".")[0]
                if name in module_names:
                    problems.append((child.lineno, name, node.name))
    return problems


class NoImportShadowingTest(unittest.TestCase):

    def test_bot_modules_do_not_reimport_module_level_names(self):
        offenders = []
        for path in sorted(BOT_DIR.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                problems = _shadowed_imports(path)
            except SyntaxError as exc:      # pragma: no cover
                self.fail(f"Could not parse {path}: {exc}")

            for lineno, name, func in problems:
                offenders.append(
                    f"{path.relative_to(ROOT)}:{lineno} "
                    f"'{name}' re-imported inside {func}()")

        self.assertEqual(
            [], offenders,
            "Function-local imports shadow the module-level binding and "
            "cause UnboundLocalError on every earlier use:\n  "
            + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
