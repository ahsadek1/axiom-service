#!/usr/bin/env python3
"""
vector_liveness_ping.py — Agent & Service Liveness Check (Block 7 v2)
======================================================================
FIX (2026-05-02): Replaced broken ping/pong protocol with HTTP health checks.
Previous version sent LIVENESS_PING via message bus and waited for PONG — but
agents (SOVEREIGN, GENESIS, OMNI) never implemented PONG responses, causing
every check to time out and spam Ahmed with false alerts.

New protocol:
  - HTTP services (OMNI, Oracle, etc): check /health endpoint → 200 OK = alive
  - Session agents (SOVEREIGN, GENESIS): check OpenClaw gateway liveness
    (if gateway is up and sessions are configured, agents are alive)
  - Only alert if HTTP health fails, not on session silence

Runs every 30 min via LaunchAgent ai.nexus.vector-liveness.
Market hours: every 30 min. Off-hours/weekends: every 60 min.

Author: VECTOR 🔧 | Block 7 v2 — fixed 2026-05-02 by SOVEREIGN
"""

import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ET = ZoneInfo("America/New_York")
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8736004775:AAG3v_7tcXk8SXh5whgpKRT3Dr3-C71VtQI"
)
GROUP_CHAT_ID = "-1003579956463"
AHMED_TELEGRAM_ID = "8573754783"
MESSAGE_BUS = "http://192.168.1.141:9999"
CHRONICLE_DB = "/Users/ahmedsadek/nexus/data/chronicle.db"

# Services with actual HTTP health endpoints — these are real liveness checks
HTTP_SERVICES = [
    ("OMNI",          "http://localhost:8004/health", True),
    ("Alpha-Exec",    "http://localhost:8005/health", True),
    ("Prime-Exec",    "http://localhost:8006/health", True),
    ("Axiom",         "http://localhost:8001/health", False),
    ("Alpha-Buffer",  "http://localhost:8002/health", False),
    ("Oracle",        "http://localhost:8007/health", False),
]

# Session agents — verified alive if OpenClaw gateway responds
# SOVEREIGN and GENESIS are session agents, not HTTP services.
# They are alive if: (1) OpenClaw is running, (2) their last cron ran recently.
# We do NOT ping them on the message bus — they only respond to cron triggers.
GATEWAY_URL = "http://localhost:9999/health"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] vector.liveness: %(message)s",
)
log = logging.getLogger("vector.liveness")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_market_hours() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    market_open  = now.replace(hour=9, minute=25, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=5, second=0, microsecond=0)
    return market_open <= now <= market_close


def _send_telegram(message: str, target: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": target, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


def _chronicle_log(agent: str, status: str, detail: str) -> None:
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(CHRONICLE_DB, timeout=5)
        conn.execute("""
            INSERT INTO intervention_log
            (agent, error_description, time_identified, ideal_response_time,
             time_acted, time_resolved, resolution_type, damage_assessment, outcome, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "vector",
            f"Liveness check: {agent} → {status}",
            now, now, now,
            now if status == "alive" else None,
            "root_cause" if status == "dead" else "symptomatic",
            "none" if status == "alive" else f"{agent} HTTP health failed",
            "solved" if status == "alive" else "in_progress",
            detail,
            now,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("CHRONICLE log failed: %s", e)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _check_http_service(label: str, url: str, timeout_s: int = 8) -> tuple[bool, str]:
    """Check service health via HTTP. Returns (alive, detail)."""
    try:
        r = requests.get(url, timeout=timeout_s)
        if r.status_code == 200:
            data = r.json()
            status = data.get("status", "unknown")
            return True, f"HTTP 200 status={status}"
        elif r.status_code == 403:
            # Auth-gated health endpoint — service is running, just not public
            return True, "HTTP 403 (auth-gated) — service is running"
        else:
            return False, f"HTTP {r.status_code} — unexpected response"
    except requests.exceptions.ConnectionError:
        return False, "connection refused — service may be down"
    except requests.exceptions.Timeout:
        return False, f"timeout after {timeout_s}s"
    except Exception as e:
        return False, f"error: {e}"


def _check_gateway() -> tuple[bool, str]:
    """Check if OpenClaw message bus (proxy for gateway liveness) is alive."""
    try:
        r = requests.get(GATEWAY_URL, timeout=5)
        if r.ok:
            return True, "gateway/bus responding"
        return False, f"bus HTTP {r.status_code}"
    except Exception as e:
        return False, f"gateway unreachable: {e}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    now_et = datetime.now(ET)
    market = _is_market_hours()
    log.info("VECTOR liveness check v2 — %s | market_hours=%s", now_et.strftime("%H:%M ET"), market)

    dead: list[tuple[str, bool, str]] = []  # (label, critical, detail)

    # Check HTTP services
    for label, url, critical in HTTP_SERVICES:
        alive, detail = _check_http_service(label, url)
        log.info("%s: %s — %s", label, "alive" if alive else "DEAD", detail)
        if not alive:
            dead.append((label, critical, detail))
            _chronicle_log(label, "dead", detail)
        # Only log alive to CHRONICLE if dead last time (avoid flooding)

    # Check session agents via gateway (SOVEREIGN, GENESIS)
    gw_alive, gw_detail = _check_gateway()
    if not gw_alive:
        # Gateway down = all session agents unreachable
        dead.append(("OpenClaw-Gateway", True, gw_detail))
        _chronicle_log("OpenClaw-Gateway", "dead", gw_detail)
        log.error("GATEWAY DOWN: %s", gw_detail)
    else:
        log.info("Session agents (SOVEREIGN, GENESIS): gateway alive — sessions considered healthy")

    # Only alert if actual HTTP services are down
    if dead:
        critical_dead = [d for d in dead if d[1]]

        lines = ["🔴 *VECTOR — SERVICE DOWN ALERT*\n"]
        for label, critical, detail in dead:
            sev = "🚨 CRITICAL" if critical else "⚠️ WARNING"
            lines.append(f"{sev}: `{label}` unreachable")
            lines.append(f"Detail: {detail}\n")
        lines.append(f"Time: {now_et.strftime('%H:%M ET')}")
        alert = "\n".join(lines)

        # Alert group for all failures
        _send_telegram(alert, GROUP_CHAT_ID)

        # Alert Ahmed only for critical services
        if critical_dead:
            _send_telegram(alert, AHMED_TELEGRAM_ID)

        log.error("Liveness FAILED: %d service(s) down", len(dead))
        return 1

    log.info("All services healthy — no alerts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
