# ==========================================
# USER STATES
# ==========================================
# Every WAITING_* dict is keyed by telegram user id and cleared by
# handlers._reset_waiting_states() whenever the user navigates away,
# so a stale flag can never swallow an unrelated future message.

# Waiting for project name
WAITING_PROJECT_NAME = {}

# Waiting for source username
WAITING_SOURCE = {}

# Waiting for destination username
WAITING_DESTINATION = {}

# Waiting for rename project
WAITING_RENAME = {}

# Waiting for "min,max" delay input
WAITING_DELAY = {}

# Waiting for whitelist keyword input
WAITING_WHITELIST = {}

# Waiting for blacklist keyword input
WAITING_BLACKLIST = {}

# Waiting for regex filter input
WAITING_REGEX = {}

# Waiting for admin broadcast message
WAITING_BROADCAST = {}

# Project name captured, waiting for the user to tap a platform-type
# button (Telegram / Instagram Broadcast / Both) before the project
# row is actually created - user_id -> pending project name.
PENDING_PROJECT_NAME = {}

# Waiting for the converter-output (processing) channel username, when
# setting up an instagram_broadcast/both project.
WAITING_PROCESSING_CHANNEL = {}

# Waiting for an Instagram Business Account id to attach as a
# destination.
WAITING_INSTAGRAM_DESTINATION = {}

# Phase 7: unified filter + formatting engine.
# user_id -> {"project_id": int, "field": str} for the single-value
# content_rules / formatting_rules fields (required_keywords,
# hashtag_filter, domain_whitelist, domain_blacklist, min_length,
# max_length, sender_whitelist, sender_blacklist, prefix, suffix).
WAITING_CONTENT_RULE_FIELD = {}
WAITING_FORMATTING_FIELD = {}

# user_id -> {"project_id": int, "stage": "find"|"replace", "find": str}
WAITING_REPLACE_RULE = {}

# user_id -> {"project_id": int}
WAITING_REMOVE_PATTERN = {}

# user_id -> payment_request_id, waiting for a screenshot photo
WAITING_PAYMENT_SCREENSHOT = {}

# user_id -> True while writing a feature request / feedback message
WAITING_FEEDBACK = {}

# user_id -> True while typing a custom AI prompt (send '-' to clear)
WAITING_AI_PROMPT = {}

# user_id -> True while typing watermark text (send '-' to clear)
WAITING_WM_TEXT = {}

# user_id -> {"context": "plan"|"wallet", "amount": float, "plan": str}
# while typing a coupon code
WAITING_COUPON_CODE = {}

# user_id -> {"category": str} while typing ticket subject
WAITING_TICKET_SUBJECT = {}

# user_id -> {"category": str, "subject": str} while typing ticket message
WAITING_TICKET_MESSAGE = {}

# user_id -> ticket_id while typing a ticket reply
WAITING_TICKET_REPLY = {}

# Per-user Telegram login (/connect). See core/user_sessions.py.
# user_id -> True while waiting for a phone number, "code"/"password"
# while a core.user_sessions attempt is in the corresponding stage
# (kept in sync with core.user_sessions._pending's own stage field so
# the bot layer knows which prompt to show without importing the
# module's internal state directly).
WAITING_CONNECT_PHONE = {}
WAITING_CONNECT_STAGE = {}

# WhatsApp connect: user_id -> {"stage": "phone_id"|"token"} while
# submitting their WABA credentials step by step (never logged)
WAITING_WA_CREDS = {}

# Threads connect: user_id -> {"stage": "user_id"|"token"} while
# submitting their Threads credentials step by step (never logged)
WAITING_TH_TOKEN = {}

# Current selected project
CURRENT_PROJECT = {}

# Current selected source
CURRENT_SOURCE = {}

# Current selected destination
CURRENT_DESTINATION = {}

# Temporary user data
USER_CACHE = {}
