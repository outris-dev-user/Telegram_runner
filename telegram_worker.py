"""
Telethon worker — owns the single Telegram session.

One job runs at a time (serial queue). Concurrent submissions are queued and
processed in order, which avoids session conflicts and Telegram flood-wait errors.

State lives entirely in memory — jobs are short-lived and Railway restarts are
rare. No DB needed.

Config (env vars):
    TELEGRAM_API_ID
    TELEGRAM_API_HASH
    TELEGRAM_SESSION_STRING       string session from auth.py (preferred, no volume needed)
    TELEGRAM_LOOKUP_BOT_USERNAME  e.g. @SomeDataBot
    DEFAULT_COUNTRY_CODE          digits only, default "91" (India)
    BOT_MIN_PHONES                minimum phones the bot accepts, default 100
    JOB_TIMEOUT_SECONDS           default 1800 (30 min)
    BOT_REPLY_POLL_INTERVAL       seconds between inbox checks (default 5)
"""

import asyncio
import csv
import io
import logging
import os
import re
import tempfile
import time
import uuid
from typing import Dict, Optional

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "")
BOT_USERNAME = os.environ["TELEGRAM_LOOKUP_BOT_USERNAME"]
DEFAULT_COUNTRY_CODE = os.environ.get("DEFAULT_COUNTRY_CODE", "91")
BOT_MIN_PHONES = int(os.environ.get("BOT_MIN_PHONES", "100"))
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SECONDS", "1800"))

# Text fragments in plain-text (no-button) replies that indicate a hard rejection
_BOT_REJECTION_PHRASES = [
    "too few requests",
    "minimum file volume",
    "minimum number",
    "invalid file",
    "unsupported format",
]

# Text fragments that indicate the bot is waiting for us to click a confirm button
_BOT_CONFIRM_PHRASES = [
    "confirm the start",
    "total cost of execution",
]

# ---------------------------------------------------------------------------
# Phone normalisation
# ---------------------------------------------------------------------------

def normalize_phone(raw: str, country_code: str = DEFAULT_COUNTRY_CODE) -> str:
    """
    Normalise a phone number to E.164-ish digits with country code prepended.

    Rules (in order):
      1. Strip all non-digit characters (+, spaces, dashes, parentheses).
      2. If the result is already 12+ digits (e.g. 919876543210), return as-is.
      3. If the result is 10 digits, prepend the default country code.
      4. Anything else is returned as-is (caller decides what to do).
    """
    digits = re.sub(r"\D", "", raw)
    cc = re.sub(r"\D", "", country_code)

    if len(digits) >= len(cc) + 10:
        # Already has a country code (or longer — leave it alone)
        return digits
    if len(digits) == 10:
        return cc + digits
    # Unexpected length — return stripped digits, let the bot reject if invalid
    return digits


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
        "result_bytes": None,   # raw bytes returned by the bot (CSV or ZIP)
        "result_type": None,    # "csv" | "zip" | "unknown"
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
    if not SESSION_STRING:
        raise RuntimeError(
            "TELEGRAM_SESSION_STRING env var is not set. "
            "Run auth.py locally to generate it, then set it on Railway."
        )
    _client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await _client.connect()
    if not await _client.is_user_authorized():
        raise RuntimeError(
            "Telegram session string is invalid or expired. "
            "Re-run auth.py locally to generate a fresh session string."
        )
    me = await _client.get_me()
    logger.info("Telegram client ready — logged in as %s (id=%s)", me.username, me.id)

    asyncio.create_task(_worker_loop())


async def stop_worker():
    global _client
    if _client:
        await _client.disconnect()


async def enqueue_job(phones: list[str], country_code: str = DEFAULT_COUNTRY_CODE) -> str:
    """
    Normalise phones to include country code, then add to the serial queue.
    Returns job_id immediately.
    """
    normalised = [normalize_phone(p, country_code) for p in phones]
    job_id = _new_job(len(normalised))
    await _job_queue.put((job_id, normalised))
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

    # Enforce bot minimum before even sending
    if len(phones) < BOT_MIN_PHONES:
        job["status"] = "failed"
        job["error"] = (
            f"Too few phones: {len(phones)} submitted, bot requires minimum {BOT_MIN_PHONES}. "
            "Use the fallback /api/investigate/phone endpoint for small batches."
        )
        logger.warning("[%s] rejected before send — only %d phones", job_id, len(phones))
        return

    job["status"] = "sending"
    job["message"] = f"Sending {len(phones)} phones to bot"
    logger.info("[%s] sending %d phones to %s", job_id, len(phones), BOT_USERNAME)

    csv_bytes = _phones_to_csv_bytes(phones)
    deadline = time.time() + JOB_TIMEOUT

    while True:
        try:
            result_bytes = await _send_and_wait(job_id, csv_bytes, deadline)
            break
        except FloodWaitError as e:
            wait = e.seconds + 5
            logger.warning("[%s] FloodWait — sleeping %ds", job_id, wait)
            job["message"] = f"FloodWait — resuming in {wait}s"
            await asyncio.sleep(wait)
        except _BotRejectedError as e:
            job["status"] = "failed"
            job["error"] = f"Bot rejected the request: {e}"
            logger.error("[%s] bot rejected: %s", job_id, e)
            return
        except asyncio.TimeoutError:
            job["status"] = "failed"
            job["error"] = f"Timed out after {JOB_TIMEOUT}s waiting for bot reply"
            logger.error("[%s] timed out", job_id)
            return

    result_bytes, result_type = result_bytes
    job["status"] = "done"
    job["message"] = f"Bot replied with result ({result_type})"
    job["result_bytes"] = result_bytes
    job["result_type"] = result_type
    logger.info("[%s] done — %d bytes received, type=%s", job_id, len(result_bytes), result_type)


class _BotRejectedError(Exception):
    pass


async def _send_and_wait(job_id: str, csv_bytes: bytes, deadline: float) -> tuple[bytes, str]:
    """
    Upload the phone CSV to the bot, wait for a document reply.

    Returns:
        (result_bytes, result_type) where result_type is "csv", "zip", or "unknown"

    Raises:
        asyncio.TimeoutError   — deadline exceeded
        _BotRejectedError      — bot sent a rejection text (too few lines, bad format, etc.)
    """
    job = jobs[job_id]
    bot_entity = await _client.get_entity(BOT_USERNAME)

    reply_future: asyncio.Future = asyncio.get_event_loop().create_future()
    confirmed = False   # True after we clicked Confirm
    downloaded = False  # True after we clicked Download Results

    @_client.on(events.NewMessage(chats=bot_entity))
    async def _on_message(event):
        nonlocal confirmed, downloaded

        if reply_future.done():
            return
        if event.document:
            reply_future.set_result(("document", event))
            return

        if not event.text:
            return

        text_lower = event.text.lower()
        logger.info("[%s] bot text (confirmed=%s downloaded=%s): %s",
                    job_id, confirmed, downloaded, event.text[:300])

        # After clicking Download Results, ignore progress text — only waiting for document
        if downloaded:
            return

        # Fetch buttons
        buttons = event.buttons
        if buttons is None:
            try:
                buttons = await event.get_buttons()
            except Exception:
                buttons = None

        all_buttons = [btn for row in buttons for btn in row] if buttons else []
        button_texts = [repr(btn.text) for btn in all_buttons if btn.text]
        if button_texts:
            logger.info("[%s] bot buttons: %s", job_id, button_texts)

        # --- Stage 1: not yet confirmed — look for Confirm button ---
        if not confirmed:
            confirm_btn = next(
                (b for b in all_buttons if b.text and "confirm" in b.text.lower()), None
            )
            if confirm_btn:
                job["message"] = "Clicking Confirm..."
                try:
                    await confirm_btn.click()
                    confirmed = True
                    job["message"] = "Confirmed — waiting for search to complete..."
                    logger.info("[%s] clicked Confirm: %r", job_id, confirm_btn.text)
                except Exception as e:
                    logger.warning("[%s] Confirm click failed: %s", job_id, e)
                    reply_future.set_result(("rejected", f"Could not click Confirm: {e}"))
            elif all_buttons:
                # Buttons present but no Confirm — rejection before search started
                logger.warning("[%s] no Confirm button — rejection: %s", job_id, button_texts)
                reply_future.set_result(("rejected", event.text))
            else:
                # Plain text, no buttons — check hard rejection phrases
                if any(phrase in text_lower for phrase in _BOT_REJECTION_PHRASES):
                    reply_future.set_result(("rejected", event.text))
            return

        # --- Stage 2: confirmed, search running — look for Download Results button ---
        download_btn = next(
            (b for b in all_buttons if b.text and "download" in b.text.lower()), None
        )
        if download_btn:
            job["message"] = "Search complete — clicking Download Results..."
            try:
                await download_btn.click()
                downloaded = True
                job["message"] = "Downloading results from bot..."
                logger.info("[%s] clicked Download Results: %r", job_id, download_btn.text)
            except Exception as e:
                logger.warning("[%s] Download click failed: %s", job_id, e)
                reply_future.set_result(("rejected", f"Could not click Download Results: {e}"))
        # Otherwise it's a progress update — log already done above, keep waiting

    try:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp.write(csv_bytes)
            tmp_path = tmp.name

        job["status"] = "waiting"
        job["message"] = "Sending file to bot..."

        await _client.send_file(bot_entity, tmp_path, caption="batch lookup")
        os.unlink(tmp_path)
        job["message"] = "Waiting for bot reply..."

        timeout = max(deadline - time.time(), 0)
        kind, payload = await asyncio.wait_for(reply_future, timeout=timeout)

        if kind == "rejected":
            raise _BotRejectedError(payload)

        # kind == "document" — detect file type from mime_type or filename
        result_type = _detect_result_type(payload.document)
        logger.info("[%s] bot document: mime=%s filename=%s → type=%s",
                    job_id,
                    getattr(payload.document, "mime_type", "?"),
                    _get_filename(payload.document),
                    result_type)

        result_bytes = await payload.download_media(bytes)
        return result_bytes, result_type

    finally:
        _client.remove_event_handler(_on_message, events.NewMessage)


def _detect_result_type(document) -> str:
    """Classify bot document as 'json', 'zip', or 'unknown'."""
    mime = getattr(document, "mime_type", "") or ""
    filename = _get_filename(document).lower()

    if mime in ("application/zip", "application/x-zip-compressed") or filename.endswith(".zip"):
        return "zip"
    if mime in ("application/json", "text/plain") or filename.endswith(".json"):
        return "json"
    return "unknown"


def _get_filename(document) -> str:
    """Extract filename from document attributes, fallback to empty string."""
    for attr in getattr(document, "attributes", []):
        name = getattr(attr, "file_name", None)
        if name:
            return name
    return ""


def _phones_to_csv_bytes(phones: list[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    for phone in phones:
        writer.writerow([phone])
    # Strip trailing newline so the bot doesn't count an empty last line
    return buf.getvalue().rstrip("\r\n").encode("utf-8")
