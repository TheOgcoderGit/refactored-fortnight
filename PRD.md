# ChannelFlow AI --- Product Requirements Document (PRD)

## Complete Reconstruction, Integration, Hardening & Verification Specification

**Document Type:** Master Product Requirements Document\
**Product:** ChannelFlow AI\
**Purpose:** This document is the single source of truth for rebuilding,
completing, integrating, fixing, improving, and verifying the
ChannelFlow AI Telegram bot.\
**Baseline:** Latest available
`channelflow_bot.zip` plus the
requirements  during the ChannelFlow project conversations.\
**Primary runtime target:** Pydroid 3 / Android, started with
`python main.py`.

------------------------------------------------------------------------

# 1. Executive Summary

ChannelFlow AI is a Telegram-based content forwarding and automation
platform. A user connects their Telegram account, creates forwarding
projects, configures sources and destinations, applies optional
transformations such as AI rewriting, formatting, filters, watermarking
and affiliate replacement, and runs the project continuously.

The product must behave like a real multi-user production service, not
like a collection of partially connected handlers.

The current codebase contains useful foundations and services, but
several areas are incomplete or inconsistently wired. The most important
architectural problem is that some features have a UI keyboard and/or
backend service but do not have a complete
`UI -> callback/command -> handler -> service -> database -> runtime -> test`
chain.

This PRD therefore requires a systematic completion rather than random
bug patching.

## Core principle

A feature is **NOT IMPLEMENTED** merely because a function, class,
keyboard, database table, or service exists.

A feature is considered **VERIFIED** only when:

1.  Its data model exists and migrates correctly.
2.  Its service/business logic exists.
3.  Its command/callback handler exists.
4.  Its UI exists where required.
5.  The handler actually calls the service.
6.  The service changes/reads the correct database records.
7.  Runtime behaviour works.
8.  Error and cancellation paths work.
9.  Multi-user isolation is preserved.
10. Automated and runtime tests pass.

------------------------------------------------------------------------

# 2. Non-Negotiable Product Decisions

These rules are locked unless a future PRD explicitly changes them.

## 2.1 Payment methods

Exactly three payment methods:

-   Telegram Stars
-   UPI
-   Crypto

Do not introduce additional payment methods.

## 2.2 Billing model

ChannelFlow must NOT charge users for each forward.

Forbidden:

-   per-forward INR debit
-   per-forward USD debit
-   per-forward wallet deduction
-   "₹X per message" billing

The monetization model is:

1.  Subscription plan with daily forwarding limits.
2.  Optional extra-forward credit packages.

## 2.3 Plans

Target plan limits:

  Plan                   Daily Forward Limit
  -------------------- ---------------------
  Free                                   100
  Starter / Beginner                     200
  Pro                                   1000
  Creator                              2000+

The exact Creator limit can be configurable by admin, but it must be
greater than or equal to the defined Creator baseline.

Plan prices are configurable by admin/database. Do not hard-code
commercial prices into business logic.

## 2.4 Trial

Every new user receives a 7-day Creator trial.

Rules:

-   Trial starts according to the configured onboarding policy.
-   Trial is not a paid subscription.
-   Trial conversion does not count as a paid referral conversion.
-   Trial must expire automatically.
-   Trial expiry must update entitlements.
-   A user must not receive unlimited repeated trials by restarting the
    bot.

## 2.5 Final primary navigation

The user-facing product should converge on:

-   Projects
-   Subscription
-   Rewards
-   Account

Support and Settings should be accessible consistently without mixing
legacy and new navigation systems.

## 2.6 Deferred feature

Clone Bot Network / Phase 10.2 is deferred.

Do not implement it in this reconstruction unless explicitly requested
later.

Do not show a fake Clone feature that appears functional if it is not
implemented.

------------------------------------------------------------------------

# 3. Product Goals

## 3.1 Primary goals

-   Reliable Telegram account connection.
-   Secure session handling.
-   One external Telegram account cannot be simultaneously owned by
    multiple ChannelFlow users.
-   Easy project creation.
-   Reliable Telegram -\> Telegram forwarding.
-   Reliable Telegram -\> WhatsApp forwarding.
-   Production-grade quota and credit accounting.
-   Three payment methods.
-   Referral/reward system.
-   AI rewriting.
-   Content filtering.
-   Formatting.
-   Watermarking.
-   Affiliate replacement.
-   Support/ticket system.
-   Admin management.
-   Multi-user isolation.
-   Recovery after process restart.
-   Pydroid 3 compatibility.
-   No dead buttons.
-   No misleading "implemented" status.

## 3.2 Quality goals

The system must be:

-   deterministic
-   idempotent
-   transaction-safe
-   concurrency-safe
-   secure
-   recoverable
-   observable
-   user-friendly
-   maintainable

------------------------------------------------------------------------

# 4. Product Architecture Principle

Every user-facing feature must follow this chain:

``` text
UI
 |
 | command / callback
 v
Handler
 |
 v
Service / Business Logic
 |
 v
Database
 |
 v
Runtime / External API
 |
 v
User-visible result
```

For a forwarding feature:

``` text
Incoming Telegram message
        |
        v
Project lookup
        |
        v
Deduplication
        |
        v
Quota reservation
        |
        v
Content filters
        |
        v
AI rewriting (optional)
        |
        v
Affiliate replacement (optional)
        |
        v
Formatting
        |
        v
Watermark/media processing
        |
        v
Destination send
        |
        v
Usage accounting + logs
```

Every stage must have defined failure behaviour.

------------------------------------------------------------------------

# 5. P0 --- Telegram Account Connection

## 5.1 User journey

The connection flow must be:

``` text
/connect +919876543210
        |
        v
Validate phone number
        |
        v
Start Telegram client/login attempt
        |
        v
Telegram sends OTP
        |
        v
Bot asks user to send FLOW<OTP>
        |
        v
User sends FLOW94563
        |
        v
Extract OTP
        |
        v
Submit OTP
        |
        +---- 2FA required? ---- YES ---> Ask password
        |                                   |
        |                                   v
        |                              Submit password
        |
        NO
        |
        v
get_me()
        |
        v
Obtain actual Telegram user ID
        |
        v
Claim account ownership atomically
        |
        v
Encrypt/save session
        |
        v
Create/activate connection record
        |
        v
Start/resume project engine if applicable
        |
        v
Connected
```

## 5.2 `FLOW` command

The required command is `FLOW`.

Examples:

``` text
FLOW94563
FLOW 94563
```

Both should be accepted.

The old `/myflow` login-code convention must not remain as the primary
documented flow.

## 5.3 OTP isolation

A numeric message such as:

``` text
94563
```

must not automatically be treated as a login code.

Only an active login attempt belonging to the same ChannelFlow user may
consume `FLOW`.

If no login attempt exists:

``` text
No active Telegram login attempt.
Please use /connect first.
```

## 5.4 Login attempt state

Use an explicit state model:

-   IDLE
-   WAITING_CODE
-   WAITING_2FA
-   CONNECTED
-   FAILED
-   EXPIRED
-   CANCELLED

State must be scoped by ChannelFlow user ID and login attempt ID.

Do not use unsafe global state shared across users.

## 5.5 OTP expiry

Each login attempt must expire after a configurable period.

Expired attempts must:

-   reject old `FLOW`
-   clean temporary login state
-   disconnect temporary client if needed
-   tell the user to start again

## 5.6 OTP retry

Wrong OTP:

-   show a clear error
-   allow retry while the attempt is valid
-   enforce attempt limits
-   do not reveal internal exception details

## 5.7 2FA

If Telegram requests a password:

``` text
OTP accepted.
Your Telegram account has 2-step verification enabled.
Please enter your Telegram password.
```

Password must:

-   never be logged
-   never be stored as plaintext
-   never be included in analytics
-   be cleared from temporary state after use

## 5.8 Current known login bug

The existing connection implementation has a `time`/`_time` naming
mismatch around monotonic-time usage in session handling. This must be
corrected and covered by a test so OTP finalization cannot fail after a
valid OTP.

## 5.9 Error transparency

Users should receive safe, useful errors.

Example:

``` text
❌ Telegram login could not be completed.

Please start /connect again.
```

Internal logs should retain the sanitized exception class/message for
debugging, but never expose secrets.

------------------------------------------------------------------------

# 6. P0 --- Telegram External Account Ownership

This is a mandatory security and data-integrity feature.

## 6.1 Distinguish two IDs

There are two identities:

1.  ChannelFlow user ID --- the owner of the SaaS account.
2.  Telegram user ID --- the actual external Telegram account returned
    by Telethon `get_me()`.

They must never be confused.

## 6.2 Successful connection

After Telegram login:

``` python
me = await client.get_me()
telegram_user_id = me.id
```

The actual Telegram ID must be stored.

Recommended conceptual record:

``` text
connection_id
channel_flow_user_id
platform = telegram
external_user_id = telegram_user_id
phone
encrypted_session
status
created_at
updated_at
last_seen_at
```

## 6.3 One Telegram account, one active ChannelFlow owner

Example:

``` text
ChannelFlow User A
        |
        +---- Telegram user ID 123456
                     |
                  CONNECTED
```

User B attempts to connect the same Telegram account:

``` text
ChannelFlow User B
        |
        +---- Telegram user ID 123456
                     |
                  REJECT
```

The bot should tell User B that this Telegram account is already
connected to another ChannelFlow account.

Do not reveal the other user's ID, phone number, username, or private
information.

## 6.4 Database enforcement

Do not rely only on:

``` python
if not exists():
    insert()
```

A race condition can still create duplicates.

Use a database-level unique constraint/index appropriate to the
connection lifecycle, for example a uniqueness rule for active Telegram
ownership.

The exact SQL design may use a dedicated active-connection table or a
partial unique index if SQLite version/architecture permits.

## 6.5 Atomic claim

Account claiming must be transactional.

Two simultaneous connection attempts for the same Telegram account must
result in exactly one successful owner.

## 6.6 Same-user duplicate connection

If a user already has the Telegram account connected:

``` text
/connect
```

must not start a second OTP flow.

Show:

``` text
✅ Your Telegram account is already connected.

Use Connected Accounts to manage it, or disconnect it before connecting again.
```

## 6.7 Disconnect releases ownership

When the owner disconnects:

1.  Stop dependent forwarding workers.
2.  Disconnect Telethon client.
3.  Mark connection inactive/disconnected.
4.  Remove or securely invalidate the session according to retention
    policy.
5.  Release the external Telegram ID for future ownership.
6.  Cancel active login state.
7.  Clean temporary resources.

Then another ChannelFlow user may connect that Telegram account.

## 6.8 Reconnect

If a session becomes invalid/expired:

``` text
🔴 Telegram connection needs attention.
[Reconnect]
```

Reconnect must verify ownership before replacing the stored session.

------------------------------------------------------------------------

# 7. P0 --- Session Management

## 7.1 Encryption

Telegram sessions and other sensitive credentials must be encrypted at
rest.

Encryption keys come from secure environment configuration.

## 7.2 Never log secrets

Never log:

-   Telegram OTP
-   Telegram 2FA password
-   session string
-   bot token
-   API hash
-   access token
-   WhatsApp credentials
-   crypto credentials

## 7.3 Restart recovery

After process restart:

``` text
Database
   |
   v
active Telegram connections
   |
   v
load encrypted session
   |
   v
recreate Telethon client
   |
   v
validate session
   |
   v
resume eligible project workers
```

If a session is invalid, mark it as requiring reconnection instead of
crashing the entire bot.

------------------------------------------------------------------------

# 8. P0 --- Final User Navigation

The primary UX should be consistent:

``` text
📁 Projects
💳 Subscription
🎁 Rewards
👤 Account
```

Support and Settings must be reachable consistently.

Do not maintain multiple competing legacy menus.

Every screen should provide appropriate:

-   Back
-   Cancel
-   Home

actions.

No dead-end flows.

------------------------------------------------------------------------

# 9. P0 --- Projects

## 9.1 Projects screen

Example:

``` text
📁 Projects

➕ New Project

🟢 News Forwarder
🟡 WhatsApp Deals
⏸ Test Project
```

Each project should show a clear status.

## 9.2 Project actions

A project may expose:

-   Start
-   Pause
-   Resume
-   Stop
-   Rename
-   Settings
-   Stats
-   Logs
-   Test
-   Delete

Only expose actions valid for the current state.

## 9.3 Project state

Recommended states:

-   DRAFT
-   READY
-   ACTIVE
-   PAUSED
-   DEGRADED
-   ERROR
-   STOPPED
-   DELETED

## 9.4 Project ownership

Every project lookup must validate ownership.

A user must never be able to access another user's project by changing
an ID in a callback.

------------------------------------------------------------------------

# 10. P0 --- Project Creation

Required flow:

``` text
New Project
    |
    v
Project name
    |
    v
Platform / route
    |
    v
Source
    |
    v
Destination
    |
    v
Optional transformations
    |
    v
Test
    |
    v
Activate
```

The UI must not advertise a platform as ready unless its complete
connection and forwarding path has been verified.

------------------------------------------------------------------------

# 11. P0 --- Telegram -\> Telegram

This is a core route.

## 11.1 Source

Support the source types that Telegram/Telethon can safely and
legitimately access, such as channels/groups/chats according to account
permissions.

Accept configured identifiers such as:

-   username
-   supported numeric ID
-   supported invite-based identification

Validate the source before activation.

## 11.2 Destination

Validate:

-   destination exists
-   connected Telegram account can post/send
-   permissions are sufficient

## 11.3 Test

Provide:

``` text
🧪 Test
```

The test should perform a real destination capability check or safe test
action, then return:

``` text
✅ Destination is ready
```

or a useful failure reason.

## 11.4 Forwarding

Support, where destination/platform APIs permit:

-   text
-   photo
-   video
-   document
-   audio
-   animation
-   captions
-   links
-   albums

## 11.5 Media cleanup

Downloaded/processed temporary files must be deleted after
success/failure.

------------------------------------------------------------------------

# 12. P0 --- Telegram -\> WhatsApp

This route is a production feature and must not be treated as a
placeholder.

Required flow:

``` text
Create Project
   |
   v
Telegram source
   |
   v
Connect WhatsApp
   |
   v
Pair/authenticate
   |
   v
Verify connection
   |
   v
Select destination
   |
   v
Test
   |
   v
Activate
```

## 12.1 WhatsApp states

Recommended:

-   DISCONNECTED
-   PAIRING
-   CONNECTING
-   CONNECTED
-   DESTINATION_SELECTED
-   READY
-   DEGRADED
-   ERROR

## 12.2 Pairing

Pairing codes must:

-   expire
-   be single-use where appropriate
-   be scoped to the user/connection
-   not be logged
-   have rate limiting

## 12.3 Destination validation

Verify that the connected WhatsApp account can send to the selected
destination.

## 12.4 Reconnect

If the WhatsApp connection drops:

-   mark status
-   notify user when appropriate
-   stop only affected routes
-   allow reconnect
-   avoid crashing unrelated Telegram projects

## 12.5 Disconnect

Disconnect must release the WhatsApp external identity and clean
dependent resources.

## 12.6 Message conversion

Telegram content may need transformation to WhatsApp-compatible content.

At minimum verify:

-   text
-   image
-   video
-   document
-   caption
-   links

Unsupported content must have defined fallback behaviour rather than
silently disappearing.

------------------------------------------------------------------------

# 13. P1 --- Other Platforms

The codebase contains pieces for platforms such as Threads and
Instagram.

A platform may only be presented as an active user-facing option if its
full path is implemented:

``` text
Connect
 -> Authenticate
 -> Validate
 -> Destination
 -> Test
 -> Forward
 -> Retry
 -> Disconnect
```

If not fully working, hide it or mark it explicitly unavailable.

Never present a half-implemented platform as production-ready.

------------------------------------------------------------------------

# 14. P1 --- Forwarding Pipeline

The processing order must be deterministic.

Recommended:

``` text
Incoming message
      |
      v
Project routing
      |
      v
Deduplication
      |
      v
Quota reservation
      |
      v
Content filters
      |
      v
AI rewrite
      |
      v
Affiliate replacement
      |
      v
Formatting
      |
      v
Watermark/media processing
      |
      v
Destination conversion
      |
      v
Send
      |
      v
Finalize usage + log
```

If a transformation is disabled, skip it.

If an optional transformation fails, follow its defined fallback policy
instead of taking down the project.

## 14.1 Post Edit Sync

A task may enable `Post Edit Sync`.

When enabled, if an already-forwarded source message is edited, ChannelFlow
must update the corresponding destination copy where the destination API and
permissions allow editing.

Required behavior:

``` text
Source message forwarded
 -> store source-message <-> destination-message mapping
 -> source edit event received
 -> verify task is still active/authorized
 -> re-run the configured transformation pipeline
 -> edit the mapped destination message
 -> record success/failure
```

Requirements:

-   Mapping must be scoped by user, task, source, destination, and message ID.
-   Multiple destinations require a mapping for each destination message.
-   Never edit a message belonging to another user/task.
-   If the destination message can no longer be edited, record the failure and
    apply a defined fallback; do not create uncontrolled duplicate posts.
-   Deleting or disabling a task must define how retained mappings are cleaned
    up.
-   Edit events must be idempotent so duplicate events do not corrupt output.

## 14.2 Telegram Forum Topic Forwarding

Support Telegram forum/topic-aware routing where Telegram permissions and API
support allow it.

A task may configure:

-   all topics from a source
-   one or more specific source topics
-   destination topic/thread
-   optional source-topic -> destination-topic mapping

Messages from non-selected topics must not be forwarded by a topic-restricted
task. Topic IDs and routing configuration must be stored per task and validated
against task ownership.

If a configured topic is deleted, inaccessible, or changed, the task should
show a clear warning/error state instead of silently forwarding to the wrong
place.

## 14.3 Auto Reaction

Allow a task to automatically react to eligible Telegram messages where the
connected account, chat, and Telegram API permit reactions.

Configuration should support:

-   Enable/Disable
-   one configured reaction or an allowed reaction set
-   source-side and/or destination-side reaction mode where supported
-   optional delay where already supported by the worker architecture

Rules:

-   Only use reactions allowed by the target chat/platform.
-   Reaction failure must not block message forwarding.
-   Rate-limit and retry safely.
-   Do not repeatedly react to the same message because of worker restart or
    duplicate events; reaction actions must be idempotent where practical.
-   If reactions are unsupported or permission is missing, surface a
    user-visible task warning instead of repeatedly failing.

------------------------------------------------------------------------

# 15. P1 --- Content Filters

Filters must be actual runtime rules, not only settings screens.

Support where applicable:

-   keyword allowlist
-   keyword blocklist
-   hashtag rules
-   sender allowlist
-   sender blocklist
-   regex rules
-   domain allowlist
-   domain blocklist
-   minimum text length
-   maximum text length
-   media-type rules

## 15.1 Filter result

A message should resolve to:

-   ACCEPT
-   REJECT
-   ERROR/FALLBACK

Rejected messages should be recorded in appropriate project
statistics/logs without being forwarded.

## 15.2 Regex safety

Invalid regex must:

-   not crash forwarding
-   return a configuration error
-   allow the user to correct the rule

------------------------------------------------------------------------

# 16. P1 --- AI Rewriter

UI:

``` text
🤖 AI Rewriter

Enable/Disable
Tone
Length
Language
Custom Prompt
```

## 16.1 Project scope

AI configuration is project-specific unless explicitly configured
otherwise.

## 16.2 Processing

If enabled:

``` text
Original caption/text
       |
       v
AI prompt construction
       |
       v
AI request
       |
       v
Validate response
       |
       v
Preserve required URLs/entities
       |
       v
Continue pipeline
```

## 16.3 Failure

If AI times out/fails:

-   do not crash forwarding
-   use configured fallback policy
-   record AI failure
-   optionally notify user after repeated failures

## 16.4 Limits

AI usage must respect plan entitlements and any configured usage limits.

------------------------------------------------------------------------

# 17. P1 --- Formatting

Required transformations:

-   Prefix
-   Suffix
-   Find -\> Replace
-   Remove Pattern

Rules should have deterministic ordering.

Example:

``` text
Original
 -> remove unwanted phrase
 -> replace URL/text
 -> add prefix
 -> add suffix
```

Provide a preview/test mode.

Invalid formatting configuration must not crash forwarding.

## 17.1 Advanced Content Cleanup & Presentation

These options are project/task-scoped by default. Where a task has multiple
destinations, a destination may override the task default so the same source
content can be formatted differently for each target.

### Link Preview ON/OFF

Allow the user to control whether Telegram generates a webpage preview for
links in forwarded messages.

-   ON: send the processed message with link preview enabled where Telegram
    supports it.
-   OFF: suppress the webpage preview without removing the URL itself.
-   This setting must not be confused with Remove Links.
-   If the destination/platform does not support link previews, use the
    platform-safe fallback without failing the task.

### Remove Usernames

When enabled, strip Telegram-style username handles such as `@example` from
the outgoing text/caption.

-   Remove handles without damaging surrounding readable text.
-   Do not modify email addresses or unrelated `@` text unless it actually
    matches the configured username rule.
-   Provide preview/test output before activation.

### Remove Links

When enabled, remove visible URLs from outgoing text/captions.

-   Support common `http://`, `https://`, and Telegram/domain URL forms.
-   Clean leftover whitespace/punctuation where safe.
-   This feature is independent from affiliate replacement and hidden-link
    handling.
-   Ordering with affiliate replacement must be deterministic and visible in
    the task configuration.

### Disable Hidden Links

Detect text entities where visible words contain a masked/embedded URL.

When enabled, the system must apply the configured safe behavior instead of
silently preserving an invisible destination. Default behavior:

``` text
Visible text with hidden URL
 -> detect embedded URL entity
 -> remove the hidden hyperlink entity
 -> keep the visible text
```

An implementation may additionally offer a user-selectable `Reveal URL`
mode, but it must not be the default unless explicitly configured.

### Mono Text

When enabled, convert the configured outgoing text/caption into Telegram
monospace/code-style formatting where supported, making copied text easier to
distinguish.

-   Preserve the actual text content.
-   Escape/encode entities correctly for the selected Telegram parse mode.
-   If the destination does not support equivalent formatting, fall back to
    plain text rather than failing forwarding.

### Trim Words / Lines

Provide deterministic trimming controls for unwanted content.

Required modes:

-   remove first N words
-   remove last N words
-   remove first N lines
-   remove last N lines
-   keep first N lines/words where configured

Rules must validate non-negative limits and must not crash on short/empty
messages. The preview/test screen must show the exact resulting content.

### Custom Header / Footer

Allow custom header and footer text to be added to forwarded text/captions.

-   Support task-level defaults.
-   Support per-destination overrides so each target can have a different
    header/footer.
-   Header is inserted before processed content; footer is inserted after it.
-   Existing prefix/suffix behavior may be reused internally, but the
    user-facing UX should expose clear `Header` and `Footer` controls where
    destination-specific configuration is required.
-   Respect Telegram/platform message and caption length limits.
-   Preview the final result before saving.

### Transformation ordering

The exact ordering must be deterministic and covered by tests. The default
content-cleanup order should be documented in the UI/service layer so options
do not produce surprising results. A recommended sequence is:

``` text
Original content
 -> trim words/lines
 -> remove usernames/links/hidden-link entities
 -> existing find/replace and pattern rules
 -> AI/affiliate processing according to task policy
 -> header/footer
 -> mono/entity formatting
 -> link-preview send option
```

Where an existing task explicitly depends on another ordering, preserve
backward-compatible behavior or migrate it explicitly rather than silently
changing output.

------------------------------------------------------------------------

# 18. P1 --- Watermark

Settings:

-   Enable/Disable
-   Text or supported logo mode
-   Size
-   Opacity
-   Position

Watermark processing must be media-aware.

If processing fails:

-   do not corrupt the original
-   use configured fallback
-   clean temporary files
-   record error

------------------------------------------------------------------------

# 19. P1 --- Affiliate Replacer

Supported providers:

-   Amazon
-   Flipkart
-   Meesho
-   Wishlink
-   EarnKaro

The feature must be optional and project-scoped.

Processing:

``` text
Message
 -> detect supported URL
 -> identify provider
 -> generate/resolve affiliate URL
 -> replace safely
 -> continue
```

If provider API is unavailable:

-   do not destroy the original URL
-   log the failure
-   continue with original content unless policy says otherwise

Provider credentials must be isolated and secured.

------------------------------------------------------------------------

# 20. P1 --- Deduplication

The same source message must not be sent multiple times because of:

-   worker restart
-   retry
-   duplicate event
-   reconnect
-   queue reprocessing

A deduplication key should include enough identity to distinguish
unrelated messages while recognizing true duplicates.

Possible conceptual key:

``` text
project_id + source_identity + source_message_id
```

If destination-specific semantics require it, destination identity can
also be included.

------------------------------------------------------------------------

# 21. P1 --- Retry Engine

Transient failures must use bounded retry.

Example:

``` text
Attempt 1
   |
   v
backoff
   |
Attempt 2
   |
   v
backoff
   |
Attempt 3
   |
   v
FAILED
```

Handle known transient conditions such as:

-   temporary network failure
-   FloodWait/rate limit
-   destination temporary failure

Do not retry permanent configuration errors forever.

------------------------------------------------------------------------

# 22. P0 --- Daily Quota

The system must never charge money per forward.

Quota accounting:

``` text
Plan daily allowance
       |
       v
Available?
  |        |
 YES       NO
  |        |
  v        v
Forward  Extra credits?
             |       |
            YES      NO
             |       |
             v       v
          Forward   Reject
```

## 22.1 Atomic quota reservation

If only one forward remains and two messages arrive simultaneously, only
one may consume the final unit.

Quota reservation must be atomic.

Do not use a simple read-then-write sequence that allows:

``` text
A reads 1
B reads 1
A consumes
B consumes
```

------------------------------------------------------------------------

# 23. P1 --- Extra Forward Credits

Extra credits are prepaid forwarding units and are not per-forward
wallet billing.

Example packages:

-   100
-   500
-   1,000
-   5,000
-   10,000

Exact packages and prices should be configurable.

## 23.1 Ledger

Track:

-   purchase
-   grant
-   consumption
-   adjustment
-   refund/rollback if applicable

Each transaction needs an idempotency identifier.

## 23.2 Consumption

Daily plan quota should be consumed according to the product's defined
priority. Recommended priority:

1.  Daily plan allowance.
2.  Extra credits only after daily allowance is exhausted.

The priority must be consistent and visible to the user.

## 23.3 Admin adjustment

Admin can grant/revoke credits with:

-   amount
-   reason
-   admin identity
-   timestamp
-   audit record

------------------------------------------------------------------------

# 24. P1 --- Subscription

Subscription screen:

``` text
💳 Subscription

Current Plan
Expiry
Daily Usage
Extra Credits
Upgrade
Renew
History
```

## 24.1 Plan entitlements

Use a centralized entitlement system.

Avoid scattered conditions such as:

``` python
if plan == "pro":
```

throughout the code.

Prefer conceptual checks:

``` text
has_feature(user, "whatsapp")
has_feature(user, "ai_rewriter")
has_feature(user, "affiliate_replacer")
```

## 24.2 Upgrade

User selects:

``` text
Plan
 -> Duration
 -> Payment method
 -> Payment
 -> Verify
 -> Activate
```

## 24.3 Downgrade

Do not delete user data automatically.

If the new plan has lower limits:

-   preserve existing projects
-   prevent new actions that exceed limits
-   clearly tell user what is restricted
-   define what happens to already-running premium projects
-   do not silently destroy configuration

## 24.4 Expiry

When subscription expires:

-   update entitlement
-   notify user
-   preserve data
-   restrict premium actions
-   follow documented project-running policy

------------------------------------------------------------------------

# 25. P1 --- Payment System

Only:

1.  Telegram Stars
2.  UPI
3.  Crypto

Payment records must be idempotent.

A duplicate payment callback must not grant subscription/credits twice.

## 25.1 Payment states

Recommended:

-   CREATED
-   PENDING
-   VERIFYING
-   PAID
-   FAILED
-   EXPIRED
-   REJECTED
-   REFUNDED

## 25.2 Payment record

Conceptual fields:

``` text
payment_id
user_id
method
currency
amount
plan/package
duration
provider_reference
status
created_at
updated_at
verified_at
```

Never store sensitive provider secrets in payment history.

------------------------------------------------------------------------

# 26. P1 --- Telegram Stars

Support:

-   plan purchase
-   subscription duration
-   extra-credit package purchase
-   successful payment
-   failed payment
-   cancellation/expiry where applicable
-   payment history
-   duplicate callback protection

Stars prices must be configurable in the database/admin system.

------------------------------------------------------------------------

# 27. P1 --- UPI

UPI flow may be manual verification:

``` text
Select plan/package
 -> UPI instructions
 -> User submits payment reference/proof
 -> Pending
 -> Admin verifies
 -> Approved / Rejected
```

Need:

-   duplicate reference detection
-   expiry
-   approve/reject
-   audit log
-   notification
-   payment history

------------------------------------------------------------------------

# 28. P1 --- Crypto

Crypto flow:

``` text
Select plan/package
 -> USD amount
 -> Create invoice
 -> User pays
 -> Provider confirms
 -> Activate
```

Need:

-   invoice expiry
-   payment state tracking
-   duplicate protection
-   confirmation
-   history
-   failure handling

------------------------------------------------------------------------

# 29. P1 --- Referral / Rewards

Final user screen:

``` text
🎁 Rewards

👥 Invited Users
🏆 Milestones
💰 Rewards Earned
🔗 Referral Link
📊 Leaderboard
```

## 29.1 Qualification

A referral qualifies for conversion reward only when the referred user
becomes a qualifying paid customer.

Trial users do not count as paid conversions.

## 29.2 Anti-abuse

Prevent:

-   self referral
-   duplicate conversion reward
-   multiple referrer assignment
-   reward replay
-   milestone replay

## 29.3 First-link-wins

If the product uses first-link attribution, the first valid referrer
must remain associated with the referred user unless an explicit admin
correction is made.

## 29.4 Milestones

Required milestone tiers:

-   50
-   100
-   250
-   500

Rewards stack according to the configured rules.

------------------------------------------------------------------------

# 30. P1 --- Account

Account screen should provide:

``` text
👤 Account

Telegram Account
Connected Accounts
Subscription
Wallet
Rewards
Language
Settings
Disconnect
```

Show connection status without exposing sensitive information.

------------------------------------------------------------------------

# 31. P1 --- Connected Accounts

Unified screen:

``` text
🔗 Connected Accounts

Telegram
🟢 Connected

WhatsApp
🟢 Connected

Threads
⚪ Not connected
```

Each supported account should have:

-   Connect
-   Reconnect
-   Disconnect
-   Status

All external account ownership must be user-scoped and race-safe.

------------------------------------------------------------------------

# 32. P1 --- Wallet

Wallet is not a per-forward billing mechanism.

It may represent:

-   eligible monetary rewards
-   supported balances
-   credit-related transactions where explicitly designed

Keep currencies separated:

-   INR
-   USD

Every balance mutation requires a transaction record.

No floating-point money arithmetic where integer minor units can be
used.

------------------------------------------------------------------------

# 33. P1 --- Support

Final Support menu:

``` text
🆘 Support

🤖 AI Chatbot
👥 Support Team
👑 Contact Owner
🔎 FAQ
📖 Guide
🎯 Tour
🎫 My Tickets
💬 New Ticket
💡 Feature Request
📣 Updates
```

## 33.1 Support Team

Link users to the configured Telegram support group, using topics when
available.

The destination must be configurable rather than hard-coded in multiple
files.

## 33.2 Contact Owner

Only Creator and higher entitled users may see/use direct owner contact.

Lower plans should not see a misleading owner-contact option.

## 33.3 Updates

Updates should point to the configured official ChannelFlow update
channel/account, currently defined as `@BotFoundrry`.

## 33.4 AI Support

AI support should answer product questions and escalate to human support
when necessary.

It must not invent account/payment actions.

------------------------------------------------------------------------

# 34. P1 --- Support Tickets

Flow:

``` text
New Ticket
 -> Open
 -> User/Admin messages
 -> Close
 -> Reopen if permitted
```

Ticket must include:

-   ticket ID
-   user ID
-   category
-   status
-   messages
-   timestamps
-   assigned admin if applicable

Every ticket query must validate ownership for users.

------------------------------------------------------------------------

# 35. P1 --- Analytics

## 35.1 User analytics

Show:

-   total forwards
-   successful forwards
-   failed forwards
-   daily quota usage
-   extra credit balance
-   AI usage where applicable

## 35.2 Project analytics

Show:

-   total processed
-   successful
-   failed
-   filtered/rejected
-   source count
-   destination count
-   current status

## 35.3 Admin analytics

Show:

-   users
-   active users
-   subscriptions
-   plans
-   revenue
-   payments
-   forwarding volume
-   failures

Do not display fabricated metrics.

------------------------------------------------------------------------

# 36. P1 --- Notifications

Notify users for important events:

-   payment approved
-   payment rejected
-   payment expired
-   subscription expiring
-   subscription expired
-   daily quota exhausted
-   extra credits exhausted
-   Telegram connection failure
-   WhatsApp connection failure
-   repeated forwarding failure
-   support reply
-   relevant security event

Notification preferences should be configurable.

------------------------------------------------------------------------

# 37. P1 --- Admin System

Admin areas:

-   Users
-   Plans
-   Pricing
-   Payments
-   Subscriptions
-   Wallet
-   Extra Credits
-   Projects
-   Connections
-   Broadcast
-   Analytics
-   Support
-   Coupons
-   Giveaways
-   Maintenance

All admin actions require authorization.

## 37.1 RBAC

At minimum distinguish:

-   normal user
-   admin
-   owner

Sensitive owner-only actions must not be available to normal admins
unless explicitly allowed.

------------------------------------------------------------------------

# 38. P1 --- Admin Pricing

Admin can configure:

-   plan price
-   duration
-   Stars price
-   UPI price
-   Crypto price
-   extra-credit package price

Database is the source of truth.

Changing pricing must not modify historical payment records.

------------------------------------------------------------------------

# 39. P1 --- Broadcast Systems

There must be two clearly separate concepts.

## 39.1 ChannelFlow user announcement

Example:

``` text
/broadcast
```

Sends an administrative announcement to ChannelFlow users according to
the selected audience.

## 39.2 Destination promotional broadcast

Example:

``` text
/post
```

Sends a configured promotional post through selected project
destinations.

These must have separate:

-   handlers
-   permissions
-   state
-   audience selection
-   logging
-   confirmation

Never mix them.

------------------------------------------------------------------------

# 40. P1 --- Coupons

Support:

-   create
-   activate/deactivate
-   expiry
-   usage limit
-   per-user limit
-   plan restriction
-   duplicate-use prevention
-   history

Coupon redemption must be transactional.

------------------------------------------------------------------------

# 41. P1 --- Giveaways

Support:

-   create
-   configure eligibility
-   entry
-   closing
-   winner selection
-   reward assignment
-   duplicate protection
-   expiry
-   admin controls

Winner selection must be auditable.

------------------------------------------------------------------------

# 42. P1 --- Security and Multi-User Isolation

This is mandatory throughout the system.

For every object:

``` text
user
project
source
destination
connection
payment
subscription
wallet
reward
ticket
setting
```

validate ownership or authorized admin access.

Never trust IDs coming from callback data.

## Example attack

User A has project ID 100.

User B manually constructs:

``` text
project:100
```

The handler must reject it.

------------------------------------------------------------------------

# 43. P1 --- Global State

Avoid unsafe module-level state such as:

``` text
CURRENT_PROJECT
CURRENT_USER
WAITING_PAYMENT
WAITING_CODE
```

if it can leak across users.

All temporary workflow state should be keyed by:

``` text
channel_flow_user_id
flow_id
project_id where applicable
```

and should have expiration.

------------------------------------------------------------------------

# 44. P1 --- Stale Flow Cleanup

Temporary flows must expire:

-   Telegram login
-   2FA
-   WhatsApp pairing
-   payment
-   ticket creation
-   AI prompt
-   watermark settings
-   formatting input
-   project creation

A user who disappears should not leave a permanent state lock.

------------------------------------------------------------------------

# 45. P1 --- Rate Limiting

Rate-limit:

-   `/connect`
-   `FLOW`
-   2FA
-   WhatsApp pairing
-   payments
-   AI requests
-   support ticket creation
-   admin broadcast
-   sensitive account actions

Rate-limit responses must be user-friendly.

------------------------------------------------------------------------

# 46. P1 --- Database

The database must support:

-   fresh initialization
-   migrations
-   indexes
-   transactions
-   foreign keys where appropriate
-   atomic quota changes
-   atomic account claiming
-   payment idempotency
-   reward idempotency
-   wallet ledger integrity

No migration may attempt to alter a table before the table exists.

Fresh database startup is mandatory.

------------------------------------------------------------------------

# 47. P1 --- Money and Credit Accounting

Money must be represented safely, preferably using integer minor units
or Decimal where appropriate.

Every balance change must have a ledger entry.

Never mutate a balance without an auditable transaction.

------------------------------------------------------------------------

# 48. P1 --- Idempotency

The following must be idempotent:

-   payment callbacks
-   payment approvals
-   reward grants
-   milestone rewards
-   extra-credit grants
-   forwarding retries
-   webhook handling
-   connection completion where applicable

If the same event arrives twice, the final state must remain correct.

------------------------------------------------------------------------

# 49. P1 --- Concurrency

Protect:

-   quota
-   credits
-   payments
-   referrals
-   account ownership
-   connection replacement
-   project worker startup

Use atomic database operations/transactions rather than application-only
checks.

------------------------------------------------------------------------

# 50. P1 --- Crash Recovery

If a forwarding worker crashes:

1.  Detect failure.
2.  Log sanitized reason.
3.  Mark project degraded/error.
4.  Prevent duplicate worker creation.
5.  Restart safely if automatic recovery is configured.
6.  Notify user after defined thresholds.

One project crash must not bring down the whole bot.

------------------------------------------------------------------------

# 51. P1 --- Health Monitoring

Track:

-   connection status
-   last successful check
-   last error
-   last forwarded message
-   worker status

Statuses:

``` text
🟢 Healthy
🟡 Warning
🔴 Error
⚪ Disconnected
```

------------------------------------------------------------------------

# 52. P1 --- Project Health

Project status should reflect actual runtime state:

``` text
🟢 Active
🟡 Degraded
🔴 Error
⏸ Paused
⚪ Stopped
```

If a project is degraded, show a human-readable reason.

------------------------------------------------------------------------

# 53. P1 --- Test Actions

Provide real test operations.

Examples:

``` text
🧪 Test Connection
🧪 Test Destination
🧪 Test Project
```

Return clear results:

``` text
✅ Test successful
```

or:

``` text
❌ Test failed
Reason: destination permissions are insufficient.
```

Tests must not silently report success.

------------------------------------------------------------------------

# 54. P1 --- Permission Checks

When adding a source/destination, validate permissions immediately.

Examples:

-   Can the connected Telegram account read the source?
-   Can it send to the destination?
-   Can the WhatsApp connection send to the selected chat?

Do not allow activation if required permissions are missing.

------------------------------------------------------------------------

# 55. P1 --- Media Handling

Verify support for:

-   text
-   photo
-   video
-   document
-   audio
-   animation
-   album
-   caption
-   links

For every media type define:

-   download
-   processing
-   upload/send
-   retry
-   cleanup
-   unsupported fallback

------------------------------------------------------------------------

# 56. P1 --- Album Handling

Albums must be buffered/grouped so that related media can be forwarded
together where the destination supports grouped media.

Avoid sending every album item as unrelated messages unless the
destination API requires it.

Album buffering must not create indefinite memory/state.

------------------------------------------------------------------------

# 57. P1 --- Message Ordering

Where ordering is meaningful, preserve source order.

Example:

``` text
Source:
1
2
3

Destination:
1
2
3
```

Concurrent workers must not randomly reorder messages within the same
ordered stream.

------------------------------------------------------------------------

# 58. P1 --- Links and Captions

Transformations must preserve URLs and captions.

Verify:

-   URLs are not accidentally removed
-   Markdown/HTML is not corrupted
-   captions remain attached to media where supported
-   entity formatting is handled safely

------------------------------------------------------------------------

# 59. P1 --- Optional Transformation Failure Policy

If AI fails:

-   configured fallback

If affiliate service fails:

-   preserve original URL

If watermark fails:

-   use defined media fallback

If formatting fails:

-   preserve original text

An optional transformation must not unexpectedly stop an otherwise
healthy project.

------------------------------------------------------------------------

# 60. P1 --- Protected Content

Do not build functionality intended to bypass Telegram's
protected-content restrictions.

The forwarding system must respect platform restrictions and only
process content the connected account is legitimately allowed to access
and forward.

------------------------------------------------------------------------

# 61. P2 --- Localization

Supported languages:

-   English
-   Hindi
-   Bengali
-   Urdu
-   Spanish
-   Arabic
-   Indonesian

The existing i18n foundation should be expanded so that actual
user-facing screens use translated strings.

Do not leave major screens hard-coded in English while claiming
localization is complete.

------------------------------------------------------------------------

# 62. P2 --- Onboarding

Recommended new-user flow:

``` text
/start
  |
  v
Welcome
  |
  v
7-day Creator Trial
  |
  v
Connect Telegram
  |
  v
Create First Project
  |
  v
Test
  |
  v
Activate
```

Onboarding should explain the core product without overwhelming the
user.

------------------------------------------------------------------------

# 63. P2 --- Guide

Guide should explain:

1.  Connect Telegram.
2.  Create project.
3.  Add source.
4.  Add destination.
5.  Test.
6.  Start forwarding.
7.  Configure filters.
8.  Configure AI.
9.  Configure watermark.
10. Configure affiliate replacement.
11. Use WhatsApp.
12. Manage subscription.
13. Use rewards.
14. Contact support.

------------------------------------------------------------------------

# 64. P2 --- Bot Tour

Interactive tour:

``` text
Projects
 -> Subscription
 -> Rewards
 -> Account
 -> Support
```

Actions:

-   Next
-   Back
-   Skip
-   Finish

Tour state must be user-scoped.

------------------------------------------------------------------------

# 65. P2 --- UX Consistency

Every flow should have consistent:

-   terminology
-   button naming
-   back behaviour
-   cancel behaviour
-   success messages
-   error messages
-   confirmation screens

Avoid mixing old names such as:

-   Premium Plans
-   Earn/Referral
-   My Account
-   Help Center

with final names.

Preferred terminology:

-   Subscription
-   Rewards
-   Account
-   Support

------------------------------------------------------------------------

# 66. P2 --- Settings

Settings may include:

-   Language
-   Notifications
-   Auto Renew
-   system status
-   account/connection preferences

Every toggle must actually persist and be used.

A button that only changes an in-memory variable but does not affect
runtime is not complete.

------------------------------------------------------------------------

# 67. P2 --- Audit Logging

Audit sensitive admin operations:

``` text
price changed
plan changed
payment approved
wallet adjusted
credits adjusted
user suspended
broadcast sent
connection disabled
```

Record:

-   actor
-   action
-   target
-   timestamp
-   relevant safe metadata

Never record secrets.

------------------------------------------------------------------------

# 68. P2 --- User Suspension

Admin should be able to suspend/block a user with a reason.

Suspended users should be prevented from sensitive actions according to
policy.

Existing data should not be destroyed.

------------------------------------------------------------------------

# 69. P2 --- Maintenance Mode

Admin can enable maintenance mode.

During maintenance:

-   normal users receive a controlled message
-   admin/owner access remains available if desired
-   background jobs follow a defined maintenance policy

------------------------------------------------------------------------

# 70. P2 --- Logging

Use structured logging.

Useful events:

-   startup
-   shutdown
-   connection
-   project start/stop
-   forwarding success/failure
-   payment state
-   support
-   admin action

Never log:

-   OTP
-   2FA password
-   session
-   bot token
-   API hash
-   access tokens
-   private credentials

------------------------------------------------------------------------

# 71. P2 --- Log Rotation

Logs must not grow forever.

Use rotation/retention suitable for Pydroid and production deployment.

------------------------------------------------------------------------

# 72. P2 --- Graceful Shutdown

On Ctrl+C/process shutdown:

``` text
Stop accepting new work
        |
        v
Stop forwarding workers
        |
        v
Stop schedulers
        |
        v
Disconnect external clients
        |
        v
Close database
        |
        v
Exit
```

Avoid:

``` text
Task was destroyed but it is pending!
```

where the application can reasonably clean up the task.

------------------------------------------------------------------------

# 73. P2 --- Dependency Management

`requirements.txt` must contain every third-party dependency actually
imported by the production code.

A clean environment must be able to install dependencies and start the
bot.

The current startup test reached the bot/listener/scheduler but
encountered missing `aiohttp` in the Pydroid environment. Dependency
installation and clean-environment testing are therefore mandatory.

------------------------------------------------------------------------

# 74. P2 --- Pydroid 3 Compatibility

Target command:

``` bash
python main.py
```

The project must be tested in the target environment.

Do not rely only on development-machine testing.

Verify:

``` bash
python -m compileall .
python main.py
```

after a clean dependency installation.

------------------------------------------------------------------------

# 75. P2 --- Callback Integrity

Every callback generated by a keyboard must have a corresponding
handler.

Examples of callback families requiring explicit verification include:

``` text
pay:*
support:*
pacct:*
wacode:*
ai*
aff*
wm*
analytics:*
```

No orphan callback should remain.

## Callback acceptance rule

For every callback:

``` text
Keyboard
 -> callback data
 -> router
 -> handler
 -> service
 -> response
```

must be tested.

------------------------------------------------------------------------

# 76. P2 --- Command Integrity

Every documented command must:

-   be registered
-   reach a handler
-   validate input
-   handle cancellation
-   handle errors
-   update correct state
-   return a user-visible result

`FLOW` is mandatory.

No undocumented legacy command should remain the only way to perform a
critical flow.

------------------------------------------------------------------------

# 77. P2 --- Dead UI Prevention

A button must be hidden if its feature is not implemented.

Examples:

Do not show Clone if Clone is deferred.

Do not show a payment method if its full flow does not work.

Do not show a platform if connection/forwarding is incomplete.

Do not show Contact Owner to users without entitlement.

------------------------------------------------------------------------

# 78. P2 --- Legacy Code Consolidation

The current codebase contains legacy/backup/duplicate structures.

Do not blindly delete them.

Perform an audit:

``` text
Is this imported?
Is this called?
Is this duplicate?
Is this legacy?
Is this required by migration?
```

Then consolidate.

The final runtime should have one authoritative implementation per
feature.

Avoid creating another duplicate handler file to solve an existing
integration problem.

------------------------------------------------------------------------

# 79. P2 --- Documentation

README and project tracker must match actual behaviour.

Documentation must clearly state:

-   setup
-   environment variables
-   database
-   Telegram connection flow
-   `FLOW`
-   project creation
-   supported routes
-   subscription
-   payment methods
-   extra credits
-   rewards
-   support
-   admin
-   testing
-   limitations

No documentation may claim a feature is complete when it is only
partially implemented.

------------------------------------------------------------------------

# 80. Feature Reality Matrix

Before implementation begins, the agent must produce a matrix like:

  Feature                 DB   Service   Handler   UI   Runtime   Tests   Status
  ----------------------- ---- --------- --------- ---- --------- ------- --------
  `FLOW`                                                               
  Telegram ownership                                                      
  Telegram -\> Telegram                                                   
  Telegram -\> WhatsApp                                                   
  Stars                                                                   
  UPI                                                                     
  Crypto                                                                  
  Extra credits                                                           
  Rewards                                                                 
  Support                                                                 
  AI                                                                      
  Filters                                                                 
  Formatting                                                              
  Link Preview ON/OFF                                                     
  Remove Usernames                                                        
  Remove Links                                                            
  Disable Hidden Links                                                    
  Mono Text                                                               
  Trim Words/Lines                                                        
  Custom Header/Footer                                                    
  Post Edit Sync                                                          
  Topics Forwarding                                                       
  Auto Reaction                                                           
  Watermark                                                               
  Affiliate                                                               
  Analytics                                                               

Statuses:

-   GREEN = VERIFIED
-   YELLOW = PARTIAL
-   RED = NOT IMPLEMENTED
-   GRAY = NOT IN CURRENT SCOPE
-   BLUE = IMPLEMENTED BUT NEEDS RUNTIME VERIFICATION

------------------------------------------------------------------------

# 81. Automated Integrity Checks

The project should include tests that detect:

## 81.1 Import integrity

``` bash
python -m compileall .
```

and import critical modules.

## 81.2 Callback integrity

Extract callback prefixes/data from keyboards and verify that routers
handle them.

## 81.3 Command integrity

Verify expected commands are registered.

## 81.4 Database integrity

Test:

-   fresh DB
-   migration
-   constraints
-   indexes
-   transactions

## 81.5 Ownership integrity

Test cross-user access denial.

## 81.6 Account uniqueness

Test two users attempting the same Telegram external ID.

## 81.7 Payment idempotency

Send the same callback twice.

Expected:

``` text
one activation
one ledger event
```

## 81.8 Reward idempotency

Trigger the same reward twice.

Expected:

``` text
one reward
```

## 81.9 Quota race

Two concurrent operations against one remaining quota unit.

Expected:

``` text
one successful reservation
one rejection
```

------------------------------------------------------------------------

# 82. Required End-to-End Test Scenarios

## 82.1 New user

``` text
/start
 -> trial created
 -> main menu
```

## 82.2 Telegram connection

``` text
/connect +phone
 -> OTP
 -> FLOW12345
 -> optional 2FA
 -> connected
```

## 82.3 Duplicate Telegram connection

User A connects Telegram ID X.

User B attempts Telegram ID X.

Expected:

``` text
B rejected
A remains owner
```

## 82.4 Disconnect/reclaim

``` text
A disconnects
 -> X released
B connects X
 -> success
```

## 82.5 Telegram -\> Telegram

``` text
create project
 -> source
 -> destination
 -> test
 -> activate
 -> send source message
 -> destination receives once
```

## 82.6 Duplicate message

Same event processed twice.

Expected:

``` text
one destination message
```

## 82.7 Quota

Reach daily quota.

Expected:

``` text
plan quota exhausted
 -> extra credits used if available
 -> otherwise reject
```

## 82.8 Extra credit purchase

``` text
purchase
 -> payment
 -> idempotent grant
 -> balance increases
```

## 82.9 Payment duplicate

Same payment callback twice.

Expected:

``` text
one subscription/credit grant
```

## 82.10 Trial referral

Trial conversion.

Expected:

``` text
no paid referral reward
```

## 82.11 Paid referral

Qualifying paid conversion.

Expected:

``` text
one referral conversion
reward granted once
```

## 82.12 Support

``` text
New Ticket
 -> reply
 -> close
 -> reopen
```

## 82.13 User isolation

User B tries to access A's project/payment/ticket/connection.

Expected:

``` text
DENIED
```

------------------------------------------------------------------------

# 83. Required P0 Acceptance Criteria

The build cannot be considered production-ready until all of these pass:

-   `/start` works.
-   New user gets one 7-day Creator trial.
-   `/connect` works.
-   `FLOW` works.
-   Valid OTP finalization works.
-   2FA works where enabled.
-   Actual Telegram user ID is captured.
-   Duplicate Telegram ownership is prevented.
-   Disconnect releases ownership.
-   Restart recovery works.
-   Final navigation works.
-   Project creation works.
-   Advanced formatting controls work: Link Preview ON/OFF, Remove Usernames,
    Remove Links, Disable Hidden Links, Mono Text, Trim Words/Lines, and
    Custom Header/Footer.
-   Per-destination Header/Footer overrides work where configured.
-   Post Edit Sync updates mapped destination messages without cross-user or
    cross-task edits.
-   Telegram forum/topic routing forwards only the configured topics.
-   Auto Reaction works where supported and never blocks forwarding on
    reaction failure.
-   Telegram -\> Telegram forwarding works.
-   Telegram -\> WhatsApp works if enabled as a production platform.
-   No per-forward billing exists.
-   Daily quota is atomic.
-   Extra credits work.
-   Stars works.
-   UPI works.
-   Crypto works.
-   Payments are idempotent.
-   Rewards are idempotent.
-   Support works.
-   Cross-user access is denied.
-   No dead critical buttons exist.
-   Fresh DB works.
-   Existing DB migration works.
-   Clean dependency installation works.
-   `python main.py` starts successfully in target environment.
-   Graceful shutdown works.

------------------------------------------------------------------------

# 84. Definition of Done

A phase or feature is complete only when:

### Code

-   implementation exists
-   no duplicate conflicting implementation
-   no dead code introduced

### UI

-   button/command exists
-   correct wording
-   correct entitlement gating
-   Back/Cancel works

### Backend

-   service works
-   DB state works
-   transactions are safe
-   error handling exists

### Runtime

-   external API flow works
-   real user journey works
-   restart behaviour works

### Security

-   ownership checked
-   secrets protected
-   rate limiting applied where required

### Testing

-   unit tests
-   integration tests
-   negative tests
-   concurrency/idempotency tests where relevant
-   target-environment smoke test

### Documentation

-   README updated
-   tracker updated
-   limitations documented

Only then:

``` text
STATUS = VERIFIED
```

------------------------------------------------------------------------

# 85. Implementation Order

Do not implement randomly.

## Phase A --- Audit

1.  Freeze current baseline.
2.  Inventory all files.
3.  Inventory commands.
4.  Inventory callback data.
5.  Inventory services.
6.  Inventory DB tables.
7.  Build Feature Reality Matrix.
8.  Identify duplicate/legacy code.
9.  Do not rewrite yet.

## Phase B --- Core identity/security

1.  Fix session/login bugs.
2.  Implement `FLOW`.
3.  Implement explicit login state.
4.  Capture actual Telegram ID.
5.  Implement unique ownership.
6.  Implement atomic account claiming.
7.  Implement disconnect/reconnect.
8.  Implement session recovery.
9.  Test multi-user isolation.

## Phase C --- Core project engine

1.  Final navigation.
2.  Project CRUD.
3.  Source/destination management.
4.  Permission checks.
5.  Telegram -\> Telegram.
6.  Dedup.
7.  Retry.
8.  Media.
9.  Quota.
10. Test buttons.

## Phase D --- WhatsApp

1.  Account connection.
2.  Pairing.
3.  Ownership.
4.  Destination.
5.  Permission.
6.  Test.
7.  Forwarding.
8.  Reconnect/disconnect.
9.  Recovery.

## Phase E --- Transformation pipeline

1.  Filters.
2.  Formatting.
3.  AI.
4.  Affiliate.
5.  Watermark.
6.  Media pipeline.

## Phase F --- Monetization

1.  Plans.
2.  Trial.
3.  Subscription.
4.  Stars.
5.  UPI.
6.  Crypto.
7.  Extra credits.
8.  Payment idempotency.
9.  Admin pricing.

## Phase G --- Rewards/support

1.  Referral.
2.  Milestones.
3.  Rewards.
4.  Support AI.
5.  Support group.
6.  Tickets.
7.  Owner contact gating.
8.  Updates.

## Phase H --- Admin/operations

1.  Users.
2.  Plans.
3.  Payments.
4.  Wallet.
5.  Credits.
6.  Broadcast.
7.  Analytics.
8.  Coupons.
9.  Giveaways.
10. Maintenance.
11. Audit logs.

## Phase I --- Hardening

1.  Localization.
2.  Recovery.
3.  Health monitoring.
4.  Crash recovery.
5.  Graceful shutdown.
6.  Rate limits.
7.  Logging/rotation.
8.  Cleanup.
9.  Documentation.

## Phase J --- Final verification

Run:

``` text
compile
imports
fresh DB
migration
unit tests
integration tests
security tests
concurrency tests
idempotency tests
Pydroid smoke test
full manual Telegram test
```

------------------------------------------------------------------------

# 86. Agent Operating Rules

The coding agent MUST follow these rules.

## Rule 1 --- Inspect before changing

Do not rewrite a service merely because the UI is missing.

First determine whether a working backend already exists.

## Rule 2 --- One source of truth

Do not create multiple competing implementations of the same feature.

## Rule 3 --- No fake completion

Never say "implemented" because a function was created.

## Rule 4 --- No dead buttons

Every visible button must work or be hidden.

## Rule 5 --- No secret logging

Never print sensitive credentials.

## Rule 6 --- No per-forward billing

Do not reintroduce it.

## Rule 7 --- Respect account uniqueness

Actual external Telegram ID is the identity used for uniqueness.

## Rule 8 --- Database enforces critical invariants

Ownership, quota, payment and reward integrity must not depend only on
Python conditionals.

## Rule 9 --- Preserve working code

Refactor only when required.

## Rule 10 --- Test after each meaningful change

Do not make 20 unrelated changes and test once.

## Rule 11 --- Runtime verification is mandatory

Static compilation is not enough.

## Rule 12 --- Update documentation after behaviour changes

README and tracker must match actual implementation.

------------------------------------------------------------------------

# 87. Required Final Deliverables

The completed project should contain:

``` text
main.py
bot/
core/
services/
database/
utils/
tests/
docs/
.env.example
requirements.txt
README.md
PROJECT_TRACKER.md
PRD.md
```

Do not include:

-   real `.env`
-   real API keys
-   real Telegram sessions
-   private credentials
-   personal database containing production user data
-   unnecessary `__pycache__`

------------------------------------------------------------------------

# 88. Final Product UX Snapshot

## New user

``` text
/start

Welcome to ChannelFlow AI

🎁 7-Day Creator Trial

[🚀 Connect Telegram]
[📖 Guide]
[🎯 Tour]
```

## Connected user

``` text
📁 Projects
💳 Subscription
🎁 Rewards
👤 Account
🆘 Support
⚙️ Settings
```

## Telegram connection

``` text
/connect +919876543210

📩 Telegram sent your login code.

Send it as:

FLOW12345
```

## Already connected account

``` text
/connect +919876543210

⚠️ This Telegram account is already connected.

Please disconnect it before connecting again.
```

## Same account owned by another ChannelFlow user

``` text
❌ This Telegram account is already connected
to another ChannelFlow account.

Disconnect it from that account first.
```

Do not reveal the other user's identity.

## Project

``` text
📁 News Forwarder

🟢 Active

Telegram → Telegram

[⏸ Pause]
[🧪 Test]
[📊 Stats]
[⚙️ Settings]
[🗑 Delete]
```

## Subscription

``` text
💳 Subscription

Creator
7-day trial / paid status
Daily: 2000+
Used: 850
Extra credits: 5000

[⬆️ Upgrade]
[🔄 Renew]
[📜 History]
```

## Rewards

``` text
🎁 Rewards

Invited: 127
Paid conversions: 83

🏆 Milestones
50 ✅
100 🔒
250 🔒
500 🔒

[🔗 Referral Link]
[📊 Leaderboard]
[💰 History]
```

------------------------------------------------------------------------

# 89. Explicit Out-of-Scope Items

Unless separately approved:

-   Clone Bot Network / Phase 10.2
-   Per-forward billing
-   Additional payment methods
-   Bypassing platform protected-content restrictions
-   Any feature that requires violating a platform's access rules

------------------------------------------------------------------------

# 90. Final Success Definition

ChannelFlow AI is considered successfully reconstructed when a fresh
installation can be deployed in the target environment and a real user
can complete this journey without manual database edits or developer
intervention:

``` text
Install
  |
  v
Configure .env
  |
  v
python main.py
  |
  v
/start
  |
  v
7-day Creator trial
  |
  v
/connect +phone
  |
  v
FLOW OTP
  |
  v
Optional 2FA
  |
  v
Secure Telegram account ownership
  |
  v
Create project
  |
  v
Add source
  |
  v
Add destination
  |
  v
Test
  |
  v
Activate
  |
  v
Receive messages
  |
  v
Process filters/AI/affiliate/formatting/watermark
  |
  v
Forward reliably
  |
  v
Track quota
  |
  v
Use extra credits if applicable
  |
  v
Purchase subscription/credits through
Stars / UPI / Crypto
  |
  v
Earn eligible referral rewards
  |
  v
Use Support
  |
  v
Restart bot
  |
  v
Recover active connections/projects
```

The system must remain secure and isolated when multiple users perform
these actions concurrently.

------------------------------------------------------------------------

# 91. Final Instruction to the Coding Agent

Treat this PRD as the product contract.

Do not start by blindly coding.

First produce:

1.  Current architecture map.
2.  Feature Reality Matrix.
3.  Command inventory.
4.  Callback inventory.
5.  Database schema inventory.
6.  Service inventory.
7.  Duplicate/legacy inventory.
8.  Missing-feature list.
9.  Broken-feature list.
10. Risk list.
11. Implementation plan mapped to this PRD.

Then implement in the prescribed order.

For every completed item, provide:

``` text
Feature:
Status:
Files changed:
Database changes:
UI changes:
Runtime flow:
Tests added:
Tests executed:
Result:
Known limitations:
```

A feature may only be marked `VERIFIED` after actual testing.

**The goal is not to make the code look complete. The goal is to make
the product actually work end-to-end.**
