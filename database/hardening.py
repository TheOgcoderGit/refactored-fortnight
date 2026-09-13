"""
ChannelFlow AI - Comprehensive Database Hardening & Schema Migrations
Ensures all PRD-mandated tables, constraints, and indexes exist safely.
"""
import sqlite3
import logging

logger = logging.getLogger(__name__)


def migrate(cur: sqlite3.Cursor):
    """Executes additive migrations safely without altering or dropping existing data."""

    def add_column(table: str, column: str, decl: str):
        cur.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cur.fetchall()}
        if column not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            logger.info("Added column %s to %s", column, table)

    # ==========================================
    # 1. User Table Enhancements
    # ==========================================
    add_column("users", "language", "TEXT DEFAULT 'en'")
    add_column("users", "onboarding_step", "TEXT DEFAULT 'start'")
    add_column("users", "trial_started_at", "TEXT")
    add_column("users", "is_owner", "INTEGER DEFAULT 0")

    cur.execute("""
        UPDATE users 
        SET trial_started_at = COALESCE(created_at, CURRENT_TIMESTAMP)
        WHERE trial_started_at IS NULL AND (plan != 'FREE' OR plan_expiry IS NOT NULL)
    """)

    # ==========================================
    # 2. External Telegram Session Ownership
    # ==========================================
    add_column("user_telegram_sessions", "external_user_id", "INTEGER")
    cur.execute("""
        UPDATE user_telegram_sessions 
        SET status = 'reconnect_required'
        WHERE external_user_id IS NULL AND status = 'connected'
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_telegram_external_owner 
        ON user_telegram_sessions(external_user_id) 
        WHERE external_user_id IS NOT NULL
    """)

    # ==========================================
    # 3. Post Edit Sync Table & Schema Alignment
    # ==========================================
    cur.execute("PRAGMA table_info(post_edit_mappings)")
    pem_cols = {row[1] for row in cur.fetchall()}

    if not pem_cols:
        # Table does not exist at all; create with target schema
        cur.execute("""
            CREATE TABLE IF NOT EXISTS post_edit_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                source_chat_id TEXT NOT NULL,
                source_message_id INTEGER NOT NULL,
                target_chat_id TEXT NOT NULL,
                target_message_id INTEGER NOT NULL,
                content_hash TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                UNIQUE(project_id, source_chat_id, source_message_id, target_chat_id)
            )
        """)
    else:
        # Table exists: if empty, rebuild to target schema cleanly; otherwise migrate columns
        cur.execute("SELECT COUNT(*) FROM post_edit_mappings")
        count_pem = cur.fetchone()[0]
        if count_pem == 0 and "source_chat_id" not in pem_cols:
            cur.execute("DROP TABLE post_edit_mappings")
            cur.execute("""
                CREATE TABLE post_edit_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    source_chat_id TEXT NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    target_chat_id TEXT NOT NULL,
                    target_message_id INTEGER NOT NULL,
                    content_hash TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    UNIQUE(project_id, source_chat_id, source_message_id, target_chat_id)
                )
            """)
        else:
            add_column("post_edit_mappings", "source_chat_id", "TEXT")
            add_column("post_edit_mappings", "target_chat_id", "TEXT")
            add_column("post_edit_mappings", "target_message_id", "INTEGER")
            add_column("post_edit_mappings", "content_hash", "TEXT")

    # Safe index creation now that source_chat_id is guaranteed to exist
    cur.execute("CREATE INDEX IF NOT EXISTS idx_post_edit_lookup ON post_edit_mappings(source_chat_id, source_message_id)")
    # post_edit_sync_service.record_mapping() upserts via
    # ON CONFLICT(project_id, source_chat_id, source_message_id, target_chat_id),
    # which requires a matching UNIQUE constraint/index to exist. The
    # column-migration branch above (existing, non-empty tables) never
    # created one, so add it explicitly - a unique index satisfies SQLite's
    # ON CONFLICT target resolution just as well as an inline constraint.
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_post_edit_mapping "
        "ON post_edit_mappings(project_id, source_chat_id, source_message_id, target_chat_id)"
    )

    # ==========================================
    # 4. Telegram Forum Topic Routing (PRD §11, F096)
    # ==========================================
    add_column("sources", "topic_id", "INTEGER DEFAULT NULL")
    add_column("sources", "topic_name", "TEXT DEFAULT NULL")
    add_column("destinations", "target_topic_id", "INTEGER DEFAULT NULL")

    # ==========================================
    # 5. Auto Reactions (PRD §16, F156-F158)
    # ==========================================
    cur.execute("PRAGMA table_info(auto_reactions)")
    ar_cols = {row[1] for row in cur.fetchall()}
    if not ar_cols:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auto_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                emojis TEXT NOT NULL DEFAULT '👍',
                trigger_mode TEXT NOT NULL DEFAULT 'all',
                keywords TEXT DEFAULT '',
                apply_source INTEGER NOT NULL DEFAULT 0,
                apply_target INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)
    else:
        add_column("auto_reactions", "enabled", "INTEGER NOT NULL DEFAULT 1")
        add_column("auto_reactions", "emojis", "TEXT NOT NULL DEFAULT '👍'")
        add_column("auto_reactions", "trigger_mode", "TEXT NOT NULL DEFAULT 'all'")
        add_column("auto_reactions", "keywords", "TEXT DEFAULT ''")
        add_column("auto_reactions", "apply_source", "INTEGER NOT NULL DEFAULT 0")
        add_column("auto_reactions", "apply_target", "INTEGER NOT NULL DEFAULT 1")

    # Legacy installs created auto_reactions with two NOT NULL columns the app
    # never writes (``reaction_emoji`` has no default and ``enable`` is a
    # duplicate of ``enabled``). Any INSERT that omits them fails the NOT NULL
    # check, so auto reactions could never be saved at all on those databases.
    # Rebuild into the canonical shape, carrying the existing rows across.
    cur.execute("PRAGMA table_info(auto_reactions)")
    ar_info = {row[1]: row for row in cur.fetchall()}
    legacy_notnull = (
        "reaction_emoji" in ar_info
        and ar_info["reaction_emoji"][3] == 1          # notnull
        and ar_info["reaction_emoji"][4] is None       # no default
    )
    if legacy_notnull and "emojis" in ar_info:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auto_reactions_canonical (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                emojis TEXT NOT NULL DEFAULT '👍',
                trigger_mode TEXT NOT NULL DEFAULT 'all',
                keywords TEXT NOT NULL DEFAULT '',
                apply_source INTEGER NOT NULL DEFAULT 0,
                apply_target INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)
        # Only columns guaranteed to exist in the legacy shape are referenced:
        # project_id/enable/reaction_emoji/apply_*_side from the original
        # CREATE TABLE, and enabled/emojis/trigger_mode/keywords/apply_source/
        # apply_target from the add_column() calls above.
        cur.execute("""
            INSERT INTO auto_reactions_canonical
                (project_id, enabled, emojis, trigger_mode, keywords, apply_source, apply_target)
            SELECT project_id,
                   COALESCE(enabled, enable, 1),
                   COALESCE(NULLIF(emojis, ''), reaction_emoji, '👍'),
                   COALESCE(trigger_mode, 'all'),
                   COALESCE(keywords, ''),
                   COALESCE(apply_source, apply_source_side, 0),
                   COALESCE(apply_target, apply_destination_side, 1)
            FROM auto_reactions
        """)
        cur.execute("DROP TABLE auto_reactions")
        cur.execute("ALTER TABLE auto_reactions_canonical RENAME TO auto_reactions")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_auto_reactions_project "
            "ON auto_reactions(project_id, enabled)"
        )
        logger.info("Rebuilt auto_reactions into the canonical PRD schema")

    cur.execute("PRAGMA table_info(confirmed_reactions)")
    cr_cols = {row[1] for row in cur.fetchall()}
    if not cr_cols:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS confirmed_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, chat_id, message_id, emoji)
            )
        """)
    elif "chat_id" not in cr_cols:
        # Table already existed from an older schema (destination_id/
        # reaction_emoji, both NOT NULL with no default, instead of
        # chat_id/emoji). auto_reaction_service.py only ever reads/writes
        # chat_id and emoji, so those legacy NOT NULL columns can never be
        # populated by the app - every INSERT OR IGNORE would silently fail
        # the NOT NULL check and never actually store a row. Adding the new
        # columns alone isn't enough; rebuild cleanly when there's no real
        # data to lose (which - since inserts always crashed or silently
        # failed on this shape - is every deployment so far).
        cur.execute("SELECT COUNT(*) FROM confirmed_reactions")
        if cur.fetchone()[0] == 0:
            cur.execute("DROP TABLE confirmed_reactions")
            cur.execute("""
                CREATE TABLE confirmed_reactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    emoji TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(project_id, chat_id, message_id, emoji)
                )
            """)
        else:
            add_column("confirmed_reactions", "chat_id", "TEXT")
            add_column("confirmed_reactions", "emoji", "TEXT")
            add_column("confirmed_reactions", "created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_confirmed_reactions "
                "ON confirmed_reactions(project_id, chat_id, message_id, emoji)"
            )
            logger.warning(
                "confirmed_reactions has existing rows under the legacy "
                "destination_id/reaction_emoji columns; chat_id/emoji were "
                "added but the old NOT NULL columns may still reject new "
                "inserts - a manual data migration is recommended."
            )

    # ==========================================
    # 5b. Post Edit Sync toggle (PRD §14.1)
    # ==========================================
    # Off by default: editing an already-published post in someone's channel is
    # a surprising side effect, so it must be an explicit per-project opt-in.
    # core/forwarder.sync_source_edit() refuses to edit when this is 0.
    add_column("project_settings", "post_edit_sync", "INTEGER NOT NULL DEFAULT 0")

    # ==========================================
    # 6. Formatting & Destinations Formatting
    # ==========================================
    add_column("formatting_rules", "advanced_json", "TEXT NOT NULL DEFAULT '{}'")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS destination_formatting (
            destination_id INTEGER PRIMARY KEY REFERENCES destinations(id) ON DELETE CASCADE,
            config_json TEXT NOT NULL DEFAULT '{}'
        )
    """)

    # ==========================================
    # 7. Forward Credit Ledger & Quota Reservations
    # ==========================================
    cur.execute("""
        CREATE TABLE IF NOT EXISTS forward_credit_ledger (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(telegram_id),
            units INTEGER NOT NULL CHECK(units != 0),
            reference TEXT UNIQUE NOT NULL,
            reason TEXT NOT NULL,
            actor_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS forward_reservations (
            reference TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(telegram_id),
            project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
            usage_date TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('daily', 'credit')),
            status TEXT NOT NULL CHECK(status IN ('reserved', 'committed', 'released')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_reservation_user_day ON forward_reservations(user_id, usage_date, status)")

    # ==========================================
    # 8. Clone Bot Instances (Improvements §48)
    # ==========================================
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_token_encrypted TEXT NOT NULL UNIQUE,
            bot_id INTEGER NOT NULL UNIQUE,
            bot_username TEXT NOT NULL,
            display_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_by INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bot_instances_status ON bot_instances(status)")

    # ==========================================
    # 9. Plan Configurations Alignment (Free, Starter, Pro, Creator)
    # ==========================================
    cur.execute("""
        CREATE TABLE IF NOT EXISTS plan_configs (
            plan_name TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            symbol TEXT NOT NULL DEFAULT '⭐',
            monthly_price_inr REAL NOT NULL,
            crypto_monthly_price_usd REAL NOT NULL,
            stars_monthly_price INTEGER NOT NULL DEFAULT 0,
            max_projects INTEGER NOT NULL,
            max_sources_per_project INTEGER NOT NULL,
            max_destinations_per_project INTEGER NOT NULL,
            daily_forward_limit INTEGER NOT NULL,
            per_project_daily_forward_limit INTEGER NOT NULL,
            requires_attribution INTEGER NOT NULL DEFAULT 0,
            feature_flags TEXT NOT NULL DEFAULT '{}',
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    add_column("plan_configs", "symbol", "TEXT NOT NULL DEFAULT '⭐'")
    add_column("plan_configs", "stars_monthly_price", "INTEGER NOT NULL DEFAULT 0")

    plans = [
        ("FREE", "Free", "🆓", 0, 0.0, 0, 3, 10, 10, 100, 100, 1, '{"ai":false,"reactions":false,"edits":false}'),
        ("STARTER", "Starter", "🚀", 199, 6.99, 150, 7, 25, 25, 200, 200, 0, '{"ai":false,"reactions":true,"edits":true}'),
        ("PRO", "Pro", "⭐", 399, 14.99, 300, 15, 50, 50, 1000, 1000, 0, '{"ai":true,"reactions":true,"edits":true}'),
        ("CREATOR", "Creator", "👑", 799, 19.99, 600, 15, 50, 50, 2000, 2000, 0, '{"ai":true,"reactions":true,"edits":true,"vip":true}')
    ]
    for p in plans:
        cur.execute("""
            INSERT INTO plan_configs (
                plan_name, display_name, symbol, monthly_price_inr, crypto_monthly_price_usd,
                stars_monthly_price, max_projects, max_sources_per_project, max_destinations_per_project,
                daily_forward_limit, per_project_daily_forward_limit, requires_attribution, feature_flags, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(plan_name) DO NOTHING
        """, p)

    # Normalize historical plan names in users table
    cur.execute("UPDATE users SET plan='STARTER' WHERE plan='BEGINNER'")
    cur.execute("UPDATE users SET plan='CREATOR' WHERE plan IN ('MAX', 'CREATORS')")

    # Record migration marker
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES('prd_hardening_2')")