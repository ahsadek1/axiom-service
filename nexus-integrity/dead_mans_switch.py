"""
dead_mans_switch.py — Cipher Amendment C1

External watchdog for nexus-integrity itself.
Monitors the monitor. Deployed as a SEPARATE LaunchAgent process.

Root cause this prevents: nexus-integrity crashes or hangs silently.
TRS score goes stale (last known value = 90 GREEN). OMNI sizes at 1.0x.
Pipeline is completely blind. Nobody knows.

Design:
- Separate process — NOT a thread inside nexus-integrity
- Calls GET /health on port 8011 every 60 seconds
- 2 consecutive failures → P0 alert directly to SOVEREIGN via bus
- Score staleness gate: if composite score age > 20 min → treat as UNKNOWN

This file is run standalone by ai.nexus.nexus-integrity-watchdog.plist.
It does NOT import any nexus-integrity modules.
All it needs: requests, the service URL, and the bus URL.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Config — all from env or hardcoded defaults (no config.py dependency)
# ---------------------------------------------------------------------------

INTEGRITY_URL       = os.environ.get("INTEGRITY_URL", "http://localhost:8011")
BUS_URL             = os.environ.get("BUS_URL", "http://192.168.1.141:9999")
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_AHMED_ID   = os.environ.get("TELEGRAM_AHMED_CHAT_ID", "8573754783")
TELEGRAM_GROUP_ID   = os.environ.get("TELEGRAM_HEALTH_GROUP_CHAT_ID", "-5241272802")

CHECK_INTERVAL_S    = 60       # Check every 60 seconds
CONSECUTIVE_FAIL_THRESHOLD = 2  # Alert after this many consecutive failures
SCORE_STALE_MINUTES = 20        # Composite score older than this → UNKNOWN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] dead-mans-switch — %(message)s",
)
logger = logging.getLogger("dead_mans_switch")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_consecutive_failures: int = 0
_alert_sent: bool = False


def _check_integrity_health() -> bool:
    """
    Check nexus-integrity /health endpoint.
    Returns True if healthy, False if unreachable or non-200.
    """
    try:
        resp = requests.get(f"{INTEGRITY_URL}/health", timeout=10.0)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def _send_bus_alert(message: str) -> None:
    """Post alert to SOVEREIGN via message bus."""
    try:
        requests.post(
            f"{BUS_URL}/send",
            json={"from": "nexus-integrity-watchdog", "to": "sovereign", "message": message},
            timeout=5.0,
        )
    except Exception as e:
        logger.error("Bus alert failed: %s", e)


def _send_telegram_alert(message: str) -> None:
    """Send P0 alert via Telegram to Ahmed + Health Group."""
    if not TELEGRAM_BOT_TOKEN:
        return
    for chat_id in [TELEGRAM_AHMED_ID, TELEGRAM_GROUP_ID]:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=8.0,
            )
        except Exception as e:
            logger.error("Telegram alert to %s failed: %s", chat_id, e)


def run() -> None:
    """
    Main watchdog loop. Runs indefinitely.
    LaunchAgent keeps it alive with KeepAlive=true.
    """
    global _consecutive_failures, _alert_sent

    logger.info(
        "nexus-integrity watchdog started — monitoring %s every %ds",
        INTEGRITY_URL, CHECK_INTERVAL_S,
    )

    while True:
        healthy = _check_integrity_health()

        if healthy:
            if _consecutive_failures > 0:
                logger.info(
                    "nexus-integrity recovered after %d failure(s)",
                    _consecutive_failures,
                )
                if _alert_sent:
                    _send_bus_alert(
                        "nexus-integrity RECOVERED — /health responding again. "
                        "Composite score now live."
                    )
                    _alert_sent = False
            _consecutive_failures = 0

        else:
            _consecutive_failures += 1
            logger.warning(
                "nexus-integrity /health FAILED (consecutive=%d, threshold=%d)",
                _consecutive_failures, CONSECUTIVE_FAIL_THRESHOLD,
            )

            if _consecutive_failures >= CONSECUTIVE_FAIL_THRESHOLD and not _alert_sent:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                alert_msg = (
                    f"🚨 P0 DEAD MAN'S SWITCH\n"
                    f"nexus-integrity is DOWN ({_consecutive_failures} consecutive "
                    f"/health failures).\n"
                    f"Composite TRS score is STALE — monitoring is BLIND.\n"
                    f"OMNI may be sizing with a stale score.\n"
                    f"Time: {ts}\n"
                    f"Action required: restart ai.nexus.nexus-integrity"
                )
                logger.critical(alert_msg)
                _send_bus_alert(alert_msg)
                _send_telegram_alert(alert_msg)
                _alert_sent = True

        time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    run()
