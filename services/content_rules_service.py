"""
ChannelFlow AI - Content Rules Service
==========================================

Additional "should this message be forwarded at all" gates, layered on
top of the filters that already exist in project_settings (media type,
keyword whitelist/blacklist, regex). Kept as a separate table/service
rather than folded into settings_service so that project - a table
that's already wide - doesn't grow further, but core/forwarder.py
checks both together as one filter chain (see _passes_all_filters).

Every check here is a pure function of (text, urls, sender_id, rules
row) -> bool, so it's fully unit-testable without a running bot.
"""

from database.db import get_connection

VALID_FILTER_LOGIC = ("all", "any")

DEFAULTS = {
    "required_keywords": "",
    "filter_logic": "any",
    "hashtag_filter": "",
    "domain_whitelist": "",
    "domain_blacklist": "",
    "min_length": None,
    "max_length": None,
    "sender_whitelist": "",
    "sender_blacklist": "",
}


def get_rules(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM content_rules WHERE project_id=?", (project_id,))
    row = cur.fetchone()

    if row is None:

        cur.execute("INSERT INTO content_rules(project_id) VALUES(?)", (project_id,))
        conn.commit()

        cur.execute("SELECT * FROM content_rules WHERE project_id=?", (project_id,))
        row = cur.fetchone()

    conn.close()

    return row


def update_rules(project_id, **fields):

    if not fields:
        return

    get_rules(project_id)

    allowed = set(DEFAULTS.keys())
    columns = []
    values = []

    for key, value in fields.items():

        if key not in allowed:
            raise ValueError(f"Unknown content rule: {key}")

        if key == "filter_logic" and value not in VALID_FILTER_LOGIC:
            raise ValueError(f"Invalid filter_logic: {value}")

        columns.append(f"{key}=?")
        values.append(value)

    values.append(project_id)

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        f"UPDATE content_rules SET {', '.join(columns)} WHERE project_id=?",
        values,
    )

    conn.commit()
    conn.close()


def clear_rules(project_id):
    update_rules(project_id, **DEFAULTS)


def _csv_list(value):
    return [v.strip().lower() for v in (value or "").split(",") if v.strip()]


def _extract_hashtags(text):
    return {word.lower() for word in text.split() if word.startswith("#")}


def _extract_domains(urls):

    domains = set()

    for url in urls:

        # Lightweight, dependency-free host extraction - good enough
        # for the http(s)://host/... shape every source URL takes.
        rest = url.split("://", 1)[-1]
        host = rest.split("/", 1)[0].split("?", 1)[0].lower()
        host = host.split("@")[-1]  # strip any userinfo@ prefix
        host = host.split(":")[0]   # strip port

        if host.startswith("www."):
            host = host[4:]

        if host:
            domains.add(host)

    return domains


def passes_content_rules(text, urls, sender_id, rules_row) -> bool:
    """Pure predicate - True means "allowed to proceed", same contract
    as core/forwarder.py's existing _passes_* filter functions."""

    text = text or ""
    text_lower = text.lower()

    required = _csv_list(rules_row["required_keywords"])

    if required:
        matches = [kw in text_lower for kw in required]
        if rules_row["filter_logic"] == "all":
            if not all(matches):
                return False
        else:
            if not any(matches):
                return False

    hashtag_required = _csv_list(rules_row["hashtag_filter"])

    if hashtag_required:
        present = _extract_hashtags(text_lower)
        if not any(tag in present for tag in hashtag_required):
            return False

    domain_wl = _csv_list(rules_row["domain_whitelist"])
    domain_bl = _csv_list(rules_row["domain_blacklist"])

    if domain_wl or domain_bl:

        domains = _extract_domains(urls or [])

        if domain_wl and not any(d in domain_wl for d in domains):
            return False

        if domain_bl and any(d in domain_bl for d in domains):
            return False

    min_length = rules_row["min_length"]
    max_length = rules_row["max_length"]

    if min_length is not None and len(text) < min_length:
        return False

    if max_length is not None and len(text) > max_length:
        return False

    sender_wl = _csv_list(rules_row["sender_whitelist"])
    sender_bl = _csv_list(rules_row["sender_blacklist"])

    if (sender_wl or sender_bl) and sender_id is not None:

        sender_str = str(sender_id)

        if sender_wl and sender_str not in sender_wl:
            return False

        if sender_bl and sender_str in sender_bl:
            return False

    return True
