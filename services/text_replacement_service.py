"""
ChannelFlow AI - Text Replacement Engine

Provides deterministic find/replace rules for content transformation.
Pure text-based, no AI involved. Fast and cheap.

Per-project configuration: enable/disable, multiple find/replace rules.
Safe handling of empty rules - project text is returned unchanged.

Rules are stored as a list of {"find": ..., "replace": ...} dicts in
the formatting_rules table (shared with formatting rules) to avoid
duplicate table creation. This engine specifically handles the
find/replace subset of formatting rules.
"""

import json
import logging

from database.db import get_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-project text replacement settings
# We reuse the formatting_rules table since it already stores
# replace_rules as JSON. This engine specifically operates on the
# replace_rules list. No new table needed.
# ---------------------------------------------------------------------------

DEFAULTS = {
    "prefix": "",
    "suffix": "",
    "remove_patterns": "[]",
    "replace_rules": "[]",
}


def get_replace_rules(project_id: int) -> list:
    """Return the list of replace rules for a project (JSON list of
    {"find": str, "replace": str} dicts). Lazily creates a default row
    if one does not exist yet."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM formatting_rules WHERE project_id=?", (project_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute("INSERT INTO formatting_rules(project_id) VALUES(?)", (project_id,))
        conn.commit()
        cur.execute("SELECT * FROM formatting_rules WHERE project_id=?", (project_id,))
        row = cur.fetchone()

    conn.close()

    try:
        return json.loads(row["replace_rules"] or "[]")
    except (TypeError, ValueError):
        return []


def add_replace_rule(project_id: int, find: str, replace: str = "") -> None:
    """Add a single find/replace rule for a project.

    Raises ValueError if *find* is empty."""
    if not find:
        raise ValueError("find text cannot be empty")

    rules = get_replace_rules(project_id)
    rules.append({"find": find, "replace": replace or ""})
    _update_replace_rules(project_id, rules)


def remove_replace_rule(project_id: int, index: int) -> None:
    """Remove a replace rule at the given zero-based index."""

    rules = get_replace_rules(project_id)
    if 0 <= index < len(rules):
        rules.pop(index)
        _update_replace_rules(project_id, rules)


def clear_replace_rules(project_id: int) -> None:
    """Remove all replace rules for a project."""

    _update_replace_rules(project_id, [])


def _update_replace_rules(project_id: int, rules: list) -> None:
    """Persist the replace-rules list back to the formatting_rules row."""

    conn = get_connection()
    cur = conn.cursor()

    # Ensure the row exists
    cur.execute("SELECT * FROM formatting_rules WHERE project_id=?", (project_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute("INSERT INTO formatting_rules(project_id) VALUES(?)", (project_id,))
        conn.commit()

    cur.execute(
        "UPDATE formatting_rules SET replace_rules=? WHERE project_id=?",
        (json.dumps(rules), project_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Core replacement logic
# ---------------------------------------------------------------------------

def apply_text_replacement(text: str, project_id: int) -> str:
    """Apply all deterministic find/replace rules to *text* for the
    given project.

    Rules are applied in order. If a rule's *find* string appears in
    the text, it is replaced with the *replace* string. Rules that
    don't match are skipped silently.

    Empty or missing rule lists leave the text unchanged.

    Returns the transformed text (or the original if no rules matched
    or the feature is disabled).
    """

    if not text or not text.strip():
        return text

    rules = get_replace_rules(project_id)
    if not rules:
        return text

    result = text
    for rule in rules:
        find = rule.get("find", "") or ""
        replace = rule.get("replace", "") or ""
        if find:
            result = result.replace(find, replace)

    return result


# ---------------------------------------------------------------------------
# Public API - convenience wrappers
# ---------------------------------------------------------------------------

def set_prefix(project_id: int, prefix: str) -> None:
    """Set a text prefix for a project (applied before replacement)."""

    from services import formatting_service
    formatting_service.set_prefix(project_id, prefix)


def set_suffix(project_id: int, suffix: str) -> None:
    """Set a text suffix for a project (applied after replacement)."""

    from services import formatting_service
    formatting_service.set_suffix(project_id, suffix)