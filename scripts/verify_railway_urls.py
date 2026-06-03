#!/usr/bin/env python3
"""
verify_railway_urls.py — Pre-market Railway URL health check.

Runs at 6:55 AM ET via cron. Pings all 3 Railway health endpoints.
Alerts Ahmed via Telegram if any returns non-200.
Writes result to CHRONICLE adaptive_events.

S2 — Railway URL Audit (GENESIS, April 19 2026)
"""
import os
import sys
import json
import requests
from datetime import datetime

RAILWAY_ALPHA_URL  = os.getenv("RAILWAY_ALPHA_URL", "")
RAILWAY_PRIME_URL  = os.getenv("RAILWAY_PRIME_URL", "")
RAILWAY_AXIOM_URL  = os.getenv("RAILWAY_AXIOM_URL", "")
def _require(var: str) -> str:
    """Return env var value or raise at startup — never silently use a stale fallback."""
    val = os.getenv(var)
    if not val:
        raise RuntimeError(f"{var} is required but not set. Source .deploy-secrets before running.")
    return val

TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
AHMED_CHAT_ID      = os.getenv("AHMED_CHAT_ID", "8573754783")


def check(name: str, url: str) -> dict:
    """Ping a Railway service /health endpoint and return status dict."""
    if not url:
        return {"name": name, "status": "skipped", "reason": "env var not set"}
    try:
        r = requests.get(f"{url}/health", timeout=10)
        if r.status_code == 200:
            return {"name": name, "status": "ok", "code": 200}
        return {"name": name, "status": "error", "code": r.status_code}
    except Exception as e:
        return {"name": name, "status": "error", "reason": str(e)[:120]}


def alert_ahmed(text: str) -> None:
    """Send Telegram alert to Ahmed."""
    if not TELEGRAM_BOT_TOKEN:
        print(f"[NO TELEGRAM] {text}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": AHMED_CHAT_ID, "text": text},
            timeout=5,
        )
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")


if __name__ == "__main__":
    ts = datetime.now().strftime("%Y-%m-%d %H:%M ET")
    results = [
        check("alpha",  RAILWAY_ALPHA_URL),
        check("prime",  RAILWAY_PRIME_URL),
        check("axiom",  RAILWAY_AXIOM_URL),
    ]

    failed  = [r for r in results if r["status"] == "error"]
    skipped = [r for r in results if r["status"] == "skipped"]

    print(f"Railway URL check at {ts}: {json.dumps(results)}")

    if failed:
        names = ", ".join(r["name"] for r in failed)
        msg = (
            f"🚨 Railway URL check FAILED — {ts}\n"
            f"Failed: {names}\n"
            f"Details: {json.dumps(failed, indent=2)}"
        )
        alert_ahmed(msg)
        print(f"ALERT sent for: {names}")
        sys.exit(1)
    else:
        ok_names = [r["name"] for r in results if r["status"] == "ok"]
        print(f"All Railway URLs healthy: {ok_names}" + (f" | skipped: {[r['name'] for r in skipped]}" if skipped else ""))
        sys.exit(0)
