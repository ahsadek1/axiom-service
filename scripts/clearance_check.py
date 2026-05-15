#!/usr/bin/env python3
"""
clearance_check.py — G5: Pre-market clearance gate (9:25 AM ET).

Reads the 6 AM snapshot, queries live health, and blocks trading if AUTO_EXECUTE
state has diverged. Prints CLEARED or BLOCKED and exits accordingly.

Exit codes:
  0 — CLEARED (trading may proceed)
  1 — BLOCKED (state mismatch or snapshot missing — do not trade)
  2 — DEGRADED (one service unreachable — alert Ahmed but do not hard-block)
"""

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("clearance_check")

ALPHA_EXEC_URL     = os.getenv("ALPHA_EXEC_URL", "http://localhost:8005")
PRIME_EXEC_URL     = os.getenv("PRIME_EXEC_URL", "http://localhost:8006")
NEXUS_SECRET       = os.getenv("NEXUS_SECRET", "")
NEXUS_PRIME_SECRET = os.getenv("NEXUS_PRIME_SECRET", "")
SNAPSHOT_PATH      = os.getenv("SNAPSHOT_PATH",
                                "/Users/ahmedsadek/nexus/data/premarket_snapshot.json")
TELEGRAM_BOT       = os.getenv("TELEGRAM_BOT_TOKEN", "")
AHMED_CHAT_ID      = os.getenv("AHMED_CHAT_ID", "8573754783")


def _alert_ahmed(text: str) -> None:
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


def _query_health(url: str, secret_header: str, secret_val: str) -> dict:
    try:
        r = requests.get(f"{url}/health", headers={secret_header: secret_val}, timeout=8)
        if r.status_code == 200:
            return r.json()
        return {}
    except Exception:
        return {}


def _verify_snapshot_integrity(snapshot: dict) -> bool:
    """Return True if the snapshot SHA256 is valid."""
    stored_sig = snapshot.pop("sha256", None)
    if not stored_sig:
        return False
    recomputed = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True).encode()
    ).hexdigest()
    snapshot["sha256"] = stored_sig  # restore
    return stored_sig == recomputed


def main() -> int:
    ts_utc = datetime.now(timezone.utc).isoformat()

    # ── 1. Load snapshot ──────────────────────────────────────────────────────
    if not os.path.exists(SNAPSHOT_PATH):
        msg = f"🚨 Clearance check BLOCKED — no 6 AM snapshot found at {SNAPSHOT_PATH}"
        log.error(msg)
        _alert_ahmed(msg)
        return 1

    with open(SNAPSHOT_PATH) as f:
        snapshot = json.load(f)

    if not _verify_snapshot_integrity(snapshot):
        msg = "🚨 Clearance check BLOCKED — snapshot SHA256 mismatch (possible tampering)"
        log.error(msg)
        _alert_ahmed(msg)
        return 1

    snapshot_ae = snapshot.get("nexus_auto_execute")
    log.info("Snapshot: auto_execute=%s (captured %s)", snapshot_ae, snapshot.get("captured_at"))

    # ── 2. Query live health ──────────────────────────────────────────────────
    alpha_health = _query_health(ALPHA_EXEC_URL, "X-Nexus-Secret", NEXUS_SECRET)
    prime_health = _query_health(PRIME_EXEC_URL, "X-Nexus-Prime-Secret", NEXUS_PRIME_SECRET)

    alpha_live_ae = alpha_health.get("auto_execute", None)
    prime_live_ae = prime_health.get("auto_execute", None)

    alpha_reachable = bool(alpha_health)
    prime_reachable = bool(prime_health)

    log.info(
        "Live: alpha_auto_execute=%s prime_auto_execute=%s",
        alpha_live_ae, prime_live_ae,
    )

    # ── 3. Check for divergence ───────────────────────────────────────────────
    issues = []

    if not alpha_reachable:
        issues.append("Alpha Execution unreachable at clearance check")
    if not prime_reachable:
        issues.append("Prime Execution unreachable at clearance check")

    # Hard block: auto_execute state changed since 6 AM snapshot
    if alpha_reachable and alpha_live_ae != snapshot_ae:
        issues.append(
            f"AUTO_EXECUTE MISMATCH (alpha): snapshot={snapshot_ae} live={alpha_live_ae}"
        )
    if prime_reachable and prime_live_ae != snapshot.get("prime_auto_execute"):
        issues.append(
            f"AUTO_EXECUTE MISMATCH (prime): snapshot={snapshot.get('prime_auto_execute')} live={prime_live_ae}"
        )

    if issues:
        hard_block = any("MISMATCH" in i for i in issues)
        msg = (
            f"{'🚨 TRADING BLOCKED' if hard_block else '⚠️ Clearance degraded'} — {ts_utc}\n"
            + "\n".join(f"• {i}" for i in issues)
        )
        log.error(msg)
        _alert_ahmed(msg)
        return 1 if hard_block else 2

    # ── 4. Cleared ────────────────────────────────────────────────────────────
    mode = "LIVE" if alpha_live_ae else "DRY_RUN"
    log.info("✅ CLEARED — auto_execute=%s mode=%s at %s", alpha_live_ae, mode, ts_utc)
    print(f"CLEARED: auto_execute={alpha_live_ae} mode={mode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
