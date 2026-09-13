# Baseline audit and implementation plan

## Architecture
`main.py` registers python-telegram-bot handlers and starts `core/listener.py`.
The listener owns the shared Telethon engine, per-owner client pool, queue and schedulers.
`core/forwarder.py` routes source events into synchronous SQLite services, transformations,
Telegram sends and external publish jobs. `bot/handlers.py` is the main UI router;
admin and promotion routers are separate. `database/db.py` creates/migrates SQLite.

## Feature reality matrix
No feature is VERIFIED at baseline. P = present in source, ? = unverified, — = absent.

| Feature | DB | Service | Handler/UI | Runtime | Tests | Status / evidence |
|---|---|---|---|---|---|---|
| FLOW | ? | ? | ? | ? | — | YELLOW / Parser uses /myflow; handler submits bare numeric text; _time is undefined. |
| Telegram ownership | ? | ? | ? | ? | — | YELLOW / Encrypted session keyed by ChannelFlow ID; actual external ID and unique claim absent. |
| Telegram → Telegram | ? | ? | ? | ? | — | YELLOW / Runtime exists but dispatch owner_id is assigned after return; duplicated quota reservation; formatting loses to AI. |
| Telegram → WhatsApp | ? | ? | ? | ? | — | YELLOW / Pairing, adapter and queue exist; bridge/media/live delivery not verified. |
| Stars | ? | ? | ? | ? | — | YELLOW / No precheckout/successful-payment registration in main.py. |
| UPI | ? | ? | ? | ? | — | YELLOW / Manual request/approval services present; end-to-end and replay unverified. |
| Crypto | ? | ? | ? | ? | — | YELLOW / Invoice service present; credentials/provider verification needed. |
| Extra credits | ? | ? | ? | ? | — | YELLOW / No dedicated forwarding credit ledger found. |
| Rewards | ? | ? | ? | ? | — | YELLOW / Referral services and milestone tables present; paid-only and replay need tests. |
| Support | ? | ? | ? | ? | — | YELLOW / Ticket and AI services present; callback coverage not established. |
| AI | ? | ? | ? | ? | — | YELLOW / Service and runtime present; logger call malformed and formatting overwritten. |
| Filters | ? | ? | ? | ? | — | YELLOW / Runtime exists; negative/regex safety tests needed. |
| Formatting | ? | ? | ? | ? | — | YELLOW / Prefix/suffix/rules exist; placeholder URL protection can be damaged. |
| Advanced cleanup / previews / mono / destination headers | ? | ? | ? | ? | — | YELLOW / Required new controls absent from formatting schema. |
| Post Edit Sync | ? | ? | ? | ? | — | YELLOW / No edit event path/mapping found. |
| Topics | ? | ? | ? | ? | — | YELLOW / No topic routing path found. |
| Auto Reaction | ? | ? | ? | ? | — | YELLOW / No reaction runtime found. |
| Watermark | ? | ? | ? | ? | — | YELLOW / Photo processing present; live media verification needed. |
| Affiliate | ? | ? | ? | ? | — | YELLOW / Service present; provider correctness and plan isolation not verified. |
| Analytics | ? | ? | ? | ? | — | YELLOW / Existing counters; dispatch failure prevents reliable population. |

## Legacy inventory
- `bot/handlers.py.backup`: preserved as historical source, not imported.
- `ChannelFlowAI5_monetization/bot/handlers.py`: duplicate legacy tree, not selected by main imports.
- Root test/check/find scripts: preserved; may mutate a default database. New tests use isolated temporary databases.
- Shared operator session: security-sensitive legacy behavior; must not serve arbitrary unconnected users.

## Missing / broken / risks
The matrix above is the initial gap list, not an assertion of complete coverage of 21,000+ lines.
Payment, wallet and reward transactions require a separate full audit before real funds.
The attachment has no live credentials and this sandbox lacks several runtime dependencies.
No Android/Pydroid device or live Telegram/WhatsApp/payment environment is available.
A static or mocked test cannot establish external production readiness.

## PRD-mapped implementation order
1. A (§80,85): preserve original archive; inventory and baseline audit (this document).
2. B (§5–7): FLOW parser, serialized login/expiry/cancellation, actual identity and unique DB claim, safe recovery.
3. C (§11,20–23,42): fix dispatch and double quota; isolate shared routing; global atomic quota and credit reservation ledger.
4. E (§17): integrate cleanup, URL-safe formatting, destination overrides and usable preview/configuration commands.
5. Hardening (§21,60,72,81): bounded retry, protected content guard, shutdown cleanup and regression tests.
6. Record all remaining D/F/G/H/I/J gaps explicitly. Do not claim every feature complete.

The delivered build is an incremental hardening release, not the PRD's final production release.
