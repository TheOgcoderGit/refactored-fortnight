"""
ChannelFlow AI - WhatsApp Content Transformation
==================================================

Transforms Telegram content for WhatsApp Channel publishing.
WhatsApp Channel uses a simplified formatting syntax (WhatsApp Markdown):
- *bold* for bold
- _italic_ for italic
- ~strikethrough~ for strikethrough
- `monospace` for monospace
- No HTML, no MarkdownV2, no nested formatting
- Media must be sent via public URLs
- Text limit: 4096 characters
"""

import re
import html
from typing import Optional, Tuple, List
from dataclasses import dataclass

from services.formatting_service import apply_formatting


@dataclass
class WhatsAppDeliveryContent:
    """Content ready for WhatsApp delivery."""
    text: str
    media_url: Optional[str] = None
    media_type: Optional[str] = None  # "photo", "video", "document"
    media_caption: Optional[str] = None


# Regex patterns for Telegram/HTML/Markdown formatting
HTML_BOLD_RE = re.compile(r"<b>(.*?)</b>", re.DOTALL)
HTML_ITALIC_RE = re.compile(r"<i>(.*?)</i>", re.DOTALL)
HTML_CODE_RE = re.compile(r"<code>(.*?)</code>", re.DOTALL)
HTML_PRE_RE = re.compile(r"<pre>(.*?)</pre>", re.DOTALL)
HTML_A_RE = re.compile(r'<a href="([^"]+)">(.*?)</a>', re.DOTALL)

MD_BOLD_RE = re.compile(r"\*\*(.*?)\*\*", re.DOTALL)
MD_ITALIC_RE = re.compile(r"__(.*?)__", re.DOTALL)
MD_STRIKE_RE = re.compile(r"~~(.*?)~~", re.DOTALL)
MD_CODE_RE = re.compile(r"`(.*?)`", re.DOTALL)
MD_PRE_RE = re.compile(r"```(.*?)```", re.DOTALL)
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)", re.DOTALL)

TG_SPOILER_RE = re.compile(r"\|\|(.*?)\|\|", re.DOTALL)


def _html_to_whatsapp(text: str) -> str:
    """Convert HTML formatting to WhatsApp Markdown."""
    # Bold: <b>...</b> -> *...*
    text = HTML_BOLD_RE.sub(r"*\1*", text)
    # Italic: <i>...</i> -> _..._
    text = HTML_ITALIC_RE.sub(r"_\1_", text)
    # Code: <code>...</code> -> `...`
    text = HTML_CODE_RE.sub(r"`\1`", text)
    # Pre: <pre>...</pre> -> ```...```
    text = HTML_PRE_RE.sub(r"```\1```", text)
    # Links: <a href="url">text</a> -> text (url)
    text = HTML_A_RE.sub(r"\1 (\2)", text)
    return text


def _markdown_to_whatsapp(text: str) -> str:
    """Convert Markdown/MarkdownV2 formatting to WhatsApp Markdown."""
    # Bold: **...** -> *...*
    text = MD_BOLD_RE.sub(r"*\1*", text)
    # Italic: __...__ -> _..._
    text = MD_ITALIC_RE.sub(r"_\1_", text)
    # Strikethrough: ~~...~~ -> ~...~
    text = MD_STRIKE_RE.sub(r"~\1~", text)
    # Code: `...` -> `...`
    text = MD_CODE_RE.sub(r"`\1`", text)
    # Pre: ```...``` -> ```...```
    text = MD_PRE_RE.sub(r"```\1```", text)
    # Links: [text](url) -> text (url)
    text = MD_LINK_RE.sub(r"\1 (\2)", text)
    return text


def _telegram_to_whatsapp(text: str) -> str:
    """Convert Telegram-specific formatting to WhatsApp Markdown."""
    # Spoiler: ||...|| -> (removed or marked)
    text = TG_SPOILER_RE.sub(r"[spoiler: \1]", text)
    return text


def _clean_whatsapp_text(text: str) -> str:
    """Clean text for WhatsApp constraints."""
    # Remove zero-width characters
    text = text.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    # Normalize newlines
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse excessive newlines
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text


def _truncate_whatsapp_text(text: str, limit: int = 4096) -> str:
    """Truncate text to WhatsApp's 4096 character limit."""
    if len(text) <= limit:
        return text
    # Try to truncate at a sentence boundary
    truncated = text[:limit - 3]
    last_period = truncated.rfind(". ")
    last_newline = truncated.rfind("\n")
    cut_at = max(last_period, last_newline)
    if cut_at > limit * 0.7:  # Only use sentence boundary if it's not too short
        return truncated[:cut_at + 1] + "..."
    return truncated + "..."


def convert_telegram_to_whatsapp(text: str, parse_mode: Optional[str] = None) -> str:
    """
    Convert Telegram-formatted text to WhatsApp Markdown.
    
    Args:
        text: The text to convert
        parse_mode: "html", "markdown", "markdownv2", "markdownv2", or None for plain
        
    Returns:
        Text formatted for WhatsApp Markdown
    """
    if not text:
        return ""
    
    text = _clean_whatsapp_text(text)
    
    # Apply format conversions based on parse_mode
    if parse_mode == "html":
        text = _html_to_whatsapp(text)
    elif parse_mode in ("markdown", "markdownv2"):
        text = _markdown_to_whatsapp(text)
    elif parse_mode is None:
        # Assume plain text or Telegram-style markdown
        text = _markdown_to_whatsapp(text)
        text = _telegram_to_whatsapp(text)
    
    # Truncate to WhatsApp limit
    text = _truncate_whatsapp_text(text)
    
    return text


@dataclass
class WhatsAppMediaInfo:
    """Media information for WhatsApp delivery."""
    media_type: str  # "photo", "video", "document"
    url: str
    caption: Optional[str] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None


async def prepare_whatsapp_content(
    project_id: int,
    text: str,
    parse_mode: Optional[str] = None,
    media_list: Optional[List[dict]] = None,
    ai_settings: Optional[dict] = None,
    formatting_rules: Optional[dict] = None,
) -> WhatsAppDeliveryContent:
    """
    Prepare complete content for WhatsApp delivery.
    
    Args:
        project_id: Project ID for formatting rules
        text: Original text content
        parse_mode: "html", "markdown", "markdownv2", or None
        media_list: List of media dicts with keys: type, url, caption, etc.
        ai_settings: AI rewriting settings
        formatting_rules: Formatting rules from project
        
    Returns:
        WhatsAppDeliveryContent ready for delivery
    """
    # Apply AI rewriting if enabled
    if ai_settings and ai_settings.get("enabled"):
        from services.ai_service import rewrite_content
        rewrite_result = await rewrite_content(
            text,
            ai_settings,
            user_id=None,  # Will be filled by caller
            project_id=project_id,
            platform="whatsapp",
        )
        if rewrite_result.success and rewrite_result.text:
            text = rewrite_result.text
    
    # Apply project formatting rules
    if formatting_rules:
        text = apply_formatting(project_id, text)
    
    # Convert to WhatsApp format
    text = convert_telegram_to_whatsapp(text, parse_mode)
    
    # Handle media
    media_url = None
    media_type = None
    media_caption = None
    
    if media_list:
        # For WhatsApp, we can only send one media item per message
        # Take the first media item
        first_media = media_list[0] if media_list else None
        if first_media:
            media_url = first_media.get("url")
            media_type = first_media.get("type", "photo")
            # Use text as caption if media has no caption
            caption = first_media.get("caption") or text
            if caption and len(caption) > 1024:  # WhatsApp caption limit
                caption = caption[:1021] + "..."
            media_caption = caption
    
    return WhatsAppDeliveryContent(
        text=text,
        media_url=media_url,
        media_type=media_type,
        media_caption=media_caption,
    )


def extract_media_from_telegram_message(message) -> List[dict]:
    """
    Extract media info from a Telethon message object.
    
    Returns list of media dicts with keys: type, url, caption, etc.
    """
    media_list = []
    
    if message.photo:
        media_list.append({
            "type": "photo",
            "media": message.photo,
            "caption": message.raw_text or "",
        })
    elif message.video:
        media_list.append({
            "type": "video",
            "media": message.video,
            "caption": message.raw_text or "",
        })
    elif message.document:
        media_list.append({
            "type": "document",
            "media": message.document,
            "caption": message.raw_text or "",
        })
    elif message.gif:
        media_list.append({
            "type": "animation",
            "media": message.gif,
            "caption": message.raw_text or "",
        })
    
    return media_list


def split_long_whatsapp_text(text: str, max_length: int = 4096) -> List[str]:
    """Split text into chunks that fit WhatsApp's 4096 char limit."""
    if len(text) <= max_length:
        return [text]
    
    parts = []
    remaining = text
    
    while remaining:
        if len(remaining) <= max_length:
            parts.append(remaining)
            break
        
        # Find a good split point
        chunk = remaining[:max_length]
        split_pos = max(
            chunk.rfind("\n\n"),
            chunk.rfind(". "),
            chunk.rfind("! "),
            chunk.rfind("? "),
        )
        
        if split_pos > max_length // 2:
            parts.append(remaining[:split_pos + 1])
            remaining = remaining[split_pos + 1:]
        else:
            # Hard split
            parts.append(remaining[:max_length - 3] + "...")
            remaining = remaining[max_length - 3:]
    
    return parts