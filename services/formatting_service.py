"""
ChannelFlow AI - Formatting Service
======================================

ONE shared prefix/suffix/remove/replace engine, used by:
    * core/forwarder.py, in Telegram copy mode only (never in forward
      mode - Telegram's native forward can't have its content edited,
      and the spec requires preserving native forward behaviour rather
      than faking a transformation that didn't really happen).
    * services/instagram_service.format_for_instagram(), which calls
      apply_formatting() for the base prefix/suffix/replace/remove step
      and then layers its own Instagram-specific CTA/hashtags/link
      placement on top.

This used to be two separate implementations (a Telegram-side one and
an Instagram-side one with its own prefix/suffix). Consolidated into
this module so a formatting rule is defined once per project and
applies everywhere, per the "don't duplicate the same feature across
routes" principle - see services/instagram_service.py's module
docstring for the specific dedup this replaced.

URL safety: replace_rules and remove_patterns never touch anything
that looks like a URL. This is a hard rule, not a suggestion - link
rules / affiliate URLs must never be silently mangled by an unrelated
text-replace rule (see destinations/instagram_destination.py's
docstring on the same constraint from the other direction).
"""

import json
import re

from database.db import get_connection

URL_RE = re.compile(r"https?://\S+")

DEFAULTS = {
    "prefix": "",
    "suffix": "",
    "remove_patterns": "[]",
    "replace_rules": "[]",
    "advanced_json": "{}",
}


def get_rules(project_id):

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

    return row


def set_prefix(project_id, prefix):
    _update(project_id, prefix=prefix or "")


def set_suffix(project_id, suffix):
    _update(project_id, suffix=suffix or "")


def add_replace_rule(project_id, find, replace):

    if not find:
        raise ValueError("find text cannot be empty")

    rules = get_replace_rules(project_id)
    rules.append({"find": find, "replace": replace or ""})
    _update(project_id, replace_rules=json.dumps(rules))


def remove_replace_rule(project_id, index):

    rules = get_replace_rules(project_id)

    if 0 <= index < len(rules):
        rules.pop(index)
        _update(project_id, replace_rules=json.dumps(rules))


def get_replace_rules(project_id):

    row = get_rules(project_id)

    try:
        return json.loads(row["replace_rules"] or "[]")
    except (TypeError, ValueError):
        return []


def add_remove_pattern(project_id, pattern):

    if not pattern:
        raise ValueError("pattern cannot be empty")

    re.compile(pattern)  # reject invalid configuration before saving
    patterns = get_remove_patterns(project_id)
    patterns.append(pattern)
    _update(project_id, remove_patterns=json.dumps(patterns))


def remove_remove_pattern(project_id, index):

    patterns = get_remove_patterns(project_id)

    if 0 <= index < len(patterns):
        patterns.pop(index)
        _update(project_id, remove_patterns=json.dumps(patterns))


def get_remove_patterns(project_id):

    row = get_rules(project_id)

    try:
        return json.loads(row["remove_patterns"] or "[]")
    except (TypeError, ValueError):
        return []


def clear_rules(project_id):
    _update(project_id, **DEFAULTS)


def _update(project_id, **fields):

    get_rules(project_id)

    columns = [f"{k}=?" for k in fields]
    values = list(fields.values()) + [project_id]

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        f"UPDATE formatting_rules SET {', '.join(columns)} WHERE project_id=?",
        values,
    )

    conn.commit()
    conn.close()


def _protect_urls(text):
    """Swaps every URL for a short placeholder token before any
    remove/replace runs, then swaps them back afterward - so a rule
    like replace('9' -> '') can never accidentally eat a digit out of
    an affiliate link's query string."""

    urls = URL_RE.findall(text)
    protected = text

    for i, url in enumerate(urls):
        protected = protected.replace(url, f"\x00URL{i}\x00", 1)

    return protected, urls


def _restore_urls(text, urls):

    for i, url in enumerate(urls):
        text = text.replace(f"\x00URL{i}\x00", url)

    return text


ADVANCED_DEFAULTS = {
    "link_preview": True, "remove_usernames": False, "remove_links": False,
    "disable_hidden_links": False, "mono": False,
    "remove_first_words": 0, "remove_last_words": 0,
    "remove_first_lines": 0, "remove_last_lines": 0,
    "keep_first_words": None, "keep_first_lines": None,
    "header": None, "footer": None,
}
USERNAME_RE = re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,31}\b")
VISIBLE_URL_RE = re.compile(r"(?i)(?<![\w@])(?:https?://|www\.|t\.me/|telegram\.me/)[^\s<>]+")
BARE_DOMAIN_RE = re.compile(r"(?i)(?<![\w@./-])(?:[a-z0-9-]+\.)+(?:com|org|net|io|in|co|me|dev)(?:/[^\s<>]*)?(?![\w@])")

def _require_owner(conn, user_id, project_id, destination_id=None):
    row = conn.execute("SELECT user_id FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or row[0] != user_id:
        raise PermissionError("Project unavailable.")
    if destination_id is not None:
        if not conn.execute("SELECT 1 FROM destinations WHERE id=? AND project_id=?", (destination_id,project_id)).fetchone():
            raise PermissionError("Destination unavailable.")

def validate_advanced(config):
    if not isinstance(config, dict):
        raise ValueError("Settings must be a JSON object.")
    for key, value in config.items():
        if key not in ADVANCED_DEFAULTS:
            raise ValueError("Unknown setting: " + key)
        default = ADVANCED_DEFAULTS[key]
        if isinstance(default, bool):
            if type(value) is not bool:
                raise ValueError(key + " must be true or false.")
        elif key in ('header','footer'):
            if value is not None and (not isinstance(value,str) or len(value)>1024):
                raise ValueError(key + " must be text of at most 1024 characters or null.")
        else:
            if value is None and key.startswith('keep_'):
                continue
            if type(value) is not int or not 0 <= value <= 100000:
                raise ValueError(key + " must be a non-negative integer.")
    return config

def get_advanced(project_id, destination_id=None):
    row = get_rules(project_id)
    result = dict(ADVANCED_DEFAULTS)
    result.update(json.loads(row['advanced_json'] or '{}'))
    if destination_id is not None:
        conn=get_connection()
        try:
            override=conn.execute("SELECT f.config_json FROM destination_formatting f JOIN destinations d "
                "ON d.id=f.destination_id WHERE d.project_id=? AND d.id=?", (project_id,destination_id)).fetchone()
            if override:
                result.update(json.loads(override[0]))
        finally:
            conn.close()
    return result

def configure(user_id, project_id, config, destination_id=None):
    validate_advanced(config)
    conn=get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        _require_owner(conn,user_id,project_id,destination_id)
        if destination_id is None:
            conn.execute('INSERT OR IGNORE INTO formatting_rules(project_id) VALUES(?)',(project_id,))
            current=json.loads(conn.execute('SELECT advanced_json FROM formatting_rules WHERE project_id=?',(project_id,)).fetchone()[0])
            current.update(config)
            conn.execute('UPDATE formatting_rules SET advanced_json=? WHERE project_id=?',(json.dumps(current),project_id))
        else:
            row=conn.execute('SELECT config_json FROM destination_formatting WHERE destination_id=?',(destination_id,)).fetchone()
            current=json.loads(row[0]) if row else {}
            current.update(config)
            conn.execute('INSERT INTO destination_formatting(destination_id,config_json) VALUES(?,?) '
                'ON CONFLICT(destination_id) DO UPDATE SET config_json=excluded.config_json', (destination_id,json.dumps(current)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def cleanup_text(text, config):
    text=text or ''
    # Deterministic: first/last lines, keep lines, first/last words, keep words.
    for unit, split in [('lines',lambda s:s.split('\n')), ('words',lambda s:re.findall(r'\S+',s))]:
        first=config.get('remove_first_'+unit,0)
        last=config.get('remove_last_'+unit,0)
        keep=config.get('keep_first_'+unit)
        if first or last or keep is not None:
            chunks=split(text)[first:]
            if last:
                chunks=chunks[:-last] if last<len(chunks) else []
            if keep is not None:
                chunks=chunks[:keep]
            text=('\n' if unit=='lines' else ' ').join(chunks)
    if config.get('remove_usernames'):
        text=USERNAME_RE.sub('',text)
    if config.get('remove_links'):
        text=VISIBLE_URL_RE.sub('',text)
        text=BARE_DOMAIN_RE.sub('',text)
    if config.get('remove_links') or config.get('remove_usernames'):
        text='\n'.join(re.sub(r'[ \t]+',' ',line).strip() for line in text.split('\n')).strip()
    return text

def apply_formatting(project_id, text, destination_id=None) -> str:
    """Legacy replacement/affiliate → AI → cleanup → patterns → header/footer.

    Preserves legacy ordering explicitly; no URL placeholder can be corrupted
    by user find/replace rules. Formatting always composes with AI output.
    """
    rules=get_rules(project_id)
    cfg=get_advanced(project_id,destination_id)
    body=cleanup_text(text,cfg)
    patterns=json.loads(rules['remove_patterns'] or '[]')
    replacements=json.loads(rules['replace_rules'] or '[]')
    def transform(fragment):
        for pattern in patterns:
            try:
                fragment=re.sub(pattern,'',fragment)
            except re.error:
                continue  # old invalid configurations are ignored safely
        for rule in replacements:
            fragment=fragment.replace(rule['find'],rule['replace'])
        return fragment
    # Only non-URL spans reach regex/replacement logic.
    pieces=[]; offset=0
    for match in URL_RE.finditer(body):
        pieces.extend((transform(body[offset:match.start()]),match.group(0)))
        offset=match.end()
    pieces.append(transform(body[offset:]))
    body=''.join(pieces)
    header=rules['prefix'] if cfg['header'] is None else cfg['header']
    footer=rules['suffix'] if cfg['footer'] is None else cfg['footer']
    if not header and not footer:
        return body
    return '\n\n'.join(part for part in (header,body,footer) if part and part.strip())

def preview(user_id, project_id, text, destination_id=None):
    conn=get_connection()
    try:
        _require_owner(conn,user_id,project_id,destination_id)
    finally:
        conn.close()
    return apply_formatting(project_id,text,destination_id)
