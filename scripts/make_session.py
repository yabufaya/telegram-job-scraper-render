"""Generate a TELEGRAM_SESSION_STRING once, on your own machine.

Run it in a terminal — never through the web server, never on Render:

    pip install telethon python-dotenv
    python scripts/make_session.py

It asks for your phone number, then the login code Telegram sends you (and
your 2FA password if you have one). The result is a long string that you paste
into Render → your service → Environment → TELEGRAM_SESSION_STRING.

IMPORTANT
  * This logs in as the account that owns the phone number. Use a dedicated
    account, not your personal one: the session allows reading channels, and
    any abuse by visitors is attributed to that account.
  * The string is a credential. Treat it like a password. Do not commit it.
  * Nothing is written to disk — the old `.session` file approach breaks on
    Render because its filesystem is wiped on every deploy and idle spin-down.
"""
from __future__ import annotations

import asyncio
import getpass
import os
import re

from telethon import TelegramClient
from telethon.sessions import StringSession

PHONE_PATTERN = re.compile(r"^\+\d{7,15}$")


def prompt_phone() -> str:
    while True:
        raw = input("Phone number in international format (e.g. +251953933719): ").strip()
        cleaned = raw.replace(" ", "").replace("-", "")
        if PHONE_PATTERN.match(cleaned):
            return cleaned
        print("Rejected: must start with '+', include country code, no spaces (e.g. +251953933719).")


async def main() -> None:
    print("\nGet api_id / api_hash from https://my.telegram.org → API Development Tools\n")
    api_id = int(input("TELEGRAM_API_ID: ").strip())
    api_hash = input("TELEGRAM_API_HASH: ").strip()

    phone = prompt_phone()

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        sent = await client.send_code_request(phone)
        code = input("Login code from Telegram: ").strip()
        try:
            await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
        except Exception as exc:  # SessionPasswordNeededError and friends
            if "password" in str(exc).lower() or exc.__class__.__name__ == "SessionPasswordNeededError":
                password = getpass.getpass("Two-step verification password: ")
                await client.sign_in(password=password)
            else:
                raise

    me = await client.get_me()
    session_string = client.session.save()
    await client.disconnect()

    print("\n" + "=" * 72)
    print(f"Logged in as: {me.first_name} (@{me.username}) id={me.id}")
    print("=" * 72)
    print("\nCopy EVERYTHING between the markers into TELEGRAM_SESSION_STRING:\n")
    print("-----BEGIN TELEGRAM_SESSION_STRING-----")
    print(session_string)
    print("-----END TELEGRAM_SESSION_STRING-----\n")

    out = os.path.join(os.path.dirname(__file__), "..", "session_string.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(session_string + "\n")
    print(f"Also saved to {os.path.normpath(out)} (git-ignored). Delete it after pasting into Render.")


if __name__ == "__main__":
    asyncio.run(main())
