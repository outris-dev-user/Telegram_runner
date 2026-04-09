# Telegram Lookup Service — Client Reference

A thin HTTP adapter: send phone numbers in, get raw bot intelligence out.
The service knows nothing about customers, SFTP, or downstream processing.
All of that lives in the calling service (e.g. `number-lookup`).

---

## Base URL

| Environment | URL |
|---|---|
| Staging | `https://telegramrunner-staging.up.railway.app` |
| Production | _(set when provisioned)_ |

## Authentication

All endpoints require the `X-API-Key` header when `SERVICE_API_KEY` is configured on the server.

```
X-API-Key: <your-key>
```

If the key is wrong or missing: **401 Unauthorized**.

---

## Concurrency model — strict one-at-a-time

The service holds **a single Telegram session**. Only one job may exist at any time.

A job "occupies the slot" from the moment it is submitted until:
- the caller fetches its result (`GET /result` on a `done` job), or
- the caller deletes it (`DELETE /lookup/{job_id}`), or
- an admin uploads a manual result and the caller then fetches it.

Submitting while a job occupies the slot returns **409**. The caller must drain the current result before sending the next batch.

---

## Result format

The bot returns results as **NDJSON** — one JSON object per line, one per phone:

```json
{"NumOfResults": 1, "List": {"Source": {"Data": [{"Phone": "919876543210", "FullName": "Name Here", "Region": "MH"}], "NumOfResults": 1}}, "NumOfDatabase": 1, "search time": 0.066, "price": 0.001, "request": "919876543210"}
{"NumOfResults": 0, "List": {"No results found": {"Data": [{}], "NumOfResults": 1}}, "request": "919111222333"}
```

Parse it by splitting on `\n` and JSON-parsing each non-empty line. For batches over ~500 phones the bot may return a ZIP containing this NDJSON file — the `result_type` field tells you which.

---

## API reference

### POST /lookup — submit a batch

Submit phones for lookup. Returns a `job_id` immediately; the actual search runs asynchronously.

**Minimum batch size: 101 phones** (bot rejects smaller batches).

**Request — option A: JSON list**

```http
POST /lookup
Content-Type: multipart/form-data
X-API-Key: <key>

phones_json=["9876543210","9123456789",...]
country_code=91
```

**Request — option B: CSV file upload**

```http
POST /lookup
Content-Type: multipart/form-data
X-API-Key: <key>

file=<upload: single-column CSV, no header>
country_code=91
```

Phone normalisation applied automatically:
- Strips `+`, spaces, dashes
- 10-digit numbers get `country_code` prepended → 12-digit
- Numbers already 12+ digits are left as-is

**Response 200**

```json
{ "job_id": "7c817497-589a-4d04-9256-e7fa8e61c515" }
```

**Response 409 — slot busy**

```json
{
  "error": "service_busy",
  "message": "Service is busy: job <id> is in state 'waiting'. Fetch its result, DELETE it, or provide a manual result before submitting a new batch.",
  "active_job": {
    "job_id": "...",
    "status": "waiting",
    "recoverable": false
  }
}
```

---

### GET /lookup/{job_id} — poll status

Poll until `status` is `done` or `failed`. Recommended interval: 10–15 seconds.

**Response 200**

```json
{
  "job_id": "...",
  "status": "waiting",
  "phone_count": 143,
  "message": "Confirmed — waiting for search to complete...",
  "error": null,
  "result_type": null,
  "recoverable": false
}
```

**Status values**

| Status | Meaning |
|---|---|
| `queued` | In the async queue, not yet sent to Telegram |
| `sending` | Uploading phone list to the bot |
| `waiting` | Bot is running the search |
| `done` | Result ready — fetch with `GET /result` |
| `failed` | Something went wrong — check `error` and `recoverable` |

**When `status=failed`:**
- `recoverable: false` — bot never produced a result; safe to resubmit.
- `recoverable: true` — bot produced a result but the service failed to download it. Do NOT resubmit yet — admin must recover the result first (see Recovery Flow below).

---

### GET /lookup/{job_id}/result — fetch result

Fetch the raw result file. **Also clears the job slot** on success, so the service immediately accepts the next batch.

**Response 200 — success**

```
Content-Type: application/json          (for JSON results)
Content-Type: application/zip           (for ZIP results)
Content-Disposition: attachment; filename="<job_id>.json"
```

Body is NDJSON (JSON result) or a ZIP archive containing an NDJSON file (ZIP result).
Parse each non-empty line as a separate JSON object.

**Response 202 — still running**

```json
{ "job_id": "...", "status": "waiting", "message": "Confirmed — waiting for search to complete..." }
```

Keep polling until you get 200 or 500.

**Response 500 — non-recoverable failure**

Job slot is cleared automatically. Safe to resubmit.

```json
{ "error": "job_failed", "message": "Bot rejected the request: Too few requests" }
```

**Response 500 — recoverable failure**

Job slot remains held. See Recovery Flow.

```json
{
  "error": "job_failed_recoverable",
  "message": "Bot sent result file but download_media failed after 2 attempts: ...",
  "recovery": {
    "action": "manual_fetch_from_bot",
    "bot_username": "@LookupBot",
    "failed_at": 1712345678.0,
    "phone_count": 143,
    "job_id": "...",
    "manual_upload_endpoint": "/lookup/<id>/manual-result",
    "abandon_endpoint": "/lookup/<id>",
    "instructions": "The bot @LookupBot produced a result for this batch of 143 phones but telegram-runner could not download it ..."
  }
}
```

---

### POST /lookup/{job_id}/manual-result — admin recovery upload

Upload a result file that was manually downloaded from the bot.
Only valid when `status=failed` and `recoverable=true`.

```http
POST /lookup/{job_id}/manual-result
Content-Type: multipart/form-data
X-API-Key: <key>

file=<upload: .json or .zip file>
```

**Response 200**

```json
{ "job_id": "...", "status": "done", "bytes": 136361, "result_type": "json" }
```

After this, call `GET /lookup/{job_id}/result` as normal to retrieve the data.

---

### DELETE /lookup/{job_id} — abandon a job

Clears the job slot without retrieving data. Use when:
- A recoverable failure is unrecoverable (bot reply gone, data lost) and you want to resubmit.
- A non-critical failure occupies the slot and you want to move on.

```http
DELETE /lookup/{job_id}
X-API-Key: <key>
```

**Response 200**

```json
{ "job_id": "...", "cleared": true }
```

---

### GET /health — liveness check

```http
GET /health
```

```json
{ "status": "ok" }
```

No auth required.

---

## Typical happy-path flow

```
POST /lookup          →  { job_id }
  ↓
GET /lookup/{id}      →  { status: "queued" }      poll every 10s
GET /lookup/{id}      →  { status: "waiting" }
GET /lookup/{id}      →  { status: "done" }
  ↓
GET /lookup/{id}/result  →  200 NDJSON bytes        slot cleared
  ↓
POST /lookup (next batch)
```

---

## Recovery flow (recoverable failure)

```
GET /lookup/{id}/result  →  500 job_failed_recoverable
  ↓
  [calling service emails admin@outris.com with recovery.instructions from the response]
  ↓
  [admin opens Telegram, downloads result file from bot manually]
  ↓
POST /lookup/{id}/manual-result  (admin uploads file)  →  200 status=done
  ↓
GET /lookup/{id}/result  →  200 NDJSON bytes        slot cleared
  ↓
POST /lookup (next batch)

--- OR if bot reply is gone ---

DELETE /lookup/{id}     →  200 cleared=true        slot cleared
  ↓
POST /lookup (resubmit same batch)
```

---

## Calling from Python (number-lookup service)

```python
import httpx
import json

BASE_URL = "https://telegramrunner-staging.up.railway.app"
API_KEY  = "..."  # SERVICE_API_KEY env var

headers = {"X-API-Key": API_KEY}


async def run_telegram_lookup(phones: list[str], country_code: str = "91") -> bytes:
    """
    Submit batch, poll until done, return raw NDJSON bytes.
    Raises on non-recoverable failure. Returns bytes on success.
    On recoverable failure raises a RecoverableError with the recovery dict
    so the caller can email admin@outris.com.
    """
    async with httpx.AsyncClient(timeout=30) as client:

        # 1. Submit
        r = client.post(f"{BASE_URL}/lookup", headers=headers, data={
            "phones_json": json.dumps(phones),
            "country_code": country_code,
        })
        r.raise_for_status()
        job_id = r.json()["job_id"]

        # 2. Poll
        import asyncio
        while True:
            await asyncio.sleep(10)
            r = client.get(f"{BASE_URL}/lookup/{job_id}", headers=headers)
            r.raise_for_status()
            s = r.json()
            if s["status"] == "done":
                break
            if s["status"] == "failed":
                raise RuntimeError(f"Job failed: {s['error']}")

        # 3. Fetch result (clears slot)
        r = client.get(f"{BASE_URL}/lookup/{job_id}/result", headers=headers)
        if r.status_code == 500:
            detail = r.json().get("detail", {})
            if detail.get("error") == "job_failed_recoverable":
                raise RecoverableError(detail["recovery"])
            raise RuntimeError(detail.get("message", "Unknown failure"))
        r.raise_for_status()
        return r.content  # raw NDJSON bytes


def parse_ndjson(raw: bytes) -> list[dict]:
    """Parse NDJSON result into one dict per phone."""
    results = []
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            results.append(json.loads(line))
    return results


class RecoverableError(Exception):
    def __init__(self, recovery: dict):
        self.recovery = recovery
        super().__init__(recovery.get("instructions", "Recoverable failure"))
```

---

## Environment variables (server-side reference)

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_API_ID` | Yes | — | From my.telegram.org |
| `TELEGRAM_API_HASH` | Yes | — | From my.telegram.org |
| `TELEGRAM_SESSION_STRING` | Yes | — | Generated once via `auth.py`, paste as Railway env var |
| `TELEGRAM_LOOKUP_BOT_USERNAME` | Yes | — | e.g. `@LookupBot` |
| `SERVICE_API_KEY` | No | _(open)_ | Set to require `X-API-Key` on all requests |
| `DEFAULT_COUNTRY_CODE` | No | `91` | Country code prepended to 10-digit numbers |
| `BOT_MIN_PHONES` | No | `101` | Minimum phones enforced before sending to bot |
| `JOB_TIMEOUT_SECONDS` | No | `1800` | Max seconds to wait for bot reply before failing |
