#!/usr/bin/env python3
"""
Telegram Account Watchdog
Monitors the OpenClaw gateway log for stuck `channels.telegram.start-account` phases.
If any account is stuck >30 minutes, triggers a gateway restart.

Runs every 5 minutes via cron or launchd.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
GATEWAY_LOG_PATH = "/tmp/openclaw/openclaw-2026-05-07.log"  # updated daily by gateway
STUCK_THRESHOLD_MS = 30 * 60 * 1000   # 30 minutes
RESTART_COOLDOWN_S = 300               # don't restart more than once per 5 min
STATE_FILE = "/Users/ahmedsadek/nexus/data/telegram_watchdog_state.json"
LOG_FILE = "/Users/ahmedsadek/nexus/logs/telegram-watchdog.log"
OPENCLAW_BIN = "/opt/homebrew/bin/openclaw"
BUS_URL = "http://192.168.1.141:9999/send"

# ── Helpers ───────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"last_restart_ts": 0, "restart_count": 0}


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def notify_bus(message: str):
    """Non-fatal — if bus is down we still restart."""
    try:
        import urllib.request
        payload = json.dumps({"from": "cipher", "to": "sovereign", "message": message}).encode()
        req = urllib.request.Request(BUS_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log(f"Bus notify failed (non-fatal): {e}")


def get_today_log_path() -> str:
    """Gateway log rolls daily — always use today's date."""
    today = datetime.now().strftime("%Y-%m-%d")
    return f"/tmp/openclaw/openclaw-{today}.log"


def scan_log_for_stuck_accounts(log_path: str) -> int:
    """
    Reads the last 500 lines of the gateway log.
    Returns the maximum stuck duration in ms found, 0 if none.
    """
    if not os.path.exists(log_path):
        log(f"Log file not found: {log_path}")
        return 0

    # Read last 500 lines efficiently
    try:
        result = subprocess.run(
            ["tail", "-500", log_path],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.splitlines()
    except Exception as e:
        log(f"Failed to read log: {e}")
        return 0

    max_stuck_ms = 0

    for line in reversed(lines):  # most recent first
        try:
            d = json.loads(line)
            msg = d.get("1", "") or d.get("message", "") or ""
            if not isinstance(msg, str):
                continue

            # Look for recentPhases containing start-account duration
            m = re.search(r"recentPhases=([^\s]+)", msg)
            if not m:
                continue

            phases_str = m.group(1)
            matches = re.findall(r"channels\.telegram\.start-account:(\d+)ms", phases_str)
            if not matches:
                continue

            for ms_str in matches:
                ms = int(ms_str)
                if ms > max_stuck_ms:
                    max_stuck_ms = ms

            # We found the most recent diagnostic entry — stop here
            break

        except (json.JSONDecodeError, KeyError, ValueError):
            continue

    return max_stuck_ms


def restart_gateway() -> bool:
    """Trigger `openclaw gateway restart` and return success."""
    try:
        log("Triggering: openclaw gateway restart")
        result = subprocess.run(
            [OPENCLAW_BIN, "gateway", "restart"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            log("Gateway restart command succeeded")
            return True
        else:
            log(f"Gateway restart failed (rc={result.returncode}): {result.stderr[:200]}")
            return False
    except Exception as e:
        log(f"Gateway restart exception: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log_path = get_today_log_path()
    log(f"Scanning: {log_path}")

    stuck_ms = scan_log_for_stuck_accounts(log_path)

    if stuck_ms == 0:
        log("OK — no stuck Telegram start-account phase detected")
        return

    stuck_min = stuck_ms / 60000
    log(f"STUCK DETECTED: channels.telegram.start-account has been stuck for {stuck_min:.1f} min ({stuck_ms:,}ms)")

    if stuck_ms < STUCK_THRESHOLD_MS:
        log(f"Below 30-min threshold ({stuck_min:.1f} min) — monitoring only")
        return

    # Check cooldown
    state = load_state()
    now = time.time()
    elapsed_since_last = now - state.get("last_restart_ts", 0)

    if elapsed_since_last < RESTART_COOLDOWN_S:
        log(f"Cooldown active — last restart was {elapsed_since_last:.0f}s ago (cooldown={RESTART_COOLDOWN_S}s), skipping")
        return

    # Notify Sovereign via bus
    msg = (
        f"⚠️ TELEGRAM WATCHDOG: channels.telegram.start-account stuck for "
        f"{stuck_min:.0f} min. Triggering gateway restart."
    )
    notify_bus(msg)
    log(msg)

    # Restart
    success = restart_gateway()

    # Update state
    state["last_restart_ts"] = now
    state["restart_count"] = state.get("restart_count", 0) + 1
    state["last_stuck_ms"] = stuck_ms
    state["last_restart_success"] = success
    state["last_restart_time"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    if success:
        log(f"Gateway restarted successfully. Total auto-restarts: {state['restart_count']}")
    else:
        log("Gateway restart FAILED — manual intervention may be required")
        notify_bus("🚨 TELEGRAM WATCHDOG: Gateway restart FAILED — manual intervention needed.")


if __name__ == "__main__":
    main()
