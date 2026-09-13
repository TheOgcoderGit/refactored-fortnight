"""
ChannelFlow AI - One-Time Account Authorization
=================================================

Run this ONCE, interactively, from a terminal you trust, to log the
Telegram account that will power the forward engine into its local
session file:

    python -m core.authorize

You will be prompted for:

    * Your phone number (e.g. +919876543210)
    * The login code Telegram sends you
    * Your Two-Step-Verification password, only if you have 2FA enabled

After this succeeds, ``<SESSION_NAME>.session`` will exist next to this
project and the bot (``main.py``) can start the forward engine without
any further prompts.

This script is intentionally NOT wired into the bot's chat UI. Telegram
account login (phone + OTP + 2FA password) must never be collected
through bot chat messages from arbitrary Telegram users - that pattern
is how session-stealing bots operate, and it also violates Telegram's
terms of service when used to onboard many different people's accounts
into one automated forwarding service. Here, only the person running
this script on their own machine, for their own account, can use it.
"""

import asyncio

from telethon.errors import SessionPasswordNeededError

from core.client import client


async def _authorize():

    print("================================")
    print("ChannelFlow AI - Account Login")
    print("================================")

    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already authorized as {me.first_name} (@{me.username}).")
        return

    phone = input("Phone number (e.g. +919876543210): ").strip()

    sent = await client.send_code_request(phone)

    code = input("Login code from Telegram: ").strip()

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)

    except SessionPasswordNeededError:

        password = input("Two-Step Verification password: ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()

    print("--------------------------------")
    print(f"Logged in as {me.first_name} (@{me.username})")
    print(f"Session saved. You can now run: python main.py")
    print("--------------------------------")


if __name__ == "__main__":
    asyncio.run(_authorize())
