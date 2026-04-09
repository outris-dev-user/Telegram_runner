"""
One-time interactive auth helper.

Run this LOCALLY (not on Railway) to generate a session string:
    python auth.py

Copy the printed TELEGRAM_SESSION_STRING value and set it as a Railway
environment variable. No volumes, no file uploads needed.

Requires env vars:
    TELEGRAM_API_ID
    TELEGRAM_API_HASH
    TELEGRAM_PHONE      (your phone number, e.g. +919876543210)
"""

import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    phone = os.environ["TELEGRAM_PHONE"]

    # StringSession starts empty — Telethon will populate it after login
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start(phone=phone)

    me = await client.get_me()
    session_string = client.session.save()

    print(f"\nLogged in as: {me.first_name} ({me.username}) — id={me.id}")
    print("\n" + "=" * 60)
    print("Set this as TELEGRAM_SESSION_STRING on Railway:")
    print("=" * 60)
    print(session_string)
    print("=" * 60 + "\n")
    print("Never commit this string to git.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
