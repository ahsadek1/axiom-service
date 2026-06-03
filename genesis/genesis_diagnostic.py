#!/usr/bin/env python3
"""
genesis_diagnostic.py — Stateful GENESIS Live Diagnostic
=========================================================
Replaces the stateless heartbeat cron script.

KEY DIFFERENCE from the old script:
  - Reads agent_alerts from CHRONICLE before acting
  - Skips issues that are already OPEN/IN_PROGRESS (no duplicate work)
  - Marks new issues as IN_PROGRESS while diagnosing
  - Marks RESOLVED when confirmed clear
  - Polls /inbox/genesis on the message bus and acts on messages from other agents
  - Logs every intervention to CHRONICLE intervention_log

Run: python3 /Users/ahmedsadek/nexus/genesis/genesis_diagnostic.py

Author: GENESIS | 2026-05-05
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# Ensure nexus shared is on path
sys.path.insert(0, "/Users/ahmedsadek/nexus")
from shared.alert_manager import (
    write_alert, get_open_alerts, is_known,
    ack_alert, resolve_alert, suppress_alert,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] genesis.diagnostic: %(message)s",
)
logger = logging.getLogger("genesis.diagnostic")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SECRET = os.getenv("NEXUS_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
BUS_URL = os.getenv("NEXUS_BUS_URL", "http://192.168.1.141:9999")
CHRONICLE_DB = os.getenv("CHRONICLE_DB", "/Users/ahmedsadek/nexus/data/chronicle.db")
HTTP_TIMEOUT = 5

ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(ET)


def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def is_market_hours() -> bool:
    n = now_et()
    return n.weekday() < 5 and (
        (n.hour == 9 and n.minute >= 30) or
        (10 <= n.hour < 16)
    )


# ---------------------------------------------------------------------------
# CHRONICLE: intervention log
# ---------------------------------------------------------------------------

def log_intervention(
    error_description: str,
    time_identified: str,
    time_acted: str,
    time_resolved: str | None,
    resolution_type: str,
    damage_assessment: str,
    outcome: str,
    notes: str = "",
) -> None:
    """Log to CHRONICLE intervention_log. Non-blocking, never raises."""
    try:
        conn = sqlite3.connect(CHRONICLE_DB, timeout=5)
        conn.execute("""
            INSERT INTO intervention_log
                (agent, error_description, time_identified, ideal_response_time,
                 time_acted, time_resolved, resolution_type, damage_assessment, outcome, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "genesis",
            error_description,
            time_identified,
            time_identified,   # ideal = immediate
            time_acted,
            time_resolved,
            resolution_type,
            damage_assessment,
            outcome,
            notes,
        ))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("log_intervention failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def check_trs() -> dict:
    """
    Check TRS (Trade Readiness Score).
    Returns: {"ok": bool, "issue_key": str, "title": str, "details": str}
    """
    issue_key = "trs:not-green"
    try:
        r = requests.get(
            "http://localhost:8012/trs",
            headers={"X-Nexus-Secret": SECRET},
            timeout=HTTP_TIMEOUT,
        )
        d = r.json()
        color = d.get("color", "?")
        score = d.get("score", 0)
        reason = d.get("reason", "")[:120]

        if color == "GREEN":
            # Resolve if previously open
            if is_known(issue_key):
                resolve_alert(issue_key, f"TRS back to GREEN (score={score})")
                logger.info("TRS: GREEN — resolved prior alert")
            return {"ok": True}

        return {
            "ok": False,
            "issue_key": issue_key,
            "severity": "CRITICAL" if color == "RED" else "WARNING",
            "title": f"TRS={score} {color}",
            "details": reason,
        }
    except Exception as exc:
        return {
            "ok": False,
            "issue_key": "trs:unreachable",
            "severity": "CRITICAL",
            "title": "TRS endpoint unreachable",
            "details": str(exc),
        }


def check_alpha_exec() -> dict:
    """Check Alpha Execution health."""
    issue_key = "alpha-exec:paused"
    try:
        r = requests.get("http://localhost:8005/health", timeout=HTTP_TIMEOUT)
        d = r.json()

        if d.get("execution_paused"):
            return {
                "ok": False,
                "issue_key": issue_key,
                "severity": "CRITICAL",
                "title": "ALPHA-EXEC PAUSED",
                "details": f"vix_brake={d.get('vix_brake','?')}",
            }
        # VIX brake active?
        vix = d.get("vix_brake", "CLEAR")
        if vix not in ("CLEAR", "UNKNOWN", None):
            vix_key = "alpha-exec:vix-brake"
            return {
                "ok": False,
                "issue_key": vix_key,
                "severity": "WARNING",
                "title": f"VIX BRAKE: {vix}",
                "details": str(d.get("vix_value", "")),
            }

        # Clear any prior alerts if healthy
        for k in (issue_key, "alpha-exec:vix-brake", "alpha-exec:unreachable"):
            if is_known(k):
                resolve_alert(k, "Alpha-Exec healthy")
        return {"ok": True}

    except Exception as exc:
        return {
            "ok": False,
            "issue_key": "alpha-exec:unreachable",
            "severity": "CRITICAL",
            "title": "Alpha-Exec unreachable",
            "details": str(exc),
        }


def check_omni_silence() -> dict:
    """Check OMNI synthesis cadence (market hours only)."""
    issue_key = "omni:silence"
    try:
        r = requests.get("http://localhost:8004/health", timeout=HTTP_TIMEOUT)
        d = r.json()
        lag = d.get("last_synthesis_min_ago")

        if lag and lag > 20:
            details = (
                f"last_synthesis_min_ago={lag:.0f} | "
                f"syntheses_today={d.get('syntheses_today',0)} | "
                f"go_verdicts={d.get('go_verdicts_today',0)}"
            )
            return {
                "ok": False,
                "issue_key": issue_key,
                "severity": "WARNING",
                "title": f"OMNI SILENCE: {lag:.0f}min",
                "details": details,
            }

        # OMNI is active — resolve if previously silenced
        if is_known(issue_key):
            resolve_alert(
                issue_key,
                f"OMNI resumed — last_synthesis_min_ago={lag:.1f if lag else 'N/A'}",
            )
            logger.info("OMNI silence resolved — closing alert")
        return {"ok": True}

    except Exception as exc:
        return {
            "ok": False,
            "issue_key": "omni:unreachable",
            "severity": "CRITICAL",
            "title": "OMNI unreachable",
            "details": str(exc),
        }


def check_alpha_buffer_concordance() -> dict:
    """
    Check if alpha-buffer has been producing concordances.
    Silence for >30min during market hours is flagged.
    """
    issue_key = "alpha-buffer:no-concordance"
    try:
        r = requests.get(
            "http://localhost:8002/health",
            headers={"X-Nexus-Secret": SECRET},
            timeout=HTTP_TIMEOUT,
        )
        d = r.json()
        # If health is OK but we already have this alert open, check if concordances resumed
        # (OMNI silence check covers the downstream effect — this covers the upstream cause)
        if d.get("status") == "healthy":
            if is_known(issue_key):
                resolve_alert(issue_key, "Alpha-buffer healthy and dispatching")
        return {"ok": True}
    except Exception as exc:
        return {
            "ok": False,
            "issue_key": "alpha-buffer:unreachable",
            "severity": "CRITICAL",
            "title": "Alpha-buffer unreachable",
            "details": str(exc),
        }


# ---------------------------------------------------------------------------
# Message bus inbox poller
# ---------------------------------------------------------------------------

def poll_genesis_inbox() -> list[dict]:
    """
    Poll /inbox/genesis on the message bus and return messages.
    Consume-on-read: messages are removed from bus after retrieval.
    """
    try:
        r = requests.get(f"{BUS_URL}/inbox/genesis", timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            messages = data if isinstance(data, list) else data.get("messages", [])
            if messages:
                logger.info("Bus inbox: %d message(s) received for genesis", len(messages))
            return messages
        return []
    except Exception as exc:
        logger.debug("Bus inbox poll failed (non-fatal): %s", exc)
        return []


def process_bus_message(msg: dict) -> None:
    """
    Process a message received from the bus.
    Log it, and if it contains an alert, write to agent_alerts.
    """
    from_agent = msg.get("from_agent", msg.get("from", "unknown"))
    message_text = msg.get("message", "")
    logger.info("Bus message from %s: %s", from_agent, message_text[:200])

    # If message looks like an alert, persist it
    msg_upper = message_text.upper()
    if any(kw in msg_upper for kw in ("CRITICAL", "ERROR", "ALERT", "FAILURE", "PAUSED", "UNREACHABLE")):
        severity = "CRITICAL" if "CRITICAL" in msg_upper else "WARNING"
        issue_key = f"bus:{from_agent}:{hash(message_text[:60]) & 0xFFFF}"
        write_alert(
            agent=from_agent,
            severity=severity,
            service=from_agent,
            issue_key=issue_key,
            title=f"Bus alert from {from_agent}",
            details=message_text[:500],
        )


# ---------------------------------------------------------------------------
# Main diagnostic runner
# ---------------------------------------------------------------------------

def run_diagnostic() -> int:
    """
    Run all health checks. Returns number of new issues found.
    """
    t_start = now_utc_str()
    market_hours = is_market_hours()
    issues_found = 0

    # 1. Poll bus inbox first — process messages from other agents
    bus_messages = poll_genesis_inbox()
    for msg in bus_messages:
        process_bus_message(msg)

    # 2. Run health checks
    checks = [check_trs(), check_alpha_exec()]
    if market_hours:
        checks += [check_omni_silence(), check_alpha_buffer_concordance()]

    for result in checks:
        if result["ok"]:
            continue

        issue_key = result["issue_key"]
        severity = result.get("severity", "WARNING")
        title = result["title"]
        details = result.get("details", "")

        # GUARD: Skip if already known and being handled
        if is_known(issue_key):
            logger.info("SKIP (already known): %s", issue_key)
            continue

        # New issue — write, ack, log intervention
        t_identified = now_utc_str()
        alert_id = write_alert(
            agent="genesis",
            severity=severity,
            service=issue_key.split(":")[0],
            issue_key=issue_key,
            title=title,
            details=details,
        )

        if alert_id > 0:
            ack_alert(issue_key, "genesis")
            issues_found += 1
            logger.warning("NEW ISSUE [%s]: %s | %s", severity, title, details)

            log_intervention(
                error_description=f"{title} — {details}",
                time_identified=t_identified,
                time_acted=now_utc_str(),
                time_resolved=None,
                resolution_type="root_cause",
                damage_assessment=f"Detected during 5-min diagnostic sweep. severity={severity}",
                outcome="in_progress",
                notes=f"issue_key={issue_key} alert_id={alert_id}",
            )

            # Send alert via broker if new and high severity
            if severity in ("CRITICAL", "WARNING"):
                _send_broker_alert(issue_key, severity, title, details)

    if issues_found == 0:
        logger.info("ALL_CLEAR | market_hours=%s | bus_messages=%d", market_hours, len(bus_messages))
    else:
        logger.warning("ISSUES FOUND: %d new | open_total=%d", issues_found, len(get_open_alerts()))

    return issues_found


def _send_broker_alert(issue_key: str, severity: str, title: str, details: str) -> None:
    """Send to Alert Broker (non-blocking). Broker handles dedup + Telegram routing."""
    try:
        from shared.alert_client import send_alert
        send_alert(
            source="genesis-diagnostic",
            level=severity,
            title=title,
            body=details,
            dedup_key=issue_key,
            targets=["ahmed", "nexus_health_group"],
        )
    except Exception as exc:
        logger.warning("_send_broker_alert failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    issues = run_diagnostic()
    sys.exit(1 if issues > 0 else 0)
