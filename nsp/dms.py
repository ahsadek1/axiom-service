"""
dms.py — Nexus Sentinel Prime Dead Man's Switch.

Standalone process — ZERO imports from the nsp package.
Cannot share NSP's fate. Runs independently under a separate launchd plist.

Behaviour:
  • Polls NSP /health every 30 seconds.
  • 2 consecutive failures → P0 alert to Ahmed (Telegram) + SOVEREIGN (message bus).
  • Writes own heartbeat to /tmp/nsp_dms_heartbeat after every successful poll.
  • Reads all credentials from environment variables — no hardcoded secrets.
"""

import os
import time
import logging

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] dms: %(message)s",
)
log = logging.getLogger("nsp.dms")

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
NSP_HEALTH_URL: str = os.environ.get("NSP_HEALTH_URL", "http://localhost:8010/health")
POLL_INTERVAL_S: int = int(os.environ.get("DMS_POLL_INTERVAL_S", "30"))
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
AHMED_CHAT_ID: str = os.environ.get("AHMED_CHAT_ID", "8573754783")
MESSAGE_BUS_URL: str = os.environ.get("MESSAGE_BUS_URL", "http://192.168.1.141:9999")
HEARTBEAT_PATH: str = "/tmp/nsp_dms_heartbeat"


def send_telegram(message: str) -> None:
    """Send a Telegram alert to Ahmed's chat."""
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set — skipping Telegram alert")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": AHMED_CHAT_ID, "text": message}, timeout=10)
    except Exception as exc:
        log.error("Telegram alert failed: %s", exc)


def send_bus(message: str) -> None:
    """Send a P0 alert to SOVEREIGN via the Nexus message bus."""
    try:
        requests.post(
            f"{MESSAGE_BUS_URL}/send",
            json={"from": "nsp-dms", "to": "sovereign", "message": message},
            timeout=5,
        )
    except Exception as exc:
        log.error("Message bus alert failed: %s", exc)


def write_heartbeat() -> None:
    """Write own heartbeat timestamp so external monitors can watch DMS too."""
    try:
        with open(HEARTBEAT_PATH, "w") as fh:
            fh.write(str(time.time()))
    except Exception as exc:
        log.warning("Heartbeat write failed: %s", exc)


def check_nsp_health() -> bool:
    """Return True if NSP /health responds with HTTP 200, False otherwise."""
    try:
        resp = requests.get(NSP_HEALTH_URL, timeout=10)
        return resp.status_code == 200
    except Exception as exc:
        log.warning("NSP health check failed: %s", exc)
        return False


def run() -> None:
    """Main DMS loop — runs forever."""
    log.info("Dead Man's Switch started — watching %s every %ds", NSP_HEALTH_URL, POLL_INTERVAL_S)
    consecutive_failures: int = 0

    while True:
        healthy = check_nsp_health()

        if healthy:
            consecutive_failures = 0
            write_heartbeat()
            log.debug("NSP healthy")
        else:
            consecutive_failures += 1
            log.warning("NSP health check FAILED (%d consecutive)", consecutive_failures)

            if consecutive_failures >= 2:
                alert = (
                    f"🚨 P0 — NSP DEAD\n"
                    f"Nexus Sentinel Prime /health has failed {consecutive_failures} consecutive checks.\n"
                    f"Immediate intervention required."
                )
                log.error("P0 ALERT: %s", alert)
                send_telegram(alert)
                send_bus(f"P0: NSP /health failed {consecutive_failures} consecutive checks — GENESIS intervention required")

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    run()
