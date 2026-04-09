"""
Telethon worker — owns the single Telegram session.

One job runs at a time (serial queue). Concurrent submissions are queued and
processed in order, which avoids session conflicts and Telegram flood-wait errors.

State lives entirely in memory — jobs are short-lived and Railway restarts are
rare. No DB needed.

Config (env vars):
    TELEGRAM_API_ID
    TELEGRAM_API_HASH
    SESSION_PATH              path to .session file (without extension)
    TELEGRAM_LOOKUP_BOT_USERNAME  e.g. @SomeDataBot
    JOB_TIMEOUT_SECONDS       default 1800 (30 min)
    BOT_REPLY_POLL_INTERVAL   seconds between inbox checks (default 5)
"""

import asyncio
import csv
import io
import json
import logging
import os
import tempfile
import time
import uuid
from typing import Dict, Optional

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION_PATH = os.environ.get("SESSION_PATH", "/data/telegram_session")
BOT_USERNAME = os.environ["TELEGRAM_LOOKUP_BOT_USERNAME"]
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SECONDS", "1800"))
POLL_INTERVAL = int(os.environ.get("BOT_REPLY_POLL_INTERVAL", "5"))

# ---------------------------------------------------------------------------
# Job store (in-memory)
# ---------------------------------------------------------------------------
jobs: Dict[str, dict] = {}


def _new_job(phone_count: int) -> str:
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "phone_count": phone_count,
        "message": "Queued, waiting for worker slot",
        "error": None,
        "result_bytes": None,   # raw JSON bytes returned by the bot
        "created_at": time.time(),
    }
    return job_id


def get_job(job_id: str) -> Optional[dict]:
    return jobs.get(job_id)


# ---------------------------------------------------------------------------
# Serial job queue
# ---------------------------------------------------------------------------
_job_queue: asyncio.Queue = asyncio.Queue()
_client: Optional[TelegramClient] = None


async def start_worker():
    """Initialise the Telethon client and start the serial worker loop."""
    global _client
    _client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    # connect() without interactive auth — session file must already exist
    await _client.connect()
    if not await _client.is_user_authorized():
        raise RuntimeError(
            "Telegram session not authorised. Run auth.py locally first, "
            "then upload the .session file to the Railway volume."
        )
    me = await _client.get_me()
    logger.info("Telegram client ready — logged in as %s (id=%s)", me.username, me.id)

    asyncio.create_task(_worker_loop())


async def stop_worker():
    global _client
    if _client:
        await _client.disconnect()


async def enqueue_job(phones: list[str]) -> str:
    """Add a job to the serial queue and return its job_id immediately."""
    job_id = _new_job(len(phones))
    await _job_queue.put((job_id, phones))
    return job_id


# ---------------------------------------------------------------------------
# Worker loop — processes one job at a time
# ---------------------------------------------------------------------------
async def _worker_loop():
    logger.info("Worker loop started")
    while True:
        job_id, phones = await _job_queue.get()
        try:
            await _process_job(job_id, phones)
        except Exception as exc:
            logger.exception("Unhandled error in worker for job %s", job_id)
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = str(exc)
        finally:
            _job_queue.task_done()


async def _process_job(job_id: str, phones: list[str]):
    job = jobs[job_id]
    job["status"] = "sending"
    job["message"] = f"Sending {len(phones)} phones to bot"
    logger.info("[%s] sending %d phones to %s", job_id, len(phones), BOT_USERNAME)

    # Build a CSV with one phone per line (no header — match what the bot expects)
    csv_bytes = _phones_to_csv_bytes(phones)

    deadline = time.time() + JOB_TIMEOUT
    result_bytes: Optional[bytes] = None

    while True:
        try:
            result_bytes = await _send_and_wait(job_id, csv_bytes, deadline)
            break
        except FloodWaitError as e:
            wait = e.seconds + 5
            logger.warning("[%s] FloodWait — sleeping %ds", job_id, wait)
            job["message"] = f"FloodWait — resuming in {wait}s"
            await asyncio.sleep(wait)
            # retry
        except asyncio.TimeoutError:
            job["status"] = "failed"
            job["error"] = f"Timed out after {JOB_TIMEOUT}s waiting for bot reply"
            logger.error("[%s] timed out", job_id)
            return

    job["status"] = "done"
    job["message"] = "Bot replied with result"
    job["result_bytes"] = result_bytes
    logger.info("[%s] done — %d bytes received", job_id, len(result_bytes))


async def _send_and_wait(job_id: str, csv_bytes: bytes, deadline: float) -> bytes:
    """
    Upload the phone CSV to the bot, wait for a document reply, return its bytes.
    Raises asyncio.TimeoutError if deadline is exceeded.
    """
    job = jobs[job_id]
    bot_entity = await _client.get_entity(BOT_USERNAME)

    # Use a Future to receive the reply via an event handler
    reply_future: asyncio.Future = asyncio.get_event_loop().create_future()

    @_client.on(events.NewMessage(chats=bot_entity))
    async def _on_message(event):
        if reply_future.done():
            return
        # We expect the bot to reply with a document (JSON file)
        if event.document:
            reply_future.set_result(event)
        elif event.text:
            # Bot may send a text status — log it but keep waiting
            logger.debug("[%s] bot text: %s", job_id, event.text[:200])

    try:
        # Send the CSV as a file
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp.write(csv_bytes)
            tmp_path = tmp.name

        job["status"] = "waiting"
        job["message"] = "Waiting for bot reply..."

        await _client.send_file(bot_entity, tmp_path, caption="batch lookup")
        os.unlink(tmp_path)

        timeout = max(deadline - time.time(), 0)
        reply_event = await asyncio.wait_for(reply_future, timeout=timeout)

        # Download the document bytes
        result_bytes = await reply_event.download_media(bytes)
        return result_bytes

    finally:
        _client.remove_event_handler(_on_message, events.NewMessage)


def _phones_to_csv_bytes(phones: list[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    for phone in phones:
        writer.writerow([phone])
    return buf.getvalue().encode("utf-8")
