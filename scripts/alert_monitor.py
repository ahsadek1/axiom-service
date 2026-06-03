#!/usr/bin/env python3
"""
alert_monitor.py — Unified Nexus Alert Monitor v2
===================================================
Polls the unified alert queue every cycle.

ROUTING:
  HIGH    → Primus (SQS) or Cipher (Nexus) via bus
  CRITICAL → Primus/Cipher + Vector (root cause investigation) via bus

FLOOD PREVENTION:
  Per-service+code cooldown — same F-code doesn't re-fire to Primus within 30 min.
  After 30 min with no resolution → re-dispatch once more (persistent issue).
  Vector escalation: only on CRITICAL, only after Primus has been dispatched
  and 15 min have elapsed without a resolution logged.

RESOLUTION FEEDBACK:
  Primus/Cipher call mark_sqs_alert_resolved() (sqs_resolution_client.py)
  which clears the cooldown row → alert re-triggers naturally if issue recurs.

Alert queue files monitored:
  /Users/ahmedsadek/nexus/data/alert_queue.jsonl       — Nexus V1 alerts
  /Users/ahmedsadek/sqs/logs/sqs_alert_queue.jsonl     — SQS alerts

Author: GENESIS (v2 2026-05-08) | Original: OMNI 2026-04-29
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import requests

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("alert_monitor")

# ── Config ────────────────────────────────────────────────────────────────────
ALERT_QUEUES = [
    "/Users/ahmedsadek/nexus/data/alert_queue.jsonl",
    "/Users/ahmedsadek/sqs/logs/sqs_alert_queue.jsonl",
]

STATE_DB       = "/Users/ahmedsadek/nexus/data/alert_monitor_state.db"
BUS_URL        = "http://localhost:9999"
TG_BOT         = "8747601602:AAGTzRd3NJWq44Bvbzd5JvhtnO2edBUvjbc"
HEALTH_GROUP   = "-1003954790884"
AHMED_DM       = "8573754783"
VECTOR_URL     = "http://192.168.1.42:8030"      # Vector HTTP endpoint

# Cooldown windows (seconds)
PRIMUS_COOLDOWN_S  = 30 * 60   # 30 min — don't re-dispatch same issue to Primus
VECTOR_COOLDOWN_S  = 60 * 60   # 60 min — Vector gets one escalation per hour per issue
VECTOR_DELAY_S     = 15 * 60   # 15 min — wait before escalating to Vector (give Primus time)

ACT_ON = {"CRITICAL", "HIGH"}

# Service → responsible agent session key (bus recipient)
AGENT_ROUTING = {
    # SQS services → Primus
    "ATM_MULTIWEEK":    "agent:primus:main",
    "ATG_SWING":        "agent:primus:main",
    "ATM_0DTE":         "agent:primus:main",
    "ATG_INTRADAY":     "agent:primus:main",
    "CAPITAL_ROUTER":   "agent:primus:main",
    "sqs_monitor":      "agent:primus:main",
    # Nexus services → Cipher
    "nexus-alpha":      "agent:cipher:main",
    "alpha-execution":  "agent:cipher:main",
    "prime-execution":  "agent:cipher:main",
    "axiom":            "agent:cipher:main",
    "alpha-buffer":     "agent:cipher:main",
    "prime-buffer":     "agent:cipher:main",
    "omni":             "agent:cipher:main",
    "NEXUS":            "agent:cipher:main",
    "DEFAULT":          "agent:cipher:main",
}

# Services that should also escalate to Vector on CRITICAL
VECTOR_ESCALATION_SERVICES = {
    "ATM_MULTIWEEK", "ATG_SWING", "ATM_0DTE", "ATG_INTRADAY",
    "CAPITAL_ROUTER", "sqs_monitor",
    "alpha-execution", "prime-execution", "omni",
}


# ── State DB ──────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    Path(STATE_DB).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DB, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS processed_alerts (
            alert_hash   TEXT PRIMARY KEY,
            seen_at      TEXT NOT NULL,
            service      TEXT,
            severity     TEXT,
            title        TEXT,
            agent_routed TEXT,
            resolved     INTEGER DEFAULT 0
        );

        -- Per-issue cooldown tracking (flood prevention)
        -- issue_key = service:finding_code  e.g. "ATM_MULTIWEEK:F1"
        CREATE TABLE IF NOT EXISTS dispatch_cooldown (
            issue_key        TEXT NOT NULL,
            target           TEXT NOT NULL,   -- "primus" | "vector"
            dispatched_at    REAL NOT NULL,    -- unix timestamp
            dispatch_count   INTEGER DEFAULT 1,
            PRIMARY KEY (issue_key, target)
        );
    """)
    conn.commit()
    conn.close()


def _issue_key(alert: dict) -> str:
    """Stable key: service + finding code (F1–F10) or title prefix."""
    service = alert.get("service", "UNKNOWN")
    code    = alert.get("code", "")
    if not code:
        # Derive from title (first 40 chars, normalised)
        code = alert.get("title", "")[:40].strip().upper().replace(" ", "_")
    return f"{service}:{code}"


def alert_hash(alert: dict) -> str:
    """One-time hash for exact dedup (same service+title+ts)."""
    key = f"{alert.get('service','')}\x00{alert.get('title','')}\x00{alert.get('ts','')}"
    return hashlib.md5(key.encode()).hexdigest()


def is_exact_duplicate(h: str) -> bool:
    conn = _connect()
    row  = conn.execute(
        "SELECT 1 FROM processed_alerts WHERE alert_hash=?", (h,)
    ).fetchone()
    conn.close()
    return row is not None


def mark_exact_seen(alert: dict, h: str, agent: str) -> None:
    conn = _connect()
    conn.execute(
        """INSERT OR IGNORE INTO processed_alerts
           (alert_hash, seen_at, service, severity, title, agent_routed)
           VALUES (?,?,?,?,?,?)""",
        (h, datetime.datetime.now().isoformat(),
         alert.get("service","?"), alert.get("severity","?"),
         alert.get("title","?"), agent),
    )
    conn.commit()
    conn.close()


def in_cooldown(issue_key: str, target: str, window_s: float) -> bool:
    """
    Return True if we dispatched to 'target' for 'issue_key' within window_s seconds.
    False means: go ahead and dispatch.
    """
    now = time.time()
    conn = _connect()
    row = conn.execute(
        "SELECT dispatched_at FROM dispatch_cooldown WHERE issue_key=? AND target=?",
        (issue_key, target),
    ).fetchone()
    conn.close()
    if not row:
        return False
    elapsed = now - row[0]
    return elapsed < window_s


def record_dispatch(issue_key: str, target: str) -> None:
    now = time.time()
    conn = _connect()
    conn.execute(
        """INSERT INTO dispatch_cooldown (issue_key, target, dispatched_at, dispatch_count)
           VALUES (?, ?, ?, 1)
           ON CONFLICT(issue_key, target) DO UPDATE
           SET dispatched_at=excluded.dispatched_at,
               dispatch_count=dispatch_count+1""",
        (issue_key, target, now),
    )
    conn.commit()
    conn.close()


def clear_cooldown(issue_key: str) -> None:
    """Called by resolution client to allow re-dispatch if issue recurs."""
    conn = _connect()
    conn.execute(
        "DELETE FROM dispatch_cooldown WHERE issue_key=?",
        (issue_key,),
    )
    conn.execute(
        "UPDATE processed_alerts SET resolved=1 WHERE service=? AND title LIKE ?",
        (issue_key.split(":")[0], f"%{issue_key.split(':',1)[-1][:20]}%"),
    )
    conn.commit()
    conn.close()
    log.info("alert_monitor: cooldown cleared for issue_key=%s", issue_key)


def get_dispatch_time(issue_key: str, target: str) -> Optional[float]:
    conn = _connect()
    row = conn.execute(
        "SELECT dispatched_at FROM dispatch_cooldown WHERE issue_key=? AND target=?",
        (issue_key, target),
    ).fetchone()
    conn.close()
    return row[0] if row else None


# ── Telegram ──────────────────────────────────────────────────────────────────

def tg(chat_id: str, msg: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as exc:
        log.debug("Telegram send failed: %s", exc)


# ── Agent dispatch via bus ────────────────────────────────────────────────────

def dispatch_to_agent(session_key: str, alert: dict, role: str = "OPERATOR") -> bool:
    """
    Send alert to an agent via the OpenClaw message bus.
    role: "OPERATOR" (Primus/Cipher) | "INVESTIGATOR" (Vector)
    """
    service  = alert.get("service", "?")
    severity = alert.get("severity", "?")
    title    = alert.get("title", "?")
    detail   = alert.get("detail",  alert.get("details", ""))
    code     = alert.get("code", "?")
    rec      = alert.get("recommendation", "")
    ts       = alert.get("ts", "")
    agent    = session_key.split(":")[1]  # primus | cipher | vector

    if role == "INVESTIGATOR":
        msg = (
            f"🔍 ALERT MONITOR → VECTOR — ROOT CAUSE INVESTIGATION\n\n"
            f"Service:  {service}\n"
            f"Code:     {code}\n"
            f"Severity: {severity}\n"
            f"Alert:    {title}\n"
            f"Detail:   {detail}\n"
            f"Time:     {ts}\n\n"
            f"Context: Primus was dispatched {VECTOR_DELAY_S // 60} min ago but alert remains open.\n"
            f"Mandate: Investigate root cause. Cross-check DB, Capital Router, Alpaca. "
            f"Document root cause in CHRONICLE. Coordinate fix with Primus."
        )
    else:
        msg = (
            f"🚨 ALERT MONITOR → {agent.upper()} — IMMEDIATE ACTION REQUIRED\n\n"
            f"Service:  {service}\n"
            f"Code:     {code}\n"
            f"Severity: {severity}\n"
            f"Alert:    {title}\n"
            f"Detail:   {detail}\n"
            f"Time:     {ts}\n"
        )
        if rec:
            msg += f"\nRecommended fix: {rec}\n"
        msg += (
            f"\nMandate: Diagnose root cause, apply fix immediately, call "
            f"mark_sqs_alert_resolved('{service}', '{code}', '<resolution>') when done."
        )

    try:
        resp = requests.post(
            f"{BUS_URL}/send",
            json={"from": "alert-monitor", "to": agent, "message": msg},
            timeout=5,
        )
        ok = resp.status_code in (200, 201, 202)
        if ok:
            log.info("alert_monitor: dispatched to %s (%s) — %s:%s", agent, role, service, code)
        else:
            log.warning("alert_monitor: bus returned %d for %s", resp.status_code, agent)
        return ok
    except Exception as exc:
        log.warning("alert_monitor: bus dispatch failed for %s: %s", agent, exc)
        return False


def dispatch_to_vector_http(alert: dict) -> bool:
    """
    POST alert to Vector's HTTP endpoint for investigation.
    Falls back gracefully if Vector is unreachable.
    """
    try:
        payload = {
            "source":   "alert-monitor",
            "type":     "sqs_alert",
            "alert":    alert,
            "mandate":  "root_cause_investigation",
        }
        resp = requests.post(
            f"{VECTOR_URL}/investigate",
            json=payload,
            timeout=5,
        )
        return resp.status_code < 300
    except Exception as exc:
        log.debug("alert_monitor: Vector HTTP unreachable (%s) — bus fallback used", exc)
        return False


# ── Alert queue reader ────────────────────────────────────────────────────────

def read_alerts(path: str) -> list:
    alerts = []
    p = Path(path)
    if not p.exists():
        return alerts
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    alerts.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        log.warning("read_alerts: %s — %s", path, exc)
    return alerts


# ── Main dispatch logic ────────────────────────────────────────────────────────

def process_alert(alert: dict) -> None:
    severity = alert.get("severity", "").upper()
    if severity not in ACT_ON:
        return

    service    = alert.get("service", "DEFAULT")
    code       = alert.get("code", "?")
    title      = alert.get("title", "?")
    h          = alert_hash(alert)
    ik         = _issue_key(alert)

    # ── 1. Exact dedup: don't re-process the exact same alert entry
    if is_exact_duplicate(h):
        return

    mark_exact_seen(alert, h, "pending")
    log.info("alert_monitor: NEW %s alert — %s [%s] %s", severity, service, code, title)

    # ── 2. Route to primary agent (Primus / Cipher)
    agent_key = AGENT_ROUTING.get(service, AGENT_ROUTING["DEFAULT"])
    agent     = agent_key.split(":")[1]

    if not in_cooldown(ik, agent, PRIMUS_COOLDOWN_S):
        dispatched = dispatch_to_agent(agent_key, alert, role="OPERATOR")
        if dispatched:
            record_dispatch(ik, agent)
            # Update processed_alerts with actual routing
            conn = _connect()
            conn.execute(
                "UPDATE processed_alerts SET agent_routed=? WHERE alert_hash=?",
                (agent_key, h),
            )
            conn.commit()
            conn.close()

        # Telegram: post to health group for visibility
        sev_icon = "🚨" if severity == "CRITICAL" else "⚠️"
        tg(HEALTH_GROUP,
           f"{sev_icon} <b>ALERT MONITOR</b> — {severity}\n"
           f"<b>{service} [{code}]:</b> {title}\n"
           f"→ Dispatched to <b>{agent.upper()}</b> for immediate fix")
    else:
        log.info("alert_monitor: %s → %s cooldown active (%s min window) — skipping", ik, agent, PRIMUS_COOLDOWN_S // 60)

    # ── 3. Vector escalation for CRITICAL (after primary agent dispatch delay)
    if severity == "CRITICAL" and service in VECTOR_ESCALATION_SERVICES:
        primus_dispatch_time = get_dispatch_time(ik, agent)

        if primus_dispatch_time is None:
            # Primary agent wasn't dispatched this cycle (cooldown); check if enough time passed
            primus_dispatch_time = time.time()  # treat as now for conservative wait

        elapsed = time.time() - primus_dispatch_time
        vector_ok = (elapsed >= VECTOR_DELAY_S) and not in_cooldown(ik, "vector", VECTOR_COOLDOWN_S)

        if vector_ok:
            log.info("alert_monitor: escalating %s to VECTOR (elapsed %.0f min)", ik, elapsed / 60)
            # Try bus first, then HTTP
            v_dispatched = dispatch_to_agent("agent:vector:main", alert, role="INVESTIGATOR")
            if not v_dispatched:
                v_dispatched = dispatch_to_vector_http(alert)
            if v_dispatched:
                record_dispatch(ik, "vector")
                tg(HEALTH_GROUP,
                   f"🔍 <b>ALERT MONITOR</b> — VECTOR ESCALATION\n"
                   f"<b>{service} [{code}]</b> unresolved after {elapsed // 60:.0f} min\n"
                   f"→ VECTOR assigned for root cause investigation")
        elif not in_cooldown(ik, "vector", VECTOR_COOLDOWN_S) and primus_dispatch_time:
            # Too early — first dispatch just happened; Vector will engage on next cycle
            log.debug(
                "alert_monitor: %s — Vector delay not reached yet (%.0f / %d min)",
                ik, elapsed / 60, VECTOR_DELAY_S // 60,
            )


def run() -> None:
    init_db()
    now_str = datetime.datetime.now().strftime("%H:%M")
    new_actioned = 0

    for queue_path in ALERT_QUEUES:
        alerts = read_alerts(queue_path)
        for alert in alerts:
            severity = alert.get("severity", "").upper()
            if severity not in ACT_ON:
                continue
            h = alert_hash(alert)
            if not is_exact_duplicate(h):
                process_alert(alert)
                new_actioned += 1

    if new_actioned == 0:
        log.info("[%s] No new actionable alerts.", now_str)
    else:
        log.info("[%s] Processed %d alert(s).", now_str, new_actioned)


if __name__ == "__main__":
    run()
