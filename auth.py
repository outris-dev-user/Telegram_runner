"""
One-time interactive auth helper.

Run this LOCALLY (not on Railway) to produce the .session file:
    python auth.py

Then upload the resulting telegram_session.session file to your Railway volume.
Never commit the .session file to git.

Requires env vars:
    TELEGRAM_API_ID
    TELEGRAM_API_HASH
    TELEGRAM_PHONE      (your phone number, e.g. +919876543210)
    SESSION_PATH        (optional, defaults to ./telegram_session)
"""

import asyncio
import os
from telethon import TelegramClient


async def main():
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    phone = os.environ["TELEGRAM_PHONE"]
    session_path = os.environ.get("SESSION_PATH", "./telegram_session")

    client = TelegramClient(session_path, api_id, api_hash)
    await client.start(phone=phone)

    me = await client.get_me()
    print(f"Logged in as: {me.first_name} ({me.username}) — id={me.id}")
    print(f"Session saved to: {session_path}.session")
    print("Upload this file to your Railway volume at the path set in SESSION_PATH.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
