# ChannelFlow AI - Advanced Affiliate Link Replacer Service
# =========================================================

import re
import logging
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from database.db import get_connection

logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

AMAZON_DOMAINS = ("amazon.in", "amazon.com", "amzn.to", "amzn.in", "amzn.eu")
FLIPKART_DOMAINS = ("flipkart.com", "fkrt.it", "dl.flipkart.com")
MEESHO_DOMAINS = ("meesho.com",)
WISHLINK_DOMAINS = ("wishlink.com", "wish.link")
EARNKARO_DOMAINS = ("earnkaro.com", "earnkaro.app.link")


def ensure_affiliate_settings(project_id: int) -> dict:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM project_affiliate_settings WHERE project_id=?", (project_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO project_affiliate_settings(project_id) VALUES(?)", (project_id,))
        conn.commit()
        cur.execute("SELECT * FROM project_affiliate_settings WHERE project_id=?", (project_id,))
        row = cur.fetchone()
    conn.close()

    # FIX: Convert sqlite3.Row to standard dict to prevent .get() crashes
    r = dict(row) if row else {}

    return {
        "enabled": bool(r.get("enabled", 0)),
        "amazon_enabled": bool(r.get("amazon_enabled", 0)),
        "flipkart_enabled": bool(r.get("flipkart_enabled", 0)),
        "meesho_enabled": bool(r.get("meesho_enabled", 0)),
        "wishlink_enabled": bool(r.get("wishlink_enabled", 0)),
        "earnkaro_enabled": bool(r.get("earnkaro_enabled", 0)),
        "amazon_associate_tag": r.get("amazon_associate_tag") or "",
        "flipkart_publisher_id": r.get("flipkart_publisher_id") or "",
        "meesho_partner_id": r.get("meesho_partner_id") or "",
        "wishlink_partner_id": r.get("wishlink_partner_id") or "",
        "earnkaro_publisher_id": r.get("earnkaro_publisher_id") or "",
    }


def update_affiliate_settings(project_id: int, **fields):
    ensure_affiliate_settings(project_id)
    conn = get_connection()
    cur = conn.cursor()
    cols = [f"{k}=?" for k in fields]
    vals = list(fields.values()) + [project_id]
    cur.execute(f"UPDATE project_affiliate_settings SET {', '.join(cols)} WHERE project_id=?", vals)
    conn.commit()
    conn.close()


def _transform_single_url(url: str, settings: dict) -> str:
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]

        qs = parse_qs(parsed.query, keep_blank_values=True)

        if any(d in netloc for d in AMAZON_DOMAINS) and settings.get("amazon_associate_tag"):
            qs["tag"] = [settings["amazon_associate_tag"]]
            return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

        if any(d in netloc for d in FLIPKART_DOMAINS) and settings.get("flipkart_publisher_id"):
            qs["affid"] = [settings["flipkart_publisher_id"]]
            return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

        if any(d in netloc for d in MEESHO_DOMAINS) and settings.get("meesho_partner_id"):
            qs["partner_id"] = [settings["meesho_partner_id"]]
            return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

        if any(d in netloc for d in WISHLINK_DOMAINS) and settings.get("wishlink_partner_id"):
            qs["aff_id"] = [settings["wishlink_partner_id"]]
            return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

        if any(d in netloc for d in EARNKARO_DOMAINS) and settings.get("earnkaro_publisher_id"):
            qs["r"] = [settings["earnkaro_publisher_id"]]
            return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

    except Exception:
        pass
    return url


def replace_affiliate_links(text: str, project_id: int, user_id: int = None) -> str:
    if not text:
        return text
    try:
        settings = ensure_affiliate_settings(project_id)
        if not settings.get("enabled"):
            return text

        def _sub_match(match):
            orig = match.group(0)
            return _transform_single_url(orig, settings)

        return URL_REGEX.sub(_sub_match, text)
    except Exception as e:
        logger.warning("Affiliate replacement error: %s", e)
        return text