"""
ChannelFlow AI - Session Encryption
======================================

Encrypts customer Telegram session strings before they touch disk.
This is the one place that's allowed to know SESSION_ENCRYPTION_KEY -
every other module (core/user_sessions.py, bot/handlers.py) only ever
sees plaintext session strings transiently in memory, never persists
them unencrypted, and never logs them.

Uses pycryptodome's AES-256-GCM (authenticated encryption - a tampered
ciphertext fails to decrypt loudly instead of silently returning
corrupted bytes that get handed to Telethon) rather than 'cryptography'
(Fernet). pycryptodome is the pure-C/prebuilt-wheel choice here
specifically because it installs reliably under Pydroid3 on Android,
where 'cryptography' needs a Rust toolchain to build and typically
fails there.

Wire format: base64(12-byte nonce || GCM ciphertext-with-appended-tag).
Nonce is random per encryption call, never reused with the same key.

SESSION_ENCRYPTION_KEY is REQUIRED (unlike every other optional env var
in config.py) for anything in core/user_sessions.py to run - there's no
"unconfigured, feature just doesn't offer itself" fallback here, on
purpose. A per-user Telegram login session is equivalent to full
account access for that person; storing it without encryption is not
an acceptable degraded mode. If it's missing, the connect flow refuses
to start rather than falling back to storing sessions in plaintext.
"""

import base64
import os

from Crypto.Cipher import AES

from config import SESSION_ENCRYPTION_KEY

NONCE_SIZE = 12  # bytes, standard for GCM


class SessionEncryptionNotConfigured(Exception):
    pass


def _key_bytes() -> bytes:

    if not SESSION_ENCRYPTION_KEY:
        raise SessionEncryptionNotConfigured(
            "SESSION_ENCRYPTION_KEY is not set. Generate one with:\n"
            "  python -c \"import os, base64; "
            "print(base64.urlsafe_b64encode(os.urandom(32)).decode())\"\n"
            "and add it to .env before enabling per-user Telegram login."
        )

    try:
        key = base64.urlsafe_b64decode(SESSION_ENCRYPTION_KEY.encode())
    except Exception as e:
        raise SessionEncryptionNotConfigured(
            f"SESSION_ENCRYPTION_KEY is set but not valid base64: {e}"
        )

    if len(key) != 32:
        raise SessionEncryptionNotConfigured(
            "SESSION_ENCRYPTION_KEY must decode to exactly 32 bytes (AES-256). "
            "Generate a new one with the command in this module's docstring."
        )

    return key


def encrypt_session(session_string: str) -> str:

    key = _key_bytes()
    nonce = os.urandom(NONCE_SIZE)

    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(session_string.encode())

    return base64.b64encode(nonce + ciphertext + tag).decode()


def decrypt_session(encrypted: str) -> str:

    key = _key_bytes()

    try:
        raw = base64.b64decode(encrypted.encode())
        nonce, ciphertext, tag = raw[:NONCE_SIZE], raw[NONCE_SIZE:-16], raw[-16:]

        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag).decode()

    except (ValueError, KeyError) as e:
        raise SessionEncryptionNotConfigured(
            "Stored session could not be decrypted - SESSION_ENCRYPTION_KEY "
            f"may have changed since this session was saved, or the row was "
            f"tampered with. The affected user needs to /connect again. ({e})"
        )
