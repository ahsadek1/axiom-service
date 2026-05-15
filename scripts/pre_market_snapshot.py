#!/usr/bin/env python3
"""
pre_market_snapshot.py — G5: Pre-market AUTO_EXECUTE state snapshot.

Runs at 6:00 AM ET via launchd/cron.
Captures nexus_auto_execute state from both execution services, SHA256-signs it,
writes to /Users/ahmedsadek/nexus/data/premarket_snapshot.json.

The clearance_check.py (runs at 9:25 AM) reads this snapshot and blocks trading
if the live state has diverged from what was captured at 6 AM.
"""

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pre_market_snapshot")

ALPHA_EXEC_URL  = os.getenv("ALPHA_EXEC_URL", "http://localhost:8005")
PRIME_EXEC_URL  = os.getenv("PRIME_EXEC_URL", "http://localhost:8006")
NEXUS_SECRET    = os.getenv("NEXUS_SECRET", "")
NEXUS_PRIME_SECRET = os.getenv("NEXUS_PRIME_SECRET", "")
SNAPSHOT_PATH   = os.getenv("SNAPSHOT_PATH",
                             "/Users/ahmedsadek/nexus/data/premarket_snapshot.json")

TELEGRAM_BOT    = os.getenv("TELEGRAM_BOT_TOKEN", "")
AHMED_CHAT_ID   = os.getenv("AHMED_CHAT_ID", "8573754783")


def _query_health(url: str, secret_header: str, secret_val: str) -> dict:
    """Query /health and return parsed JSON. Returns {} on any error."""
    try:
        r = requests.get(
            f"{url}/health",
            headers={secret_header: secret_val},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
        log.warning("Health check %s returned %d", url, r.status_code)
        return {}
    except Exception as exc:
        log.error("Health check %s failed: %s", url, exc)
        return {}


def _alert_ahmed(text: str) -> None:
    """Send Telegram alert to Ahmed."""
    if not TELEGRAM_BOT:
        log.warning("TELEGRAM_BOT_TOKEN not set — alert not sent: %s", text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage",
            json={"chat_id": AHMED_CHAT_ID, "text": text},
            timeout=5,
        )
    except Exception as exc:
        log.error("Telegram alert failed: %s", exc)


def main() -> int:
    """Capture and sign pre-market AUTO_EXECUTE snapshot. Returns 0 on success."""
    ts_utc = datetime.now(timezone.utc).isoformat()

    alpha_health = _query_health(ALPHA_EXEC_URL, "X-Nexus-Secret", NEXUS_SECRET)
    prime_health = _query_health(PRIME_EXEC_URL, "X-Nexus-Prime-Secret", NEXUS_PRIME_SECRET)

    alpha_ae = alpha_health.get("auto_execute", None)
    prime_ae = prime_health.get("auto_execute", None)
    alpha_mode = alpha_health.get("mode", "unknown")
    prime_mode = prime_health.get("mode", "unknown")

    snapshot = {
        "captured_at":            ts_utc,
        "nexus_auto_execute":     alpha_ae,       # canonical: from alpha-execution
        "alpha_auto_execute":     alpha_ae,
        "prime_auto_execute":     prime_ae,
        "alpha_mode":             alpha_mode,
        "prime_mode":             prime_mode,
        "alpha_reachable":        bool(alpha_health),
        "prime_reachable":        bool(prime_health),
    }

    # SHA256 signature for tamper detection at clearance check time
    snapshot_bytes = json.dumps(snapshot, sort_keys=True).encode()
    snapshot["sha256"] = hashlib.sha256(snapshot_bytes).hexdigest()

    # Ensure data dir exists
    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)

    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(snapshot, f, indent=2)

    log.info(
        "Snapshot written: alpha_auto_execute=%s (mode=%s) prime_auto_execute=%s (mode=%s)",
        alpha_ae, alpha_mode, prime_ae, prime_mode,
    )

    # Alert if unreachable — trading may not be monitored correctly
    issues = []
    if not snapshot["alpha_reachable"]:
        issues.append("Alpha Execution unreachable at 6 AM snapshot")
    if not snapshot["prime_reachable"]:
        issues.append("Prime Execution unreachable at 6 AM snapshot")

    if issues:
        msg = "⚠️ Pre-market snapshot issues:\n" + "\n".join(f"• {i}" for i in issues)
        _alert_ahmed(msg)
        log.warning("Snapshot issues: %s", issues)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
