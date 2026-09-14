# Live acceptance — all unchecked

Use test users/chats, not production accounts/funds. Keep keys in local environment configuration.
- [ ] Clean installation on Pydroid and `python main.py` startup.
- [ ] /start grants exactly one trial; reconnect/restart does not grant another.
- [ ] FLOW and optional 2FA; invalid/expired OTP; /cancel; no secret logs.
- [ ] Same external Telegram account cannot be claimed by two users.
- [ ] Disconnect stops routes and releases ownership; reconnect/restart restores correct accounts.
- [ ] Source permission checks and destination tests use the owner's account.
- [ ] Create/Test/Start/Pause/Delete; cross-user forged callbacks denied.
- [ ] Text, captions, photos, videos, documents, audio, animation and albums.
- [ ] Protected sources are not downloaded/copied.
- [ ] Every advanced formatting option and destination override; 4096/1024 UTF-16 limits.
- [ ] Duplicate/reconnect/retry; quota across projects; credits and failure rollback.
- [ ] Reconcile ambiguous network sends without blind replay or refund.
- [ ] Ctrl+C: stop queued work and disconnect clients, no leaked tasks.
- [ ] New/reply/view/close/reopen tickets; internal notes hidden; admin replies notify users.

## Separate implementation work before enabling commerce/external routes
Stars invoice/precheckout/success callbacks, UPI references/replay, Crypto confirmations,
atomic payment+entitlement+ledger/outbox, integer currency accounting, paid-only referral rewards,
credit package purchasing, coupon/giveaway races and administrative authorization.
WhatsApp requires a verified supported destination API/bridge; credentials alone do not prove
WhatsApp Channel publishing capability. Do not enable the experimental switch in production.
Post Edit Sync, topic routing and reactions require full UI→service→DB→runtime→test implementation.
