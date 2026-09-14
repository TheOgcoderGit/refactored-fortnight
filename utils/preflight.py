"""Offline dependency/config check. Never prints secret values."""
import importlib.util
import os
import sys
from pathlib import Path
root=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(root))
missing=[name for name in ('telegram','telethon','dotenv','httpx','aiosqlite','aiohttp','Crypto','PIL','cryptography') if importlib.util.find_spec(name) is None]
if missing:
    print('Missing dependencies: '+', '.join(missing))
    print('Use this interpreter: python -m pip install -r requirements.txt')
    raise SystemExit(1)
try:
    import config
    from core.session_crypto import _key_bytes
    _key_bytes()
except Exception as error:
    print('Configuration invalid: '+type(error).__name__+'. Check local .env and README; values are not printed.')
    raise SystemExit(1)
print('Dependency discovery and encryption-key validation passed. This is NOT a network, login or deployment test.')
