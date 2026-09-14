# ChannelFlow AI — partial PRD hardening release

**Not production-ready. Not every PRD feature has been implemented.**
This is an incremental update to the supplied ChannelFlow_Bot_ForAstar.zip,
not a rewrite or a release fetched from GitHub. Read PROJECT_TRACKER.md first.

## What changed
- FLOW12345 / FLOW 12345 parsing is wired into the existing login handler; bare numbers are rejected as OTP input.
- Login uses a valid monotonic timer, per-user operation locks, attempt identifiers, expiry, retry limits and cleanup.
- Successful login captures get_me().id and transactionally claims external account ownership using a unique SQLite index.
- Legacy sessions without an external account ID are retained but marked reconnect_required. Explicit disconnect stops projects and removes the session/ownership.
- Registration grants one 7-day Creator trial with a durable one-time stamp. Effective expiry returns users to Free.
- Forwarding fixes: owner lookup, duplicate quota debit, AI + formatting composition, private-chat guard, bounded FloodWait retry and protected-source checks.
- Global daily quota across projects, daily-first extra credits, idempotent grants, same-day rollback and a persistent reservation ledger. No money is charged per forward.
- Advanced formatting: link preview flag, username/link removal, hidden-link entity stripping, monospace, word/line trimming and task/destination headers/footers.
- Primary navigation and critical project callbacks; formatting/preview/credit/ticket commands.
- Source resolution and test sends use the owning Telegram account, not an unrelated shared operator session.

## Important release limitations
- Stars, UPI and Crypto checkout/approval are deliberately disabled pending transaction/provider verification. Legacy service code is preserved behind explicit failure guards, not claimed complete.
- Paid credit-package purchases are not implemented. Only audited admin credit grants/revocations are available through /grantcredits.
- WhatsApp/Threads external workers and pairing HTTP server default OFF. Existing bridge/media paths are not production verified. Experimental opt-in is for developer testing only.
- Post Edit Sync, forum/topic routing and auto reactions are NOT implemented in this release.
- AI/affiliate/watermark services are preserved; their missing setup controls are hidden rather than presented as ready.
- Full localization, milestone/payment/reward accounting, all admin/legacy callbacks, production recovery and media-album ordering still need work.
- Unknown network outcomes retain delivery claims/reservations for operator reconciliation. Automatic crash replay cannot honestly guarantee exactly-once network delivery.
- Telegram API, real account login, payment providers, WhatsApp and Pydroid have NOT been tested live here.

## Install (target: Pydroid 3 / Android)
1. Back up your complete old folder, database and encryption key. Do not overwrite the only copy.
2. Extract this folder into a separate test location. Do not start with your production DB.
3. Copy .env.example to .env. Set BOT_TOKEN, API_ID, API_HASH and SESSION_ENCRYPTION_KEY locally. Never send credentials in chat.
4. Generate a new key only for a fresh deployment:
   `python -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`
   **For an existing encrypted database retain its existing key.**
5. Run `python -m pip install -r requirements.txt` in the same Pydroid interpreter.
6. Run `python utils/preflight.py`, then `python -m compileall .`.
7. Run `python -m unittest discover -s tests -v` for offline regression tests.
8. Run `python main.py`, open the bot privately, and follow the live acceptance checklist in docs/LIVE_ACCEPTANCE.md.

Dependency installation was attempted here but package-host DNS was unavailable.
The sandbox did not have Telegram/Telethon/aiohttp and other required dependencies;
clean install and production module import/startup are NOT verified.
Pydroid may need its repository plugin/prebuilt wheels for native dependencies.

## User flow
/start → one 7-day Creator trial → /connect +countrycodephone → FLOW12345 → optional 2FA.
A numeric message without FLOW is not a login code. Use /cancel to abort.
Treat this bot as a trusted account-access service: Telegram sessions confer broad account access.
Create a project from Projects, add source/destination, test, then start.
Plan limits: Free 100/day, Beginner 200/day, Pro 1000/day, Creator 2000/day by default.
Known legacy default Pro/Creator limits are migrated once; customized limits are retained.

## Advanced formatting (Copy mode)
Open Project → Edit Project → Formatting → Advanced cleanup / preview.
Example:
`/format 10 {"remove_usernames":true,"link_preview":false,"header":"Deals","mono":true}`
Per-destination override using the destination database row ID:
`/format 10:100 {"header":"Target-specific header","footer":"Thanks"}`
Preview saved formatting with `/preview 10 sample text` or `/preview 10:100 sample text`.
This is a local formatting preview, not an AI/provider call or a live destination test.
Available keys: link_preview, remove_usernames, remove_links, disable_hidden_links, mono,
remove_first_words, remove_last_words, remove_first_lines, remove_last_lines,
keep_first_words, keep_first_lines, header, footer.
null header/footer inherits legacy prefix/suffix. null keep_* means no keep limit.
Word trimming normalizes whitespace; line trimming preserves remaining line breaks.

Ordering preserves the old pipeline deliberately: text replacement → affiliate → AI →
advanced cleanup → remove/replace patterns → header/footer → entities → link-preview send option.
Find/replace does not corrupt protected URL spans. Explicit Remove Links intentionally removes URLs.
Formatting is opt-in and only applies to Copy mode. Overlength output is rejected and logged,
not silently truncated. A requested noforwards flag unsupported by the installed Telethon API fails safely.

## Credits and tickets
- /credits: your extra forward unit balance.
- /grantcredits USER_ID UNITS UNIQUE_REFERENCE reason: ADMIN_IDS only; idempotent unit grant/revoke, no money movement.
- /ticket subject | message: create a ticket, maximum five per hour in this UI.
- /tickets, /ticketview ID, /ticketreply ID message, /ticketclose ID, /ticketreopen ID.
Ticket reads exclude internal notes. User operations check ownership. Admin support reply/notification needs live testing.
Updates: https://t.me/BotFoundrry. Set SUPPORT_GROUP_URL for your support group.

## Owner and admin access

These are two different roles and they are not interchangeable.

**Admins** (`ADMIN_IDS`, comma-separated Telegram user IDs) get `/admin`:
support tickets, broadcasts, coupons and user management.

**The owner** (`OWNER_ID`, a single Telegram user ID) additionally gets
`/owner`, which opens the payment approval queue, the full userbase and the
clone network. Being in `ADMIN_IDS` does **not** grant `/owner` — the two
are checked separately on purpose.

`/owner` is protected by a three-step challenge: your Telegram ID, then a
username, then a password and a security answer. The session lasts one
hour and is held in memory, so restarting the bot requires signing in
again.

Set these in `.env` to enable it:

```
OWNER_ID=123456789
OWNER_USERNAME=your-owner-username
OWNER_PASSWORD_HASH=<sha256 of your password>
OWNER_SECURITY_ANSWER_HASH=<sha256 of your security answer>
```

Generate a hash with:

```
python -c "import hashlib;print(hashlib.sha256(b'your-value').hexdigest())"
```

`/owner` refuses to start the challenge until all four are set, and tells
you which are missing — an unconfigured challenge cannot be completed,
because every answer hashes to something that never matches an empty
string. If `OWNER_ID` itself is unset, `/owner` says so rather than
telling the owner they are unauthorised.

Values may carry surrounding whitespace, quotes or a trailing `# comment`;
the parser tolerates all three. A malformed value is ignored and logged,
which is better than silently becoming `None` and locking you out.

## Database and recovery
SQLite WAL with foreign keys and additive migrations. UTC daily accounting, one unit per source dispatch,
shared across its destinations. Original databases are never bundled.
Reserved/claimed rows left by an ambiguous send must be inspected before manual retry/refund;
never delete them wholesale. Confirm destination receipt first. No automatic reconciliation UI yet.
Project deletion retains reservation history so deleting projects cannot reset quota.
Unknown legacy external account identities require reconnect; they are never guessed.

## Verification scope
The included tests execute real SQLite transactions and transformation code.
Runtime contract tests use explicit Telegram/encryption/provider doubles — they are not live integration tests.
See docs/TEST_RESULTS.txt for executed results and docs/BASELINE_AUDIT.md for the original audit.
