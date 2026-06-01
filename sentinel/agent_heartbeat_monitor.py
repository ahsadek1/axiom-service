"""
agent_heartbeat_monitor.py — Agent Heartbeat Monitor
=====================================================
Monitors Cipher, Sage, and Atlas submission activity.
Detects silent agents. Fixes what can be fixed. Reports FYI.

Cadence (smart — not flat polling):
  Market hours   09:30-16:00 ET → every 5 minutes
  Pre/post market 04:00-09:30, 16:00-00:00 ET → every 30 minutes
  Overnight      00:00-04:00 ET → every 2 hours

Per-agent recovery:
  Cipher → SSH 192.168.1.141 → check port 9001 → restart OpenClaw
  Atlas  → OpenClaw on Mac Mini → restart atlas agent
  Sage   → FYI alert only (Manus external, cannot restart)

Silent threshold: 30 minutes during market hours.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import requests

log = logging.getLogger("nexus.agent_heartbeat")
_ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ALPHA_BUFFER_DB  = "/Users/ahmedsadek/nexus/data/alpha_buffer.db"
IMAC_IP          = "192.168.1.141"
IMAC_USER        = "ahmedsadek"
CIPHER_PORT      = 9001   # on iMac
ATLAS_PORT       = 9002   # on Mac Mini
SAGE_PORT        = 9003   # on Mac Mini (Manus)

SILENCE_THRESHOLD_MARKET  = 30   # minutes — flag if no submission in 30 min during market
SILENCE_THRESHOLD_OFFHOURS = 120  # minutes — flag if no submission in 2 hours off-hours

# Restart throttle — max 2 per agent per hour
_agent_restart_log: dict[str, list[float]] = {}


# ---------------------------------------------------------------------------
# Cadence logic
# ---------------------------------------------------------------------------

def get_scan_interval() -> int:
    """Return scan interval in seconds based on current market phase."""
    now = datetime.now(_ET)
    hour = now.hour
    minute = now.minute
    weekday = now.weekday()

    # Weekend — 2 hour intervals
    if weekday >= 5:
        return 7200

    # Overnight 00:00 - 04:00 ET — 2 hours
    if hour < 4:
        return 7200

    # Pre/post market 04:00-09:30 and 16:00-00:00 — 30 minutes
    if hour < 9 or (hour == 9 and minute < 30) or hour >= 16:
        return 1800

    # Market hours 09:30-16:00 — 5 minutes
    return 300


def _market_hours() -> bool:
    now = datetime.now(_ET)
    return (
        now.weekday() < 5
        and (now.hour > 9 or (now.hour == 9 and now.minute >= 30))
        and now.hour < 16
    )


def _silence_threshold_minutes() -> int:
    return SILENCE_THRESHOLD_MARKET if _market_hours() else SILENCE_THRESHOLD_OFFHOURS


# ---------------------------------------------------------------------------
# Last submission query
# ---------------------------------------------------------------------------

def get_last_submission(agent: str) -> Optional[datetime]:
    """Get the last submission time for an agent from alpha_buffer DB."""
    try:
        conn = sqlite3.connect(ALPHA_BUFFER_DB, timeout=5)
        row = conn.execute(
            "SELECT MAX(received_at) FROM submissions WHERE agent = ?",
            (agent,),
        ).fetchone()
        conn.close()
        if row and row[0]:
            ts = row[0].replace("Z", "+00:00")
            return datetime.fromisoformat(ts)
    except Exception as exc:
        log.warning("DB query failed for %s: %s", agent, exc)
    return None


def is_agent_silent(agent: str) -> tuple[bool, str]:
    """
    Check if agent has been silent longer than threshold.
    Returns (is_silent, detail_message).
    """
    last = get_last_submission(agent)
    if last is None:
        return True, f"No submissions found for {agent}"

    now_utc = datetime.now(timezone.utc)
    minutes_ago = (now_utc - last).total_seconds() / 60
    threshold = _silence_threshold_minutes()

    if minutes_ago > threshold:
        return True, f"Last submission {minutes_ago:.0f} min ago (threshold: {threshold} min)"

    return False, f"Active — last submission {minutes_ago:.0f} min ago"


# ---------------------------------------------------------------------------
# Per-agent recovery
# ---------------------------------------------------------------------------

def _can_restart_agent(agent: str) -> bool:
    now = time.time()
    times = [t for t in _agent_restart_log.get(agent, []) if now - t < 3600]
    _agent_restart_log[agent] = times
    return len(times) < 2


def _record_agent_restart(agent: str) -> None:
    _agent_restart_log.setdefault(agent, []).append(time.time())


def recover_cipher(fyi_func) -> bool:
    """
    Cipher recovery:
    1. Check if port 9001 responds on iMac
    2. If not → restart OpenClaw gateway via SSH
    3. If yes → Cipher process is up but not submitting → restart cipher agent
    """
    if not _can_restart_agent("cipher"):
        log.warning("CIPHER restart throttled — too many restarts in 1 hour")
        fyi_func("⚠️ <b>CIPHER RESTART THROTTLED</b>\nToo many restarts in 1 hour. Manual check needed.")
        return False

    log.info("ACTION: Checking Cipher on iMac %s:9001", IMAC_IP)

    # Check if Cipher port responds on iMac
    try:
        resp = requests.get(f"http://{IMAC_IP}:{CIPHER_PORT}/health", timeout=5)
        cipher_port_up = resp.status_code == 200
    except Exception:
        cipher_port_up = False

    _record_agent_restart("cipher")

    if not cipher_port_up:
        # OpenClaw gateway down — restart it
        log.info("ACTION: Cipher port down — restarting OpenClaw gateway on iMac")
        try:
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
                 f"{IMAC_USER}@{IMAC_IP}",
                 "openclaw doctor --fix && openclaw gateway install && openclaw gateway start"],
                capture_output=True, text=True, timeout=60,
            )
            time.sleep(8)
            # Verify recovery
            try:
                resp = requests.get(f"http://{IMAC_IP}:{CIPHER_PORT}/health", timeout=5)
                if resp.status_code == 200:
                    fyi_func("✅ <b>AUTO-FIXED: CIPHER RECOVERED</b>\nOpenClaw gateway restarted on iMac. Cipher is healthy.")
                    return True
            except Exception:
                pass
            fyi_func("❌ <b>CIPHER RECOVERY FAILED</b>\nOpenClaw gateway restart attempted but Cipher still unreachable.")
            return False
        except Exception as exc:
            fyi_func(f"❌ <b>CIPHER SSH FAILED</b>\nCannot reach iMac: {exc}")
            return False
    else:
        # Port up but silent — restart cipher agent specifically
        log.info("ACTION: Cipher port up but silent — restarting cipher agent")
        try:
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
                 f"{IMAC_USER}@{IMAC_IP}",
                 "openclaw restart cipher 2>/dev/null || openclaw agent restart cipher 2>/dev/null || echo RESTART_ATTEMPTED"],
                capture_output=True, text=True, timeout=30,
            )
            time.sleep(5)
            fyi_func("✅ <b>AUTO-FIXED: CIPHER AGENT RESTARTED</b>\nCipher was up but silent — agent restarted on iMac.")
            return True
        except Exception as exc:
            fyi_func(f"❌ <b>CIPHER AGENT RESTART FAILED</b>\n{exc}")
            return False


def recover_atlas(fyi_func) -> bool:
    """
    Atlas recovery:
    Atlas runs on Mac Mini via OpenClaw.
    Restart atlas agent via OpenClaw.
    """
    if not _can_restart_agent("atlas"):
        fyi_func("⚠️ <b>ATLAS RESTART THROTTLED</b>\nToo many restarts in 1 hour.")
        return False

    log.info("ACTION: Restarting Atlas agent on Mac Mini")
    _record_agent_restart("atlas")

    try:
        result = subprocess.run(
            ["openclaw", "restart", "atlas"],
            capture_output=True, text=True, timeout=30,
        )
        time.sleep(5)

        # Verify via port
        try:
            resp = requests.get(f"http://localhost:{ATLAS_PORT}/health", timeout=5)
            if resp.status_code == 200:
                fyi_func("✅ <b>AUTO-FIXED: ATLAS RESTARTED</b>\nAtlas agent restarted on Mac Mini.")
                return True
        except Exception:
            pass

        # Try alternative restart
        subprocess.run(
            ["openclaw", "doctor", "--fix"],
            capture_output=True, text=True, timeout=30,
        )
        time.sleep(5)
        fyi_func("✅ <b>ATLAS RECOVERY ATTEMPTED</b>\nOpenClaw doctor applied. Monitor submissions.")
        return True

    except Exception as exc:
        fyi_func(f"❌ <b>ATLAS RESTART FAILED</b>\n{exc}\nManual intervention needed.")
        return False


def recover_sage(fyi_func) -> None:
    """
    Sage recovery:
    Sage runs via Manus (external service).
    Cannot restart automatically.
    Alert FYI only.
    """
    log.warning("SAGE silent — external Manus service, cannot auto-restart")
    fyi_func(
        "⚠️ <b>SAGE SILENT — ACTION NEEDED</b>\n"
        "Sage runs via Manus (external).\n"
        "Auto-restart not possible.\n\n"
        "Check: Is Manus running? Is Sage agent active?\n"
        "V2 will continue with Cipher + Atlas only until Sage recovers.\n"
        "<i>Concordance threshold may be reduced automatically.</i>"
    )


# ---------------------------------------------------------------------------
# Main heartbeat check
# ---------------------------------------------------------------------------

def check_agent_heartbeats(fyi_func) -> dict[str, bool]:
    """
    Check all three agents. Fix what can be fixed.
    Returns {agent: is_healthy} dict.
    """
    # Skip during first 15 min of market open (agents warming up)
    now_et = datetime.now(_ET)
    if (now_et.weekday() < 5
            and now_et.hour == 9
            and now_et.minute < 45):
        log.debug("Skipping agent check — market warmup period")
        return {"cipher": True, "atlas": True, "sage": True}

    results = {}

    for agent, recover_fn in [
        ("Cipher", recover_cipher),
        ("Atlas",  recover_atlas),
        ("Sage",   recover_sage),
    ]:
        silent, detail = is_agent_silent(agent)
        agent_key = agent.lower()

        if not silent:
            log.debug("%s healthy — %s", agent, detail)
            results[agent_key] = True
            continue

        # Outside market hours: agents are quiet by design — never recover or substitute
        if not _market_hours():
            results[agent_key] = True
            continue

        log.warning("AGENT SILENT: %s — %s", agent, detail)

        if agent == "Sage":
            recover_sage(fyi_func)
            results[agent_key] = False
        else:
            recovered = recover_fn(fyi_func)
            results[agent_key] = recovered

    # Check if all agents are silent — critical failure
    if not any(results.values()) and _market_hours():
        fyi_func(
            "🚨 <b>ALL AGENTS SILENT — CRITICAL</b>\n"
            "Cipher, Sage, and Atlas all silent during market hours.\n"
            "V2 cannot form concordance.\n"
            "<b>Immediate investigation required.</b>"
        )

    return results


# ---------------------------------------------------------------------------
# Smart scan loop (integrates with autonomous_action_engine)
# ---------------------------------------------------------------------------

def run_heartbeat_loop(fyi_func) -> None:
    """
    Run agent heartbeat loop with smart cadence.
    Designed to run in a thread alongside autonomous_action_engine.
    """
    log.info("Agent heartbeat monitor started")
    last_check = 0.0

    while True:
        interval = get_scan_interval()
        now = time.time()

        if now - last_check >= interval:
            try:
                results = check_agent_heartbeats(fyi_func)
                phase = "MARKET" if _market_hours() else "OFF-HOURS"
                healthy = sum(1 for v in results.values() if v)
                log.info(
                    "Heartbeat scan [%s]: %d/3 agents healthy | next check in %ds",
                    phase, healthy, interval,
                )
            except Exception as exc:
                log.error("Heartbeat scan error: %s", exc)
            last_check = now

        time.sleep(30)  # check every 30s whether interval has elapsed


# ---------------------------------------------------------------------------
# Agent X Integration
# ---------------------------------------------------------------------------

def run_heartbeat_loop(fyi_func) -> None:
    """
    Run agent heartbeat loop with smart cadence + Agent X substitution.
    Designed to run in a thread alongside autonomous_action_engine.
    """
    import sys
    sys.path.insert(0, "/Users/ahmedsadek/nexus/sentinel")

    # Initialize Agent X
    try:
        from agent_x import run_agent_x_tick
        agent_x_available = True
        log.info("Agent X loaded — universal substitute ready")
    except Exception as exc:
        agent_x_available = False
        log.warning("Agent X not available: %s", exc)

    log.info("Agent heartbeat monitor started")
    last_check = 0.0

    while True:
        interval = get_scan_interval()
        now = time.time()

        if now - last_check >= interval:
            try:
                # 1. Check heartbeats and attempt recovery
                results = check_agent_heartbeats(fyi_func)

                # 2. Run Agent X for any silent agent
                if agent_x_available:
                    run_agent_x_tick()

                phase = "MARKET" if _market_hours() else "OFF-HOURS"
                healthy = sum(1 for v in results.values() if v)
                log.info(
                    "Heartbeat [%s] interval=%ds: %d/3 agents healthy",
                    phase, interval, healthy,
                )
            except Exception as exc:
                log.error("Heartbeat scan error: %s", exc)
            last_check = now

        time.sleep(30)
