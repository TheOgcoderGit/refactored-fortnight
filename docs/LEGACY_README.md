# ChannelFlow AI

A personal Telegram auto-forward tool. One Telegram account (yours), managed
through a Bot-API front end, forwards/copies messages between chats that
account is actually a member of / admin of.

## Latest fix (why forwarding wasn't working)

Every forward attempt was crashing before it ever reached Telegram:
`client.forward_messages()` was called with a `noforwards` keyword that this
Telethon build doesn't accept, so every single send raised a `TypeError`
that got silently logged as a generic "failed" entry. This is why sources
and destinations added/started fine but nothing ever showed up on the
destination side. Fixed, plus these additions so the same class of problem
can never go silent again:

- **Real-time alerts**: if forwarding to a destination keeps failing, or the
  forward engine itself crashes, you (or the admins) get a Telegram DM about
  it immediately — not just a line in 📜 Logs.
- **🧪 Test button**: on each destination, and a "Test All Destinations"
  button per project — sends one real message right now and tells you
  pass/fail with the actual reason, instead of waiting for a real post to
  silently fail.
- **Permission check on add**: adding a destination now checks whether the
  account can actually post there and warns immediately if not, instead of
  only failing later.
- **Clearer Start/Stop**: the project card now shows just the one button
  that applies to its current state (▶ Start when stopped, ⏹ Stop when
  running).
- Real error messages (not a generic "Invalid Channel") when adding a
  source/destination fails for a Telegram-side reason (rate limit, etc).

## Scope of this rebuild

This version keeps the architecture your project already had — a single
Telethon session powering the forward engine, controlled through a
python-telegram-bot chat interface — and hardens/completes it:

**Added / fixed:**
- Single shared Telethon client (the previous code opened two separate
  clients on the same `.session` file, which can cause "database is locked"
  crashes under load).
- Per-project settings: Forward vs Copy mode, silent sends, protect content,
  keep-albums-together, fixed/random delay.
- Filter engine: media type filter, keyword whitelist/blacklist, regex filter.
- Enable/disable individual sources and destinations without deleting them.
- Retry with Telegram FloodWait handling and exponential backoff.
- Media-group ("album") batching so multi-photo posts forward as one album.
- Per-project logs and stats, viewable from the bot.
- Auto-reconnect + auto-restart of the forward engine on crash.
- WAL-mode SQLite, foreign keys enforced, indexes, safe in-place schema
  migration for your existing `channelflow.db`.
- `/admin` dashboard (users count, engine status, maintenance mode, broadcast)
  gated to the Telegram user ids listed in `ADMIN_IDS`.
- Global error handler so one bad update can't crash the bot process.

**Deliberately NOT included**, and why:
- **Per-bot-user Telegram account onboarding (phone/OTP/2FA collected via bot
  chat).** Turning this into a service where many different Telegram users
  each log their own account into your bot is the pattern Telegram's abuse
  systems treat as a userbot farm, and it's the architecture behind a lot of
  session-theft bots. This build stays single-account: you authorize once,
  from your own terminal, with `python -m core.authorize`.
- **Affiliate link replacement, invite-link/username/hashtag/mention
  stripping.** These rewrite or remove the parts of a message that show
  where it came from, which functions as attribution stripping / content
  republishing regardless of framing. Not implemented.
- **Bypassing a source chat's "protect content" setting.** The `protect
  content` toggle in Settings applies to *your own* outgoing messages
  (stops others from re-forwarding what you send) — it does not, and
  cannot, unlock protected content coming from someone else's channel.

## One-time setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Get your own Telegram API credentials from https://my.telegram.org
   (API_ID, API_HASH), and a bot token from @BotFather (BOT_TOKEN).

3. Copy `.env.example` to `.env` and fill in your values, including
   `ADMIN_IDS` (your own Telegram user id, comma-separated if more than one).

4. Log the Telegram account that will do the forwarding into its local
   session file (interactive, one time only):
   ```
   python -m core.authorize
   ```

5. Create the database schema:
   ```
   python -m database.schema
   ```

6. Start the bot:
   ```
   python main.py
   ```

## Using it

- `/start` — opens the main menu (New Project / My Projects / Status / Settings)
- Create a project, add sources and destinations by username
  (the account must already be a member/admin of each chat you add)
- On each project: **⚙ Forward Settings** (mode, silent, protect content,
  albums, delay) and **🧹 Filters** (media type, keyword lists, regex)
- **📊 Stats** and **📜 Logs** per project
- `/admin` — dashboard for the user ids listed in `ADMIN_IDS`

## Notes on your existing data

Your existing `channelflow.db` and `.session` file are compatible with this
version — `python -m database.schema` (or just starting the bot) will add
the new columns/tables to your existing database in place without touching
your existing projects, sources, or destinations. Copy your existing `.env`
and `<SESSION_NAME>.session` file into this folder rather than typing your
credentials in fresh, if you'd rather not re-authorize.
