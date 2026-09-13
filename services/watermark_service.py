"""
ChannelFlow AI - Image Watermark Service
==========================================

Adds text or logo watermarks to images using Pillow.

Per-project settings (project_watermark_settings):
    enabled, type (text/logo), text, position, font_size,
    opacity, margin, logo_path

Design:
    * Single re-encode per image - never multiple passes
    * Supports JPEG/PNG input; outputs JPEG for photos
    * Position presets: top-left, top-right, bottom-left, bottom-right, center
    * Text watermark uses default font (Pillow built-in) - no external deps
    * Logo watermark: user uploads a PNG once, stored per project
"""

import io
import logging
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from database.db import get_connection

logger = logging.getLogger(__name__)

# Where uploaded logos are stored (relative to project root)
LOGO_STORAGE_DIR = Path("media/logos")

POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "center")


# ==========================================
# PER-PROJECT SETTINGS
# ==========================================

def ensure_watermark_settings(project_id: int) -> dict:

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM project_watermark_settings WHERE project_id=?", (project_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute(
            "INSERT INTO project_watermark_settings(project_id) VALUES(?)",
            (project_id,),
        )
        conn.commit()
        cur.execute("SELECT * FROM project_watermark_settings WHERE project_id=?", (project_id,))
        row = cur.fetchone()

    conn.close()

    return {
        "enabled": bool(row["enabled"]),
        "type": row["type"] or "text",
        "text": row["text"] or "",
        "position": row["position"] or "bottom-right",
        "font_size": row["font_size"] or 24,
        "opacity": row["opacity"] if row["opacity"] is not None else 0.7,
        "margin": row["margin"] if row["margin"] is not None else 16,
        "logo_path": row["logo_path"],
    }


def update_watermark_settings(project_id: int, **fields):

    allowed = {
        "enabled", "type", "text", "position", "font_size",
        "opacity", "margin", "logo_path",
    }

    filtered = {k: v for k, v in fields.items() if k in allowed}
    if not filtered:
        return

    conn = get_connection()
    cur = conn.cursor()

    ensure_watermark_settings(project_id)

    sets = ", ".join(f"{k}=?" for k in filtered)
    values = list(filtered.values()) + [project_id]

    cur.execute(f"UPDATE project_watermark_settings SET {sets} WHERE project_id=?", values)
    conn.commit()
    conn.close()


def save_logo(project_id: int, image_bytes: bytes) -> str:
    """Saves an uploaded logo PNG and returns its relative path."""

    LOGO_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    path = LOGO_STORAGE_DIR / f"logo_{project_id}.png"

    # Validate it's a real image before storing
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.verify()
    except Exception:
        raise ValueError("Uploaded file isn't a valid image.")

    path.write_bytes(image_bytes)
    return str(path)


# ==========================================
# WATERMARK APPLICATION
# ==========================================

def _calculate_position(img_size, wm_size, position, margin):

    img_w, img_h = img_size
    wm_w, wm_h = wm_size

    positions = {
        "top-left": (margin, margin),
        "top-right": (img_w - wm_w - margin, margin),
        "bottom-left": (margin, img_h - wm_h - margin),
        "bottom-right": (img_w - wm_w - margin, img_h - wm_h - margin),
        "center": ((img_w - wm_w) // 2, (img_h - wm_h) // 2),
    }

    return positions.get(position, positions["bottom-right"])


def _apply_text_watermark(img, settings):

    text = settings.get("text") or ""
    if not text.strip():
        return img

    draw = ImageDraw.Draw(img)

    font_size = settings.get("font_size") or 24

    # Try to load a TrueType font, fall back to Pillow's bitmap default
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", font_size)
    except (OSError, IOError):
        font = ImageFont.load_default()

    opacity = max(0.0, min(1.0, settings.get("opacity", 0.7)))
    alpha = int(255 * opacity)

    # Measure text
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    position = _calculate_position(img.size, (text_w, text_h), settings.get("position"), settings.get("margin", 16))

    # Draw semi-transparent text by compositing on an RGBA layer
    overlay = Image.new("RGBA", img.size, (255, 255, 255, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.text(position, text, font=font, fill=(255, 255, 255, alpha))

    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _apply_logo_watermark(img, settings):

    logo_path = settings.get("logo_path")
    if not logo_path or not Path(logo_path).exists():
        logger.warning("Logo watermark configured but file missing: %s", logo_path)
        return img

    try:
        logo = Image.open(logo_path).convert("RGBA")
    except Exception:
        logger.exception("Failed to open logo %s", logo_path)
        return img

    # Scale logo to ~15% of image width (keeps aspect)
    target_w = max(1, img.width // 6)
    ratio = target_w / logo.width
    target_h = max(1, int(logo.height * ratio))
    logo = logo.resize((target_w, target_h), Image.LANCZOS)

    # Apply opacity
    opacity = max(0.0, min(1.0, settings.get("opacity", 0.7)))
    if opacity < 1.0:
        alpha = logo.split()[3]
        alpha = alpha.point(lambda p: int(p * opacity))
        logo.putalpha(alpha)

    position = _calculate_position(img.size, logo.size, settings.get("position"), settings.get("margin", 16))

    base = img.convert("RGBA")
    base.alpha_composite(logo, position)

    return base.convert("RGB")


def apply_watermark(image_bytes: bytes, project_id: int) -> tuple:
    """Applies the project's watermark to raw image bytes.

    Returns (watermarked_bytes, applied_bool). If watermarking is
    disabled or fails, returns the original bytes unchanged."""

    try:
        settings = ensure_watermark_settings(project_id)
    except Exception:
        logger.exception("Failed to load watermark settings")
        return image_bytes, False

    if not settings.get("enabled"):
        return image_bytes, False

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()

        # Convert to RGB (drops transparency for JPEG output)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        if settings.get("type") == "logo":
            result = _apply_logo_watermark(img, settings)
        else:
            result = _apply_text_watermark(img, settings)

        out = io.BytesIO()
        result.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue(), True

    except Exception:
        logger.exception("Watermark application failed - returning original")
        return image_bytes, False