import os
from pathlib import Path

# -----------------------------------------------------------------------------
# .env file loader — works everywhere (normal Python, Pydroid3, systemd)
# -----------------------------------------------------------------------------
# The original code used python‑dotenv's ``load_dotenv()``, which breaks
# under Pydroid3 because it guesses the folder from the call stack.
# Instead we read the .env file explicitly right next to this file, so it
# works identically on every platform.
# -----------------------------------------------------------------------------
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.is_file():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID_RAW = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_NAME = os.getenv("SESSION_NAME", "ChannelFlow")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Add it to your .env file.")

if not API_ID_RAW:
    raise RuntimeError("API_ID is not set. Add it to your .env file.")

if not API_HASH:
    raise RuntimeError("API_HASH is not set. Add it to your .env file.")

try:
    API_ID = int(API_ID_RAW)
except ValueError:
    raise RuntimeError("API_ID must be a numeric Telegram API id.")

# Comma-separated list of Telegram user ids allowed to use /admin,
# e.g. ADMIN_IDS=123456789,987654321
_raw_admin_ids = os.getenv("ADMIN_IDS", "")
if _raw_admin_ids.strip():
    ADMIN_IDS = {
        int(x.strip())
        for x in _raw_admin_ids.split(",")
        if x.strip().isdigit()
    }
else:
    ADMIN_IDS = set()

# ==========================================
# INSTAGRAM (optional)
# ==========================================
# All optional and unchecked here on purpose: a deployment with no
# Instagram project configured should never fail to start over this.
# Capability is reported to the user via
# destinations.instagram_destination.is_configured()/verify_capability(),
# not by crashing at import time.

INSTAGRAM_APP_ID = os.getenv("INSTAGRAM_APP_ID")
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET")
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN")

# ==========================================
# PER-USER TELEGRAM LOGIN (optional)
# Required only if you enable the /connect flow (core/user_sessions.py)
# that lets customers log their own Telegram account into the bot for
# per-user forwarding. Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
SESSION_ENCRYPTION_KEY = os.getenv("SESSION_ENCRYPTION_KEY")
if not SESSION_ENCRYPTION_KEY:
    import warnings
    warnings.warn(
        "SESSION_ENCRYPTION_KEY not set - /connect flow will be disabled",
        RuntimeWarning,
    )
    SESSION_ENCRYPTION_KEY = None

# OXAPAY (crypto payments) and UPI (manual, admin-approved) - Phase 14.
OXAPAY_API_KEY = os.getenv("OXAPAY_API_KEY")
UPI_ID = os.getenv("UPI_ID")
UPI_PAYEE_NAME = os.getenv("UPI_PAYEE_NAME")

# ==========================================
# WHATSAPP BUSINESS API (Cloud API)
# ==========================================
# Get these from Meta Business Manager -> WhatsApp -> Configuration
# WHATSAPP_PHONE_NUMBER_ID: The ID of the phone number attached to your WABA
# WHATSAPP_WABA_ID: Your WhatsApp Business Account ID
# WHATSAPP_ACCESS_TOKEN: Permanent or long-lived token (or use app secret to generate)
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_WABA_ID = os.getenv("WHATSAPP_WABA_ID")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")

# ==========================================
# THREADS API (Meta)
# ==========================================
# Create a Meta App with threads_basic + threads_content_publish permissions
# THREADS_APP_ID: Your Meta App ID
# THREADS_APP_SECRET: Your Meta App Secret
THREADS_APP_ID = os.getenv("THREADS_APP_ID")
THREADS_APP_SECRET = os.getenv("THREADS_APP_SECRET")

# ==========================================
# AI / OPENROUTER
# ==========================================
# ==========================================
# AI / GOOGLE AI STUDIO (GEMINI)
# ==========================================
# Gemini via Google AI Studio is the AI provider for both the AI rewriter
# (services/ai_service.py) and the support chatbot
# (services/support_ai_service.py). Get a key from https://aistudio.google.com/apikey
#
# GEMINI_API_KEY is required for AI features; without it they stay disabled
# and forwarding continues unchanged (never a hard failure).
GEMINI_API_KEY = (
    os.getenv("GEMINI_API_KEY")
    or os.getenv("GOOGLE_AI_STUDIO_API_KEY")
    or os.getenv("GOOGLE_API_KEY")
)
# Primary model. Override per project in project_ai_settings.model_override.
AI_MODEL = os.getenv("AI_MODEL", "gemini-2.0-flash")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", AI_MODEL)
# Comma-separated fallback chain used when the primary model is unavailable.
GEMINI_FALLBACK_MODELS = tuple(
    m.strip()
    for m in os.getenv(
        "GEMINI_FALLBACK_MODELS", "gemini-2.0-flash-lite,gemini-1.5-flash"
    ).split(",")
    if m.strip()
)
# Model used by the support chatbot (can be the same as the rewriter).
SUPPORT_AI_MODEL = os.getenv("SUPPORT_AI_MODEL", "gemini-2.0-flash")

# Legacy OpenRouter configuration. Retained only so an existing .env does not
# break on startup; the provider itself is no longer called.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# ==========================================
# FX RATE (single source of truth for INR<->USD)
# ==========================================
INR_PER_USD = float(os.getenv("INR_PER_USD", "85"))


# ==========================================
# OWNER IDENTITY (separate from normal admins)
# ==========================================
# Owner is identified first by OWNER_ID (env), then by a 3-step
# credential challenge (username / password / security answer) implemented
# in bot/owner_panel.py. Admin/owner actions are audited there.
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID", "").isdigit() else None
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "")
OWNER_PASSWORD_HASH = os.getenv("OWNER_PASSWORD_HASH", "")
OWNER_SECURITY_ANSWER_HASH = os.getenv("OWNER_SECURITY_ANSWER_HASH", "")
# Max failed owner-auth attempts before a temporary cooldown.
OWNER_MAX_ATTEMPTS = int(os.getenv("OWNER_MAX_ATTEMPTS", "5"))
OWNER_LOCKOUT_SECONDS = int(os.getenv("OWNER_LOCKOUT_SECONDS", "300"))

# ==========================================
# WHATSAPP PAIRING
# ==========================================
# Pairing code expires after this many minutes (default 10 minutes)
PAIRING_CODE_EXPIRY_MINUTES = int(os.getenv("PAIRING_CODE_EXPIRY_MINUTES", "10"))
# HMAC secret for signing pairing codes so the WhatsApp bot can verify
# the code was issued by this server (replay/cross-user protection).
# Generate: python -c "import secrets; print(secrets.token_hex(32))"
PAIRING_HMAC_SECRET = os.getenv("PAIRING_HMAC_SECRET", "")
# Standalone pairing server bind config (see pairing_server.py)
PAIRING_SERVER_HOST = os.getenv("PAIRING_SERVER_HOST", "0.0.0.0")
PAIRING_SERVER_PORT = int(os.getenv("PAIRING_SERVER_PORT", "8081"))
