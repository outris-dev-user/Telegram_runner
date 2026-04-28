#!/usr/bin/env python3
"""
Debug helper for telegram-lookup-service.

Stdlib-only — runs on any clean Python 3.8+ without pip installs.

Usage:
    python debug.py status
    python debug.py watch <job_id> [--save <path>]
    python debug.py submit <csv_file> [--country 91] [--save <path>]

Env vars:
    TELEGRAM_RUNNER_URL    base URL (default: https://telegramrunner-staging.up.railway.app)
    SERVICE_API_KEY        required for every endpoint except /health

Examples:
    # Pre-flight before a batch:
    SERVICE_API_KEY=xxx python debug.py status

    # Tail a job that number-lookup just submitted, save result on completion:
    SERVICE_API_KEY=xxx python debug.py watch 7c817497-... --save out.json

    # End-to-end smoke test from a small CSV:
    SERVICE_API_KEY=xxx python debug.py submit phones_101.csv --save out.json
"""

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://telegramrunner-staging.up.railway.app"
POLL_INTERVAL = 10


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _client() -> tuple[str, str]:
    base = os.environ.get("TELEGRAM_RUNNER_URL", DEFAULT_URL).rstrip("/")
    key = os.environ.get("SERVICE_API_KEY")
    if not key:
        sys.exit("SERVICE_API_KEY env var is required")
    return base, key


def _request(method: str, path: str, base: str, key: str, *, data=None, content_type=None):
    headers = {"X-API-Key": key}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def cmd_status(_args):
    base, key = _client()
    code, body, _ = _request("GET", "/status", base, key)
    try:
        info = json.loads(body)
    except json.JSONDecodeError:
        print(f"[{_ts()}] HTTP {code}: {body[:300].decode('utf-8', 'replace')}")
        sys.exit(1)

    print(f"[{_ts()}] HTTP {code}  healthy={info.get('healthy')}")
    for k in ("client_connected", "client_authorized", "bot_reachable"):
        print(f"  {k:<20} {info.get(k)}")
    if info.get("session_user"):
        u = info["session_user"]
        print(f"  session_user         @{u.get('username')} (id={u.get('id')})")
    if info.get("bot_info"):
        b = info["bot_info"]
        print(f"  bot_info             @{b.get('username')} (id={b.get('id')})")
    if info.get("active_job"):
        print(f"  active_job           {info['active_job']}")
    if info.get("error"):
        print(f"  ERROR                {info['error']}")
    sys.exit(0 if info.get("healthy") else 1)


def cmd_watch(args):
    base, key = _client()
    job_id = args.job_id
    last_status = last_message = None
    print(f"[{_ts()}] watching job {job_id}")

    while True:
        code, body, _ = _request("GET", f"/lookup/{job_id}", base, key)
        if code == 404:
            sys.exit(f"[{_ts()}] job not found")
        if code != 200:
            print(f"[{_ts()}] HTTP {code}: {body[:300].decode('utf-8', 'replace')}")
            sys.exit(1)

        s = json.loads(body)
        status = s["status"]
        message = s.get("message")
        if status != last_status or message != last_message:
            print(f"[{_ts()}] status={status:<8} message={message}")
            last_status, last_message = status, message
        if status in ("done", "failed"):
            break
        time.sleep(POLL_INTERVAL)

    if status == "failed":
        print(f"[{_ts()}] FAILED  recoverable={s.get('recoverable')}  error={s.get('error')}")
        sys.exit(2)

    print(f"[{_ts()}] DONE  result_type={s.get('result_type')}")

    if args.save:
        rcode, rbody, rhdrs = _request("GET", f"/lookup/{job_id}/result", base, key)
        if rcode != 200:
            print(f"[{_ts()}] result fetch HTTP {rcode}: {rbody[:300].decode('utf-8', 'replace')}")
            sys.exit(1)
        with open(args.save, "wb") as f:
            f.write(rbody)
        ctype = rhdrs.get("Content-Type", "?")
        print(f"[{_ts()}] result saved to {args.save} ({len(rbody)} bytes, {ctype})")


def _build_multipart(csv_path: str, country_code: str) -> tuple[bytes, str]:
    boundary = "----debugBoundary" + str(int(time.time()))
    fname = os.path.basename(csv_path)
    with open(csv_path, "rb") as f:
        file_bytes = f.read()

    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'.encode())
    buf.write(b"Content-Type: text/csv\r\n\r\n")
    buf.write(file_bytes)
    buf.write(b"\r\n")
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(b'Content-Disposition: form-data; name="country_code"\r\n\r\n')
    buf.write(country_code.encode())
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def cmd_submit(args):
    base, key = _client()
    if not os.path.isfile(args.csv):
        sys.exit(f"file not found: {args.csv}")

    body, ctype = _build_multipart(args.csv, args.country)
    code, resp_body, _ = _request("POST", "/lookup", base, key, data=body, content_type=ctype)
    if code != 200:
        print(f"[{_ts()}] submit HTTP {code}: {resp_body[:500].decode('utf-8', 'replace')}")
        sys.exit(1)

    job_id = json.loads(resp_body)["job_id"]
    print(f"[{_ts()}] submitted job_id={job_id}")
    args.job_id = job_id
    cmd_watch(args)


def main():
    ap = argparse.ArgumentParser(
        description="Debug helper for telegram-lookup-service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="run deep readiness check (/status)")

    w = sub.add_parser("watch", help="poll an existing job until done/failed")
    w.add_argument("job_id")
    w.add_argument("--save", help="path to save the result file when status=done")

    s = sub.add_parser("submit", help="POST a CSV and watch the resulting job")
    s.add_argument("csv", help="single-column CSV of phone numbers, no header")
    s.add_argument("--country", default="91", help="country code prefix (default 91)")
    s.add_argument("--save", help="path to save the result file when status=done")

    args = ap.parse_args()
    {"status": cmd_status, "watch": cmd_watch, "submit": cmd_submit}[args.cmd](args)


if __name__ == "__main__":
    main()
