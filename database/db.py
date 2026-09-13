"""
ChannelFlow AI - Database Layer
=================================

SQLite access layer for the whole project. All connections:

    * Enable ``PRAGMA foreign_keys=ON`` (SQLite defaults this OFF per
      connection, so it must be set every time or ON DELETE CASCADE
      silently does nothing).
    * Use WAL journal mode so the bot's request/response cycle and the
      forwarder's background writes don't block each other with
      "database is locked" errors.
    * Use ``sqlite3.Row`` so callers can access columns by name.

Schema
------
users                    - one row per Telegram bot user (owner of projects)
projects                 - forwarding "pipelines", owned by a user
sources                  - chats a project listens to
destinations             - Telegram chats a project forwards into
project_settings         - per-project forward/filter/delay configuration
logs                     - forward/error/retry log lines per project
stats                    - running counters per project

Added for multi-destination (Telegram + Instagram Broadcast) support:

instagram_destinations   - Instagram target(s) attached to a project
instagram_format_settings- per-project Instagram caption formatting
processing_jobs          - one row per converter-output post detected,
                           carrying it through DETECTED -> ... -> PUBLISHED
                           (or FAILED_PERMANENTLY); the unique constraint on
                           (project_id, converter_chat_id, converter_message_id)
                           is what makes duplicate publishing structurally
                           impossible, not just unlikely.
publish_logs             - append-only attempt history per processing_job
                           (processing_jobs holds current state; this holds
                           the retry history that led there)
promotional_posts        - one row per /post or /broadcast admin send
promotional_deliveries   - one row per individual target of a promotional
                           post/broadcast, so partial failures are visible

Added for Phase 7 (unified filter + formatting engine):

content_rules            - per-project ADDITIONAL filter dimensions
                           (hashtag/domain/length/sender/required-keyword
                           logic), layered on top of the existing
                           project_settings whitelist/blacklist/regex/
                           media filters rather than replacing them.
formatting_rules         - per-project content TRANSFORMATION rules
                           (prefix/suffix/replace/remove), shared by
                           every destination type - see
                           services/formatting_service.py's module
                           docstring for why this isn't duplicated per
                           platform.

Added for Phase 8 (minimal plan/entitlement layer):

users.plan               - FREE/BEGINNER/PRO/MAX, admin-assigned for
                           now (no payment processing yet). See
                           services/plan_service.py.
users.plan_expiry        - nullable; unused until Phase 14 (real
                           payments) wires up expiry enforcement, but
                           the column exists now so that phase doesn't
                           need another migration.
daily_usage               - per-project, per-day forward count, used by
                           plan_service.within_daily_forward_limit().
                           Kept separate from `stats` (a lifetime
                           counter) and from `logs` (capped/trimmed at
                           500 rows per project, so it can't reliably
                           answer "how many today").

Added for the /connect flow (per-user Telegram login):

user_telegram_sessions   - one encrypted Telethon session per customer
                           who has connected their own Telegram account
                           (core/session_crypto.py encrypts before this
                           table ever sees the session string).

Added for the referral program (Invite button):

referrals                - one row per successful invite. Captured at
                           /start (deep-link ref_<inviter_id> param),
                           rewarded later when the referred user's plan
                           actually becomes PRO - see
                           services/referral_service.py for why those
                           are two separate steps, not one.

Added for the Upgrade flow:

payment_requests          - one row per upgrade attempt: plan, price,
                           method (crypto via OXAPAY / UPI), the
                           generated payment link/reference, and the
                           admin-approval state. See
                           services/payment_service.py.
"""

import sqlite3
import os

DB_NAME = os.getenv("DB_NAME", "channelflow.db")


def get_connection():

    conn = sqlite3.connect(DB_NAME, timeout=30)

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")

    return conn


def _column_exists(cur, table, column):

    cur.execute(f"PRAGMA table_info({table})")
    return any(row["name"] == column for row in cur.fetchall())


def _migrate_existing_schema(cur):
    """
    Adds columns that didn't exist in earlier versions of this project
    to tables that already exist, so upgrading in place on a database
    that already has real data never crashes with
    'no such column'.
    """

    if not _column_exists(cur, "sources", "enabled"):
        cur.execute("ALTER TABLE sources ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")

    if not _column_exists(cur, "destinations", "enabled"):
        cur.execute("ALTER TABLE destinations ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")

    if not _column_exists(cur, "users", "is_admin"):
        cur.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

    # --- Phase 8: minimal plan/entitlement layer ---
    # No real payment processing yet - admin sets this manually (see
    # services/plan_service.py). Every existing user gets 'FREE' for
    # free via the column default, same backfill-for-free pattern used
    # throughout this file.
    if not _column_exists(cur, "users", "plan"):
        cur.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'FREE'")

    if not _column_exists(cur, "users", "plan_expiry"):
        cur.execute("ALTER TABLE users ADD COLUMN plan_expiry TIMESTAMP")

    # MAX was renamed to CREATORS, now renamed to CREATOR - convert any rows
    cur.execute("UPDATE users SET plan='CREATOR' WHERE plan='MAX'")
    cur.execute("UPDATE users SET plan='CREATOR' WHERE plan='CREATORS'")

    # --- Wallet INR migration ---
    if not _column_exists(cur, "users", "wallet_balance_usd"):
        cur.execute("ALTER TABLE users ADD COLUMN wallet_balance_usd REAL NOT NULL DEFAULT 0")

    if not _column_exists(cur, "users", "wallet_balance_inr"):
        cur.execute("ALTER TABLE users ADD COLUMN wallet_balance_inr REAL NOT NULL DEFAULT 0")
        # Backfill: convert existing USD balances to INR
        cur.execute("UPDATE users SET wallet_balance_inr = wallet_balance_usd * 85 WHERE wallet_balance_usd > 0")

    # Preferred wallet currency per user (separated INR / USD balances).
    if not _column_exists(cur, "users", "wallet_currency"):
        cur.execute("ALTER TABLE users ADD COLUMN wallet_currency TEXT NOT NULL DEFAULT 'INR'")

    if not _column_exists(cur, "users", "auto_renew"):
        cur.execute("ALTER TABLE users ADD COLUMN auto_renew INTEGER NOT NULL DEFAULT 0")

    # What to auto-renew INTO - set whenever a real (non-trial,
    # non-topup) plan payment is approved, read by the subscription
    # scheduler when a plan expires with auto_renew=1.
    if not _column_exists(cur, "users", "last_purchase_plan"):
        cur.execute("ALTER TABLE users ADD COLUMN last_purchase_plan TEXT")

    if not _column_exists(cur, "users", "last_purchase_months"):
        cur.execute("ALTER TABLE users ADD COLUMN last_purchase_months INTEGER")

    # User language preference
    if not _column_exists(cur, "users", "language"):
        cur.execute("ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'en'")

    # User last name (Telegram profile)
    if not _column_exists(cur, "users", "last_name"):
        cur.execute("ALTER TABLE users ADD COLUMN last_name TEXT")

    # Account status: active / suspended / banned
    if not _column_exists(cur, "users", "status"):
        cur.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")

    # --- multi-destination platform support ---
    #
    # Every existing row gets platform_type='telegram' for free because
    # that's the column default, which is exactly the "existing projects
    # default to Telegram mode" migration behaviour that's required -
    # nothing else has to run to backfill it.

    if not _column_exists(cur, "projects", "platform_type"):
        cur.execute(
            "ALTER TABLE projects ADD COLUMN platform_type TEXT NOT NULL DEFAULT 'telegram'"
        )

    if not _column_exists(cur, "projects", "promo_enabled"):
        cur.execute(
            "ALTER TABLE projects ADD COLUMN promo_enabled INTEGER NOT NULL DEFAULT 1"
        )

    # The Telegram channel that the external affiliate-converter bot
    # posts *into*. Nullable - only set on instagram_broadcast/both
    # projects. Deliberately not reusing `sources`/`destinations` for
    # this: it's not a place messages come from or get forwarded to by
    # this engine, it's the thing the processing listener watches.
    if not _column_exists(cur, "projects", "processing_chat_id"):
        cur.execute("ALTER TABLE projects ADD COLUMN processing_chat_id TEXT")

    if not _column_exists(cur, "projects", "processing_username"):
        cur.execute("ALTER TABLE projects ADD COLUMN processing_username TEXT")

    if not _column_exists(cur, "projects", "processing_title"):
        cur.execute("ALTER TABLE projects ADD COLUMN processing_title TEXT")

    # --- destinations columns for multi-platform support ---
    if not _column_exists(cur, "destinations", "is_middle"):
        cur.execute("ALTER TABLE destinations ADD COLUMN is_middle INTEGER NOT NULL DEFAULT 0")

    if not _column_exists(cur, "destinations", "platform_account_id"):
        cur.execute("ALTER TABLE destinations ADD COLUMN platform_account_id INTEGER")

    if not _column_exists(cur, "destinations", "pairing_code"):
        cur.execute("ALTER TABLE destinations ADD COLUMN pairing_code TEXT")

    if not _column_exists(cur, "destinations", "pairing_status"):
        cur.execute("ALTER TABLE destinations ADD COLUMN pairing_status TEXT NOT NULL DEFAULT 'pending'")

    if not _column_exists(cur, "destinations", "pairing_created_at"):
        cur.execute("ALTER TABLE destinations ADD COLUMN pairing_created_at INTEGER")

    if not _column_exists(cur, "destinations", "pairing_expires_at"):
        cur.execute("ALTER TABLE destinations ADD COLUMN pairing_expires_at INTEGER")

    if not _column_exists(cur, "destinations", "whatsapp_session_state"):
        cur.execute(
            "ALTER TABLE destinations ADD COLUMN whatsapp_session_state TEXT NOT NULL DEFAULT 'pending'"
        )

    if not _column_exists(cur, "destinations", "pairing_signature"):
        cur.execute("ALTER TABLE destinations ADD COLUMN pairing_signature TEXT")

    # Fresh databases create giveaway_winners later in init_schema();
    # never ALTER a table that does not exist yet.
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='giveaway_winners'")
    if cur.fetchone() and not _column_exists(cur, "giveaway_winners", "granted_at"):
        cur.execute("ALTER TABLE giveaway_winners ADD COLUMN granted_at TIMESTAMP")

    # --- Phase 10: WhatsApp pairing codes ---
    # UUIDv4 pairing codes with expiry, rate limiting, one-time usage.
    # Check if the table exists; if not, create it.
    cur.execute("""
        SELECT name FROM sqlite_master WHERE type='table' AND name='whatsapp_pairing_codes'
    """)
    if not cur.fetchone():
        cur.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_pairing_codes(
                code TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                platform_account_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                used_at INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                FOREIGN KEY(platform_account_id) REFERENCES platform_accounts(id) ON DELETE SET NULL,
                UNIQUE(user_id, code)
            )
        """)
        # Seed with example codes for the first registered user.
        # In production, codes are generated on-demand via /connect_whatsapp.
        # Use the first user from the users table; if none exist, skip seeding.
        import uuid, time
        conn2 = get_connection()
        cur2 = conn2.cursor()
        cur2.execute("SELECT telegram_id FROM users LIMIT 1")
        user_row = cur2.fetchone()
        conn2.close()
        if user_row is None:
            # No users yet; seeding will happen later when first user registers
            pass
        else:
            owner_id = user_row[0]
            now = int(time.time())
            for i, (_, stars_price) in enumerate([(1, 100), (3, 300), (6, 600)]):
                code = str(uuid.uuid4())
                expires = now + 24 * 3600  # 24 hours expiry
                cur.execute(
                    "INSERT INTO whatsapp_pairing_codes(code, user_id, expires_at, created_at, status) VALUES (?, ?, ?, ?, ?)",
                    (code, owner_id, expires, now, "pending"),
                )

def _migrate_late_tables(cur):
    """
    Column migrations for tables created LATER in init_db than the
    point where _migrate_existing_schema runs (wallet_transactions,
    payment_requests, plan_configs). Called at the very end of init_db
    so every table already exists by then.
    """

    # Crypto USD pricing (separate from INR - Prompt 3 requirement)
    if not _column_exists(cur, "plan_configs", "crypto_monthly_price_usd"):
        cur.execute("ALTER TABLE plan_configs ADD COLUMN crypto_monthly_price_usd REAL NOT NULL DEFAULT 0")
        cur.execute("UPDATE plan_configs SET crypto_monthly_price_usd=6.99 WHERE plan_name='BEGINNER'")
        cur.execute("UPDATE plan_configs SET crypto_monthly_price_usd=14.99 WHERE plan_name='PRO'")
        cur.execute("UPDATE plan_configs SET crypto_monthly_price_usd=19.99 WHERE plan_name='CREATOR'")

    # Crypto-specific duration discount override (nullable - falls back
    # to discount_percent when NULL)
    if not _column_exists(cur, "plan_durations", "crypto_discount_percent"):
        cur.execute("ALTER TABLE plan_durations ADD COLUMN crypto_discount_percent REAL")

    # Payment price snapshot + provider fields (Prompt 3 section 25/26)
    if not _column_exists(cur, "payment_requests", "currency"):
        cur.execute("ALTER TABLE payment_requests ADD COLUMN currency TEXT NOT NULL DEFAULT 'INR'")

    if not _column_exists(cur, "payment_requests", "base_amount"):
        cur.execute("ALTER TABLE payment_requests ADD COLUMN base_amount REAL")

    if not _column_exists(cur, "payment_requests", "discount_percent"):
        cur.execute("ALTER TABLE payment_requests ADD COLUMN discount_percent REAL")

    if not _column_exists(cur, "payment_requests", "coupon_code"):
        cur.execute("ALTER TABLE payment_requests ADD COLUMN coupon_code TEXT")

    if not _column_exists(cur, "payment_requests", "final_amount"):
        cur.execute("ALTER TABLE payment_requests ADD COLUMN final_amount REAL")

    if not _column_exists(cur, "payment_requests", "provider_payment_id"):
        cur.execute("ALTER TABLE payment_requests ADD COLUMN provider_payment_id TEXT")

    if not _column_exists(cur, "payment_requests", "payment_url"):
        cur.execute("ALTER TABLE payment_requests ADD COLUMN payment_url TEXT")

    if not _column_exists(cur, "payment_requests", "expires_at"):
        cur.execute("ALTER TABLE payment_requests ADD COLUMN expires_at TIMESTAMP")

    # Migrate wallet_transactions to INR
    if not _column_exists(cur, "wallet_transactions", "amount_inr"):
        cur.execute("ALTER TABLE wallet_transactions ADD COLUMN amount_inr REAL")
        # Backfill: approximate conversion for existing USD transactions
        cur.execute("UPDATE wallet_transactions SET amount_inr = amount_usd * 85 WHERE amount_inr IS NULL")

    # Idempotency: a UNIQUE index on reference prevents any duplicate
    # charge (e.g. per-forward billing replay) from debiting twice.
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_tx_reference "
        "ON wallet_transactions(reference) WHERE reference IS NOT NULL"
    )

    # Migrate payment_requests to INR
    if not _column_exists(cur, "payment_requests", "amount_inr"):
        cur.execute("ALTER TABLE payment_requests ADD COLUMN amount_inr REAL")
        cur.execute("UPDATE payment_requests SET amount_inr = amount_usd * 85 WHERE amount_inr IS NULL")


def init_db():

    conn = get_connection()

    cur = conn.cursor()

    # ==========================
    # USERS
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        language TEXT NOT NULL DEFAULT 'en',
        is_admin INTEGER NOT NULL DEFAULT 0,
        plan TEXT NOT NULL DEFAULT 'FREE',
        plan_expiry TIMESTAMP,
        wallet_balance_usd REAL NOT NULL DEFAULT 0,
        wallet_balance_inr REAL NOT NULL DEFAULT 0,
        auto_renew INTEGER NOT NULL DEFAULT 0,
        last_purchase_plan TEXT,
        last_purchase_months INTEGER,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # ==========================
    # PROJECTS
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS projects(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        status INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status)")

    # ==========================
    # SOURCES
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sources(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        chat_id TEXT NOT NULL,
        username TEXT,
        title TEXT,
        chat_type TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_sources_project ON sources(project_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sources_chat ON sources(chat_id)")

    # ==========================
    # DESTINATIONS
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS destinations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        chat_id TEXT NOT NULL,
        username TEXT,
        title TEXT,
        chat_type TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        is_middle INTEGER NOT NULL DEFAULT 0,
        platform_account_id INTEGER,
        pairing_code TEXT,
        pairing_status TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_destinations_project ON destinations(project_id)")

    # ==========================
    # PLATFORM ACCOUNTS (multi-account architecture)
    # ==========================
    # Stores per-user, per-platform credentials (encrypted tokens, status, health)
    # Replaces the single-session approach with a flexible account abstraction.

    cur.execute("""
    CREATE TABLE IF NOT EXISTS platform_accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        platform TEXT NOT NULL,              -- 'telegram', 'whatsapp_channel', 'threads', etc.
        account_identifier TEXT NOT NULL,    -- phone_number_id, threads_user_id, telegram_session_name
        encrypted_credentials TEXT,          -- Fernet-encrypted JSON with tokens/secrets
        access_token TEXT,                   -- Plain access token (for quick lookup, not primary storage)
        refresh_token TEXT,                  -- Refresh token for OAuth platforms
        expires_at REAL,                     -- Unix timestamp of token expiry
        status TEXT NOT NULL DEFAULT 'connected',  -- 'connected', 'disconnected', 'error', 'expired'
        health_status TEXT,                  -- 'healthy', 'warning', 'error', 'disconnected'
        last_health_check REAL,              -- Unix timestamp
        rate_limit_state TEXT,               -- JSON with rate limit tracking
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, platform, account_identifier),
        FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_platform_accounts_user ON platform_accounts(user_id, platform)")

    # ==========================
    # AFFILIATE SETTINGS (per-project affiliate configuration)
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS project_affiliate_settings(
        project_id INTEGER PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 0,
        amazon_enabled INTEGER NOT NULL DEFAULT 0,
        flipkart_enabled INTEGER NOT NULL DEFAULT 0,
        meesho_enabled INTEGER NOT NULL DEFAULT 0,
        wishlink_enabled INTEGER NOT NULL DEFAULT 0,
        earnkaro_enabled INTEGER NOT NULL DEFAULT 0,
        amazon_associate_tag TEXT NOT NULL DEFAULT '',
        flipkart_publisher_id TEXT NOT NULL DEFAULT '',
        meesho_partner_id TEXT NOT NULL DEFAULT '',
        wishlink_partner_id TEXT NOT NULL DEFAULT '',
        earnkaro_publisher_id TEXT NOT NULL DEFAULT '',
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    # ==========================
    # PROJECT AI SETTINGS (per-project AI configuration)
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS project_ai_settings(
        project_id INTEGER PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 0,
        model_override TEXT,
        tone TEXT NOT NULL DEFAULT 'original',
        length_mode TEXT NOT NULL DEFAULT 'keep',
        preserve_urls INTEGER NOT NULL DEFAULT 1,
        preserve_hashtags INTEGER NOT NULL DEFAULT 1,
        custom_prompt TEXT,
        language TEXT,
        add_cta INTEGER NOT NULL DEFAULT 0,
        remove_spam INTEGER NOT NULL DEFAULT 0,
        mandatory INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    # ==========================
    # AI USAGE LOG (cost control + analytics)
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS ai_usage(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        project_id INTEGER,
        model TEXT NOT NULL,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        latency_ms INTEGER NOT NULL DEFAULT 0,
        success INTEGER NOT NULL DEFAULT 1,
        fallback_used INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_user ON ai_usage(user_id, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_project ON ai_usage(project_id, created_at)")

    # ==========================
    # PROJECT WATERMARK SETTINGS
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS project_watermark_settings(
        project_id INTEGER PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 0,
        type TEXT NOT NULL DEFAULT 'text',
        text TEXT NOT NULL DEFAULT '',
        position TEXT NOT NULL DEFAULT 'bottom-right',
        font_size INTEGER NOT NULL DEFAULT 24,
        opacity REAL NOT NULL DEFAULT 0.7,
        margin INTEGER NOT NULL DEFAULT 16,
        logo_path TEXT,
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    # ==========================
    # MESSAGE DEDUPLICATION
    # ==========================
    # UNIQUE on dedup_key makes claim() atomic even across processes.
    # Survives restarts - it's the database, not memory.

    cur.execute("""
    CREATE TABLE IF NOT EXISTS message_deduplication(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dedup_key TEXT NOT NULL UNIQUE,
        source_platform TEXT NOT NULL DEFAULT 'telegram',
        source_account_id TEXT,
        source_channel_id TEXT,
        source_message_id TEXT,
        project_id INTEGER,
        destination_id INTEGER,
        content_hash TEXT,
        status TEXT NOT NULL DEFAULT 'claimed',
        delivered_message_id TEXT,
        delivered_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_dedup_project ON message_deduplication(project_id, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_dedup_status ON message_deduplication(status, created_at)")

# ==========================
    # POST EDIT SYNC MAPPINGS
    # ==========================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS post_edit_mappings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        source_chat_id TEXT,
        source_message_id INTEGER NOT NULL,
        target_chat_id TEXT,
        target_message_id INTEGER,
        content_hash TEXT,
        destination_message_id TEXT,
        destination_id INTEGER,
        edit_token TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_post_edit_project "
        "ON post_edit_mappings(project_id, created_at)"
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_post_edit_status ON post_edit_mappings(status, created_at)")

    # ==========================
    # AUTO REACTIONS
    # ==========================
    # Stores configured auto-reaction rules per project. A rule defines
    # which Telegram messages (by filter) should receive a specific
    # reaction emoji. Reactions are applied by the forwarder when a
    # matching message is detected, and are idempotent via the
    # confirmed_reactions table.
    # NOTE: this shape must stay in sync with database/hardening.py section 5
    # and services/auto_reaction_service.py. The canonical columns the app
    # reads are (enabled, emojis, trigger_mode, keywords, apply_source,
    # apply_target). The legacy columns are kept nullable purely so that
    # databases created from an older build still accept inserts - they are
    # never read.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS auto_reactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        emojis TEXT NOT NULL DEFAULT '👍',
        trigger_mode TEXT NOT NULL DEFAULT 'all',
        keywords TEXT NOT NULL DEFAULT '',
        apply_source INTEGER NOT NULL DEFAULT 0,
        apply_target INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        -- legacy, nullable, unused by the app
        enable INTEGER,
        reaction_emoji TEXT,
        filter_logic TEXT,
        required_keywords TEXT,
        excluded_keywords TEXT,
        hashtag_filter TEXT,
        sender_whitelist TEXT,
        sender_blacklist TEXT,
        min_length INTEGER,
        max_length INTEGER,
        media_type TEXT,
        apply_source_side INTEGER,
        apply_destination_side INTEGER,
        rate_limit_per_hour INTEGER,
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_auto_reactions_project "
        "ON auto_reactions(project_id, enabled)"
    )

    # ==========================
    # CONFIRMED REACTIONS (idempotency)
    # ==========================
    # Tracks which messages have already been reacted to, so that
    # duplicate reaction events from worker restarts or Telegram
    # resends don't produce double reactions.
    # Reaction idempotency ledger. services/auto_reaction_service.py reads
    # and writes (project_id, chat_id, message_id, emoji) only - the legacy
    # destination_id/reaction_emoji columns were NOT NULL with no default,
    # which made every INSERT OR IGNORE silently fail. database/hardening.py
    # rebuilds that shape on existing deployments; fresh databases get the
    # canonical shape straight away.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS confirmed_reactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        chat_id TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        emoji TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(project_id, chat_id, message_id, emoji)
    )
    """)

    # ==========================
    # JOBS (persistent queue)
    # ==========================
    # JOBS (persistent queue)
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending',
        priority INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        next_run_at TIMESTAMP,
        locked_at TIMESTAMP,
        locked_by TEXT,
        error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, priority DESC, id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(type, status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_stuck ON jobs(status, locked_at)")

    # ==========================
    # DEAD LETTER QUEUE
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS dead_letter_jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        payload TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        error_code TEXT,
        error_message TEXT,
        retried_as_job_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        resolved_at TIMESTAMP
    )
    """)

    # ==========================
    # PROJECT SETTINGS
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS project_settings(
        project_id INTEGER PRIMARY KEY,
        mode TEXT NOT NULL DEFAULT 'forward',
        silent INTEGER NOT NULL DEFAULT 0,
        protect_content INTEGER NOT NULL DEFAULT 0,
        keep_media_groups INTEGER NOT NULL DEFAULT 1,
        delay_min REAL NOT NULL DEFAULT 0,
        delay_max REAL NOT NULL DEFAULT 0,
        media_filter TEXT NOT NULL DEFAULT 'all',
        keyword_whitelist TEXT NOT NULL DEFAULT '',
        keyword_blacklist TEXT NOT NULL DEFAULT '',
        regex_filter TEXT NOT NULL DEFAULT '',
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    # ==========================
    # LOGS
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        level TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_project ON logs(project_id, id DESC)")

    # ==========================
    # STATS
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS stats(
        project_id INTEGER PRIMARY KEY,
        forwarded INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0,
        retried INTEGER NOT NULL DEFAULT 0,
        filtered INTEGER NOT NULL DEFAULT 0,
        last_forward_at TIMESTAMP,
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    # ==========================
    # INSTAGRAM DESTINATIONS
    # ==========================
    #
    # target_type distinguishes the two Instagram publish paths:
    #   "broadcast_channel" - no official publish API exists (verified
    #                         against current Meta docs), so these are
    #                         always routed through the Ready-to-Publish
    #                         approval queue, never auto-published.
    #   "feed"              - real Graph API container/publish flow,
    #                         actually automatable when status='available'.
    #
    # status is set by the app based on whether INSTAGRAM_ACCESS_TOKEN /
    # a valid long-lived token is actually configured and verified - it
    # is never hard-coded to "available".

    cur.execute("""
    CREATE TABLE IF NOT EXISTS instagram_destinations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        ig_user_id TEXT,
        username TEXT,
        title TEXT,
        target_type TEXT NOT NULL DEFAULT 'broadcast_channel',
        status TEXT NOT NULL DEFAULT 'setup_required',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_instagram_destinations_project "
        "ON instagram_destinations(project_id)"
    )

    # ==========================
    # INSTAGRAM FORMAT SETTINGS
    # ==========================
    # Lazily created per project on first read, same pattern as
    # project_settings (see services/settings_service.py).

    cur.execute("""
    CREATE TABLE IF NOT EXISTS instagram_format_settings(
        project_id INTEGER PRIMARY KEY,
        prefix TEXT NOT NULL DEFAULT '',
        suffix TEXT NOT NULL DEFAULT '',
        cta TEXT NOT NULL DEFAULT '',
        hashtags TEXT NOT NULL DEFAULT '',
        emoji_enabled INTEGER NOT NULL DEFAULT 1,
        source_attribution INTEGER NOT NULL DEFAULT 0,
        link_placement TEXT NOT NULL DEFAULT 'bottom',
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    # ==========================
    # PROCESSING JOBS
    # ==========================
    # One row per message detected in a project's processing_chat_id
    # (the converter output channel). The UNIQUE constraint below is the
    # actual duplicate-publish guard, not just the status field - even
    # a crash/retry/duplicate-update mid-insert can't create a second
    # row for the same converted message.

    cur.execute("""
    CREATE TABLE IF NOT EXISTS processing_jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        source_chat_id TEXT,
        source_message_id INTEGER,
        converter_chat_id TEXT NOT NULL,
        converter_message_id INTEGER NOT NULL,
        media_type TEXT,
        media_count INTEGER NOT NULL DEFAULT 0,
        text_content TEXT,
        detected_urls TEXT NOT NULL DEFAULT '[]',
        destination_id INTEGER,
        status TEXT NOT NULL DEFAULT 'DETECTED',
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        published_at TIMESTAMP,
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
        FOREIGN KEY(destination_id) REFERENCES instagram_destinations(id) ON DELETE SET NULL,
        UNIQUE(project_id, converter_chat_id, converter_message_id)
    )
    """)

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_processing_jobs_project "
        "ON processing_jobs(project_id, id DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_processing_jobs_status "
        "ON processing_jobs(status)"
    )

    # ==========================
    # PUBLISH LOGS
    # ==========================
    # Append-only attempt history per processing_job - processing_jobs
    # holds *current* status; this holds every attempt that led there,
    # for debugging a specific post per FINAL ACCEPTANCE CRITERIA #17.

    cur.execute("""
    CREATE TABLE IF NOT EXISTS publish_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        attempt INTEGER NOT NULL,
        status TEXT NOT NULL,
        error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(job_id) REFERENCES processing_jobs(id) ON DELETE CASCADE
    )
    """)

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_publish_logs_job ON publish_logs(job_id)"
    )

    # ==========================
    # PROMOTIONAL POSTS / DELIVERIES
    # ==========================
    # kind distinguishes /post (one admin-picked target) from /broadcast
    # (many targets) - both share the same delivery-tracking shape.

    cur.execute("""
    CREATE TABLE IF NOT EXISTS promotional_posts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL,
        kind TEXT NOT NULL DEFAULT 'broadcast',
        text_content TEXT,
        media_file_id TEXT,
        media_type TEXT,
        target_scope TEXT NOT NULL DEFAULT 'all',
        status TEXT NOT NULL DEFAULT 'DRAFT',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS promotional_deliveries(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        promo_post_id INTEGER NOT NULL,
        project_id INTEGER,
        destination_type TEXT NOT NULL,
        destination_ref TEXT NOT NULL,
        status TEXT NOT NULL,
        error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(promo_post_id) REFERENCES promotional_posts(id) ON DELETE CASCADE,
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
    )
    """)

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_promo_deliveries_post "
        "ON promotional_deliveries(promo_post_id)"
    )

    # ==========================
    # REMINDER LOG (subscription scheduler dedup)
    # ==========================
    # UNIQUE(user_id, plan_expiry, days_mark) means "send the 3-day
    # reminder for THIS specific expiry once" - core/subscription_
    # scheduler.py's loop runs hourly and would otherwise resend the
    # same reminder ~24 times on the day it's due. A renewal changes
    # plan_expiry, which naturally makes the old reminder rows
    # irrelevant without needing to clean them up.

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reminder_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        plan_expiry TEXT NOT NULL,
        days_mark INTEGER NOT NULL,
        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, plan_expiry, days_mark)
    )
    """)

    # ==========================
    # CONTENT RULES (Phase 7)
    # ==========================
    # Additional filter dimensions layered on top of
    # project_settings.{media_filter,keyword_whitelist,keyword_blacklist,
    # regex_filter} - deliberately a separate table rather than more
    # columns crammed onto project_settings, but conceptually the SAME
    # "should this message be forwarded at all" gate, checked together
    # in core/forwarder.py's _passes_all_filters(). Lazily created on
    # first read, same pattern as project_settings.
    #
    # filter_logic governs required_keywords only: "all" = every listed
    # keyword must appear, "any" = at least one must appear.

    cur.execute("""
    CREATE TABLE IF NOT EXISTS content_rules(
        project_id INTEGER PRIMARY KEY,
        required_keywords TEXT NOT NULL DEFAULT '',
        filter_logic TEXT NOT NULL DEFAULT 'any',
        hashtag_filter TEXT NOT NULL DEFAULT '',
        domain_whitelist TEXT NOT NULL DEFAULT '',
        domain_blacklist TEXT NOT NULL DEFAULT '',
        min_length INTEGER,
        max_length INTEGER,
        sender_whitelist TEXT NOT NULL DEFAULT '',
        sender_blacklist TEXT NOT NULL DEFAULT '',
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    # ==========================
    # FORMATTING RULES (Phase 7)
    # ==========================
    # Content TRANSFORMATION (as opposed to content_rules, which only
    # decides forward-or-not). Applied in Telegram copy mode and by the
    # Instagram caption formatter - see services/formatting_service.py.
    # Never applied in Telegram forward mode: native forwards can't have
    # their content edited, and spec requires preserving native forward
    # behaviour rather than faking it.
    #
    # remove_patterns / replace_rules are stored as JSON so this table
    # doesn't need a variable number of columns for a variable number of
    # rules.

    cur.execute("""
    CREATE TABLE IF NOT EXISTS formatting_rules(
        project_id INTEGER PRIMARY KEY,
        prefix TEXT NOT NULL DEFAULT '',
        suffix TEXT NOT NULL DEFAULT '',
        remove_patterns TEXT NOT NULL DEFAULT '[]',
        replace_rules TEXT NOT NULL DEFAULT '[]',
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    # ==========================
    # DAILY USAGE (Phase 8)
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_usage(
        project_id INTEGER NOT NULL,
        usage_date TEXT NOT NULL,
        forward_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(project_id, usage_date),
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    )
    """)

    # ==========================
    # USER TELEGRAM SESSIONS (per-user /connect)
    # ==========================
    # encrypted_session is ALWAYS ciphertext - see
    # core/session_crypto.py. Nothing writes plaintext here.

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_telegram_sessions(
        telegram_id INTEGER PRIMARY KEY,
        encrypted_session TEXT NOT NULL,
        phone_number TEXT,
        status TEXT NOT NULL DEFAULT 'connected',
        connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    # ==========================
    # REFERRALS
    # ==========================
    # referred_id is UNIQUE - each person can only ever be someone's
    # referral once (whoever's link they first used), so there's no
    # ambiguity about who gets rewarded. reward_granted flips to 1 the
    # first (and only) time the referred user's plan becomes PRO -
    # see services/referral_service.py.

    cur.execute("""
    CREATE TABLE IF NOT EXISTS referrals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER NOT NULL,
        referred_id INTEGER NOT NULL UNIQUE,
        reward_granted INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        rewarded_at TIMESTAMP,
        FOREIGN KEY(referrer_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
        FOREIGN KEY(referred_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)"
    )

    # ==========================
    # REFERRAL MILESTONES (configurable tiers)
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS referral_milestones(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrals_required INTEGER NOT NULL,
        reward_type TEXT NOT NULL DEFAULT 'plan',   -- plan|wallet_credit|coupon
        reward_value TEXT NOT NULL,                  -- plan name | INR amount | coupon code
        reward_days INTEGER NOT NULL DEFAULT 0,
        label TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1
    )
    """)

    cur.execute("SELECT COUNT(*) FROM referral_milestones")
    if cur.fetchone()[0] == 0:
        for tier in (
            (50, "plan", "PRO", 7, "50 referrals → +7 days PRO"),
            (100, "plan", "PRO", 15, "100 referrals → +15 days PRO"),
            (250, "plan", "CREATOR", 30, "250 referrals → +30 days CREATOR"),
            (500, "wallet_credit", "500", 0, "500 referrals → ₹500 wallet credit"),
        ):
            cur.execute(
                "INSERT INTO referral_milestones(referrals_required, reward_type, reward_value, reward_days, label) "
                "VALUES (?, ?, ?, ?, ?)",
                tier,
            )

    cur.execute("""
    CREATE TABLE IF NOT EXISTS referral_milestone_grants(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        milestone_id INTEGER NOT NULL,
        granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, milestone_id)
    )
    """)

    # ==========================
    # PAYMENT REQUESTS (Upgrade flow)
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS payment_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        plan TEXT,
        amount_usd REAL,
        amount_inr REAL,
        method TEXT NOT NULL,
        payment_reference TEXT,
        screenshot_file_id TEXT,
        status TEXT NOT NULL DEFAULT 'PENDING_PAYMENT',
        purpose TEXT NOT NULL DEFAULT 'plan',
        months INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        decided_at TIMESTAMP,
        decided_by INTEGER,
        FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_payment_requests_user ON payment_requests(user_id, id DESC)"
    )

    if not _column_exists(cur, "payment_requests", "amount_inr"):
        cur.execute("ALTER TABLE payment_requests ADD COLUMN amount_inr REAL")
        cur.execute("UPDATE payment_requests SET amount_inr = amount_usd * 85 WHERE amount_inr IS NULL")

    if not _column_exists(cur, "payment_requests", "purpose"):
        cur.execute("ALTER TABLE payment_requests ADD COLUMN purpose TEXT NOT NULL DEFAULT 'plan'")

    if not _column_exists(cur, "payment_requests", "months"):
        cur.execute("ALTER TABLE payment_requests ADD COLUMN months INTEGER")

    # ==========================
    # WALLET TRANSACTIONS
    # ==========================
    # Ledger, not just a running total - users.wallet_balance_inr is a
    # cached sum kept in sync by services/wallet_service.py, but every
    # credit/debit is recorded here too so a balance is always
    # auditable back to the payment_request or auto-renew event that
    # produced it.

    cur.execute("""
    CREATE TABLE IF NOT EXISTS wallet_transactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount_usd REAL,
        amount_inr REAL,
        direction TEXT NOT NULL,
        reference TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_wallet_tx_user ON wallet_transactions(user_id, id DESC)"
    )

    if not _column_exists(cur, "wallet_transactions", "amount_inr"):
        cur.execute("ALTER TABLE wallet_transactions ADD COLUMN amount_inr REAL")
        cur.execute("UPDATE wallet_transactions SET amount_inr = amount_usd * 85 WHERE amount_inr IS NULL")

    # ==========================
    # PLAN CONFIGS (admin-configurable plan limits and pricing)
    # ==========================
    # Centralized plan configuration - replaces hardcoded PLAN_LIMITS in plan_service.py
    # All limits and pricing configurable from admin panel.

    cur.execute("""
    CREATE TABLE IF NOT EXISTS plan_configs(
        plan_name TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        monthly_price_inr REAL NOT NULL,
        crypto_monthly_price_usd REAL NOT NULL DEFAULT 0,
        max_projects INTEGER,
        max_sources_per_project INTEGER,
        max_destinations_per_project INTEGER,
        max_middle_destinations_per_project INTEGER,
        daily_forward_limit INTEGER,
        per_project_daily_forward_limit INTEGER,
        requires_attribution INTEGER NOT NULL DEFAULT 1,
        feature_flags TEXT NOT NULL DEFAULT '{}',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Seed default plan configs if table is empty
    cur.execute("SELECT COUNT(*) FROM plan_configs")
    if cur.fetchone()[0] == 0:
        import json
        default_plans = [
            {
                "plan_name": "FREE",
                "display_name": "Free",
                "monthly_price_inr": 0,
                "crypto_monthly_price_usd": 0,
                "max_projects": 1,
                "max_sources_per_project": 2,
                "max_destinations_per_project": 1,
                "max_middle_destinations_per_project": 0,
                "daily_forward_limit": 100,
                "per_project_daily_forward_limit": 100,
                "requires_attribution": 1,
                "feature_flags": json.dumps({"ai_rewrite": False, "scheduling": False, "watermark": False, "analytics": False, "whatsapp": False, "threads": False, "multi_account": False})
            },
            {
                "plan_name": "BEGINNER",
                "display_name": "Beginner",
                "monthly_price_inr": 199,
                "crypto_monthly_price_usd": 6.99,
                "max_projects": 5,
                "max_sources_per_project": 5,
                "max_destinations_per_project": 2,
                "max_middle_destinations_per_project": 1,
                "daily_forward_limit": 200,
                "per_project_daily_forward_limit": 200,
                "requires_attribution": 0,
                "feature_flags": json.dumps({"ai_rewrite": False, "scheduling": True, "watermark": False, "analytics": True, "whatsapp": False, "threads": False, "multi_account": False})
            },
            {
                "plan_name": "PRO",
                "display_name": "Pro",
                "monthly_price_inr": 399,
                "crypto_monthly_price_usd": 14.99,
                "max_projects": 10,
                "max_sources_per_project": 10,
                "max_destinations_per_project": 5,
                "max_middle_destinations_per_project": 5,
                "daily_forward_limit": 1000,
                "per_project_daily_forward_limit": 1000,
                "requires_attribution": 0,
                "feature_flags": json.dumps({"ai_rewrite": True, "scheduling": True, "watermark": True, "analytics": True, "whatsapp": True, "threads": False, "multi_account": False})
            },
            {
                "plan_name": "CREATOR",
                "display_name": "Creator",
                "monthly_price_inr": 799,
                "crypto_monthly_price_usd": 19.99,
                "max_projects": 20,
                "max_sources_per_project": 20,
                "max_destinations_per_project": 10,
                "max_middle_destinations_per_project": 10,
                "daily_forward_limit": 2000,
                "per_project_daily_forward_limit": 2000,
                "requires_attribution": 0,
                "feature_flags": json.dumps({"ai_rewrite": True, "scheduling": True, "watermark": True, "analytics": True, "whatsapp": True, "threads": True, "multi_account": True})
            }
        ]
        for plan in default_plans:
            cur.execute("""
                INSERT INTO plan_configs(plan_name, display_name, monthly_price_inr, crypto_monthly_price_usd, max_projects, max_sources_per_project,
                    max_destinations_per_project, max_middle_destinations_per_project, daily_forward_limit,
                    per_project_daily_forward_limit, requires_attribution, feature_flags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                plan["plan_name"], plan["display_name"], plan["monthly_price_inr"],
                plan["crypto_monthly_price_usd"],
                plan["max_projects"], plan["max_sources_per_project"], plan["max_destinations_per_project"],
                plan["max_middle_destinations_per_project"], plan["daily_forward_limit"],
                plan["per_project_daily_forward_limit"], plan["requires_attribution"], plan["feature_flags"]
            ))

    # ==========================
    # PLAN DURATIONS (admin-configurable pricing/discounts)
    # ==========================
    # One row per (plan, months) combination that's actually for sale,
    # with its own discount_percent - admin-editable via
    # /setduration, never hard-coded in Python, so changing a discount
    # is a data change, not a deploy. See services/pricing_service.py.
    # Now includes price_inr_override for custom pricing per duration.

    cur.execute("""
    CREATE TABLE IF NOT EXISTS plan_durations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan TEXT NOT NULL,
        months INTEGER NOT NULL,
        discount_percent REAL NOT NULL DEFAULT 0,
        price_inr_override REAL,
        active INTEGER NOT NULL DEFAULT 1,
        UNIQUE(plan, months)
    )
    """)

    # Migrate existing plan_durations: add price_inr_override column if missing
    if not _column_exists(cur, "plan_durations", "price_inr_override"):
        cur.execute("ALTER TABLE plan_durations ADD COLUMN price_inr_override REAL")

    # Seed sensible defaults once - admin can change/add rows later via
    # /setduration without touching code. Seeds any plan/duration
    # combination that is missing (per-plan, per-month), so an older
    # database that was seeded before a new plan existed gets the new
    # plan's rows backfilled without ever overwriting an admin's edits
    # to rows that already exist.
    plan_discounts = {"BEGINNER": 5, "PRO": 10, "CREATOR": 20}
    months_spec = ((1, 0), (3, 0), (6, None), (12, None))

    for plan, long_discount in plan_discounts.items():
        for months, short_discount in months_spec:
            discount = long_discount if months > 3 else short_discount
            cur.execute(
                "SELECT COUNT(*) FROM plan_durations WHERE plan=? AND months=?",
                (plan, months),
            )
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "INSERT INTO plan_durations(plan, months, discount_percent, active) VALUES (?, ?, ?, 1)",
                    (plan, months, discount),
                )

    # Late-table migrations - every table now exists, so columns can be
    # added to any of them (INR backfills etc.)
    _migrate_late_tables(cur)

    # ==========================
    # FEATURE REQUESTS / FEEDBACK
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS feature_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        message TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'new',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_feature_requests_user ON feature_requests(user_id, id DESC)")

    # ==========================
    # ADMINS + RBAC
    # ==========================
    # OWNER is env-configured (ADMIN_IDS). Normal admins get granular
    # permissions stored here. Every admin callback checks permissions
    # SERVER-SIDE via services/audit_service.require_permission().

    cur.execute("""
    CREATE TABLE IF NOT EXISTS admins(
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        added_by INTEGER,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS admin_permissions(
        admin_id INTEGER NOT NULL,
        permission TEXT NOT NULL,
        granted INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(admin_id, permission),
        FOREIGN KEY(admin_id) REFERENCES admins(telegram_id) ON DELETE CASCADE
    )
    """)

    # Canonical permission list - owner has all implicitly.
    ADMIN_PERMISSIONS = (
        "dashboard.view", "payments.view", "payments.approve", "payments.reject",
        "wallet.view", "wallet.adjust",
        "users.view", "users.suspend", "users.ban",
        "projects.view", "projects.manage",
        "plans.view", "plans.edit",
        "coupons.view", "coupons.manage",
        "giveaways.view", "giveaways.manage",
        "broadcast.send",
        "support.view", "support.manage",
        "analytics.view",
        "platforms.manage",
        "audit.view",
        "admins.manage",
    )

    for perm in ADMIN_PERMISSIONS:
        pass  # list documented; per-admin rows created on grant

    # ==========================
    # ADMIN AUDIT LOG (immutable)
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS admin_audit_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL,
        admin_username TEXT,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id TEXT,
        before_state TEXT,
        after_state TEXT,
        ip TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_admin ON admin_audit_logs(admin_id, id DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON admin_audit_logs(action, id DESC)")

    # ==========================
    # COUPONS
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS coupons(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        discount_percent REAL NOT NULL DEFAULT 0,
        max_discount_inr REAL,
        min_purchase_inr REAL NOT NULL DEFAULT 0,
        applies_plans TEXT NOT NULL DEFAULT '[]',       -- JSON list of plan names, [] = any purchasable
        applies_wallet INTEGER NOT NULL DEFAULT 0,      -- wallet top-up discount allowed
        start_at TIMESTAMP,
        expires_at TIMESTAMP,
        max_total_uses INTEGER,
        max_uses_per_user INTEGER NOT NULL DEFAULT 1,
        eligible_users TEXT,                            -- JSON list of user_ids, NULL = everyone
        active INTEGER NOT NULL DEFAULT 1,
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS coupon_redemptions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        coupon_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        context TEXT NOT NULL DEFAULT 'plan',           -- 'plan' | 'wallet'
        reference_id TEXT,
        amount_discounted_inr REAL NOT NULL DEFAULT 0,
        idempotency_key TEXT UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(coupon_id) REFERENCES coupons(id) ON DELETE CASCADE
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_redemptions_user ON coupon_redemptions(user_id, id DESC)")

    # ==========================
    # GIVEAWAYS
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS giveaways(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL,                             -- 'discount' | 'subscription'
        winner_count INTEGER NOT NULL DEFAULT 1,
        config TEXT NOT NULL DEFAULT '{}',              -- JSON: discount_pct / plan / days / validity_days
        eligibility TEXT NOT NULL DEFAULT 'all',        -- all|connected|never_purchased|expired|active|inactive|plan:<NAME>
        status TEXT NOT NULL DEFAULT 'draft',           -- draft|winners_selected|delivered|cancelled
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS giveaway_winners(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        giveaway_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        coupon_code TEXT UNIQUE,
        reward_summary TEXT,
        notified_at TIMESTAMP,
        redeemed_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_gw_winners_giveaway ON giveaway_winners(giveaway_id)")

    # ==========================
    # SUPPORT TICKETS
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS support_tickets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        category TEXT NOT NULL DEFAULT 'general',
        subject TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',            -- open|pending|in_progress|resolved|closed
        assigned_admin INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS support_messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL,
        sender_id INTEGER NOT NULL,
        sender_type TEXT NOT NULL,                      -- 'user' | 'admin'
        message TEXT NOT NULL,
        media_file_id TEXT,
        is_internal_note INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(ticket_id) REFERENCES support_tickets(id) ON DELETE CASCADE
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user ON support_tickets(user_id, id DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON support_tickets(status, id DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msgs_ticket ON support_messages(ticket_id, id ASC)")

    # ==========================
    # KNOWLEDGE BASE (FAQ / Guides)
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS knowledge_articles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL DEFAULT 'faq',               -- faq | guide
        category TEXT NOT NULL DEFAULT 'general',
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        keywords TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Seed starter FAQ so the Help screen isn't empty on fresh installs
    cur.execute("SELECT COUNT(*) FROM knowledge_articles")
    if cur.fetchone()[0] == 0:
        for title, body, kw in [
            ("How do forwards work?",
             "Create a project, add source channels you control and destination "
             "channels. New posts in sources are automatically forwarded or copied.",
             "forward how works project"),
            ("Why connect my account?",
             "Forwarding runs on your own Telegram session. Sessions are encrypted "
             "at rest and never shared between users.",
             "connect account login session"),
            ("How does billing work?",
             "Plans are monthly with discounts on 6/12-month durations. Pay via UPI "
             "(INR) or crypto (USD). Auto-renew can use your wallet balance.",
             "billing payment upi crypto price"),
            ("What are daily limits?",
             "Each plan caps forwards per project per day: Beginner 200, Pro 500, "
             "Creator 1500. Check Plan & Billing for your current usage.",
             "limits daily forward quota"),
        ]:
            cur.execute(
                "INSERT INTO knowledge_articles(kind, category, title, body, keywords) VALUES ('faq','general',?,?,?)",
                (title, body, kw),
            )

    # ==========================
    # FEATURE FLAGS
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS feature_flags(
        key TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 1,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    for flag in ("whatsapp_enabled", "threads_enabled", "ai_enabled",
                 "giveaway_enabled", "multi_account_enabled"):
        cur.execute("INSERT OR IGNORE INTO feature_flags(key, enabled) VALUES (?, 1)", (flag,))

    # ==========================
    # PLATFORM STATUS (admin control plane)
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS platform_status(
        platform TEXT PRIMARY KEY,                      -- telegram|whatsapp_channel|threads
        state TEXT NOT NULL DEFAULT 'enabled',          -- enabled|disabled|maintenance
        note TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    for p in ("telegram", "whatsapp_channel", "threads"):
        cur.execute("INSERT OR IGNORE INTO platform_status(platform, state) VALUES (?, 'enabled')", (p,))

    # ==========================
    # TRANSLATIONS (multi-language)
    # ==========================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS translations(
        key TEXT NOT NULL,
        language TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY(key, language)
    )
    """)

    # Notification preferences (per-user toggles for the Notifications screen).
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_notification_prefs(
        user_id INTEGER NOT NULL,
        pref_key TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(user_id, pref_key)
    )
    """)

    # ==========================
    # SUPPORT AI CHAT HISTORY
    # ==========================================

    cur.execute("""
    CREATE TABLE IF NOT EXISTS support_ai_sessions(
        user_id INTEGER PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        message_count INTEGER DEFAULT 0
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS support_ai_messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        role TEXT NOT NULL,                -- 'user' or 'assistant'
        content TEXT NOT NULL,
        model_used TEXT,
        tokens_in INTEGER DEFAULT 0,
        tokens_out INTEGER DEFAULT 0,
        latency_ms INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_support_ai_user ON support_ai_messages(user_id, id DESC)")

    # Late schema migrations - all tables now exist, so we can safely
    # add columns that may have been missed in earlier versions.
    _migrate_existing_schema(cur)

    # PRD hardening migrations run only after every legacy table exists.
    from database.hardening import migrate
    migrate(cur)

    conn.commit()
    conn.close()
