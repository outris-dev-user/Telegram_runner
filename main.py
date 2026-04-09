"""
telegram-lookup-service — FastAPI app.

Thin adapter: phones in → Telegram bot → raw JSON out.
Knows nothing about clients, SFTP, or customer IDs.

Endpoints:
    POST /lookup                  — submit phones CSV or JSON list
    GET  /lookup/{job_id}         — poll job status
    GET  /lookup/{job_id}/result  — download raw bot JSON (when done)

Required env vars:
    TELEGRAM_API_ID
    TELEGRAM_API_HASH
    SESSION_PATH                      (path to .session file, no extension)
    TELEGRAM_LOOKUP_BOT_USERNAME      (e.g. @MyDataBot)

Optional:
    JOB_TIMEOUT_SECONDS               (default 1800)
    BOT_REPLY_POLL_INTERVAL           (default 5)
    SERVICE_API_KEY                   if set, requests must include
                                      X-API-Key header matching this value
"""

import csv
import io
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

import telegram_worker as worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

SERVICE_API_KEY = os.environ.get("SERVICE_API_KEY")


# ---------------------------------------------------------------------------
# Lifespan — start/stop Telethon client
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await worker.start_worker()
    yield
    await worker.stop_worker()


app = FastAPI(title="telegram-lookup-service", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Auth dependency (optional — only enforced when SERVICE_API_KEY is set)
# ---------------------------------------------------------------------------
def require_api_key(request: Request):
    if not SERVICE_API_KEY:
        return  # no key configured → open (internal network only)
    key = request.headers.get("X-API-Key")
    if key != SERVICE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/lookup", dependencies=[Depends(require_api_key)])
async def submit_lookup(
    file: Optional[UploadFile] = File(default=None),
    phones_json: Optional[str] = Form(default=None),
    country_code: str = Form(default="91"),
):
    """
    Submit a batch of phones for lookup.

    Two input modes (pick one):
      1. Multipart file upload — CSV with a single column of phone numbers (no header).
      2. JSON body via form field `phones_json` — '["9876543210", ...]'

    Optional:
      country_code — digits only, default "91" (India). Applied to any number
                     that doesn't already have a country code (i.e. 10-digit numbers).

    Phone normalisation applied automatically:
      - Strips +, spaces, dashes
      - 10-digit numbers get country_code prepended → 12-digit
      - Numbers already 12+ digits are left as-is

    Bot minimum: 100 phones. Smaller batches will fail immediately with an
    informative error (use the per-phone fallback API for those).

    Returns: { "job_id": "<uuid>" }
    """
    phones = []

    if file is not None:
        content = await file.read()
        text = content.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if row:
                phone = row[0].strip()
                if phone:
                    phones.append(phone)

    elif phones_json is not None:
        try:
            phones = json.loads(phones_json)
            if not isinstance(phones, list):
                raise ValueError("phones_json must be a JSON array")
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    else:
        raise HTTPException(
            status_code=422,
            detail="Provide either a CSV file upload or phones_json form field",
        )

    if not phones:
        raise HTTPException(status_code=422, detail="No phone numbers found in input")

    if len(phones) > 100_000:
        raise HTTPException(
            status_code=422,
            detail=f"Batch too large: {len(phones)} phones (max 100,000)",
        )

    job_id = await worker.enqueue_job(phones, country_code=country_code)
    logger.info("Accepted job %s with %d phones", job_id, len(phones))
    return {"job_id": job_id}


@app.get("/lookup/{job_id}", dependencies=[Depends(require_api_key)])
async def get_job_status(job_id: str):
    """
    Poll job status.

    Returns:
        {
            "job_id": "...",
            "status": "queued|sending|waiting|done|failed",
            "phone_count": 5000,
            "message": "...",
            "error": null
        }
    """
    job = worker.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "phone_count": job["phone_count"],
        "message": job["message"],
        "error": job["error"],
        "result_type": job.get("result_type"),  # "csv" | "zip" | "unknown" | null (while running)
    }


_RESULT_TYPE_META = {
    "json":    ("application/json",  ".json"),
    "zip":     ("application/zip",   ".zip"),
    "unknown": ("application/octet-stream", ".bin"),
}


@app.get("/lookup/{job_id}/result", dependencies=[Depends(require_api_key)])
async def get_job_result(job_id: str):
    """
    Download the raw file the bot returned (CSV or ZIP).

    Returns 200 with correct Content-Type when job is done:
      - text/csv        for CSV replies
      - application/zip for ZIP replies (bot sends ZIP when result is large)
    Returns 202 Accepted with status body when job is still running.
    Returns 404 if job not found.
    Returns 500 if job failed.

    Callers should check the Content-Type (or the result_type field from GET
    /lookup/{job_id}) to know which format to parse.
    """
    job = worker.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    status = job["status"]

    if status == "done":
        result_type = job.get("result_type") or "unknown"
        media_type, ext = _RESULT_TYPE_META.get(result_type, _RESULT_TYPE_META["unknown"])
        return Response(
            content=job["result_bytes"],
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{job_id}{ext}"'},
        )

    if status == "failed":
        raise HTTPException(
            status_code=500,
            detail={"error": "job_failed", "message": job["error"]},
        )

    # still in progress
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": status,
            "message": job["message"],
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
