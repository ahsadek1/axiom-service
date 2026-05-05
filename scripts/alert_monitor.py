#!/usr/bin/env python3
"""
alert_monitor.py — Unified Nexus Alert Monitor
===============================================
Polls the unified alert queue every cycle.
On NEW CRITICAL/HIGH alert → immediately routes to responsible agent via OpenClaw bus.
Tracks state to avoid re-firing on same alert.

Alert queue files monitored:
  /Users/ahmedsadek/nexus/data/alert_queue.jsonl       — Nexus V1 alerts
  /Users/ahmedsadek/sqs/logs/sqs_alert_queue.jsonl     — SQS alerts

Author: OMNI 🌐
Date: 2026-04-29
Run via OpenClaw cron every 60s during market hours.
"""

import json
import os
import sys
import time
import datetime
import sqlite3
import hashlib
import requests
from pathlib import Path

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

# Alert severity → action threshold
# CRITICAL → immediate agent dispatch + Ahmed DM if not resolved in 5 min
# HIGH     → immediate agent dispatch, no Ahmed DM
# MEDIUM   → log only, no dispatch
ACT_ON = {"CRITICAL", "HIGH"}

# Service → responsible OpenClaw agent session key
AGENT_ROUTING = {
    "ATM_MULTIWEEK":    "agent:primus:main",
    "ATG_SWING":        "agent:primus:main",
    "ATM_0DTE":         "agent:primus:main",
    "ATG_INTRADAY":     "agent:primus:main",
    "CAPITAL_ROUTER":   "agent:primus:main",
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

# ── State DB ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(STATE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_alerts (
            alert_hash  TEXT PRIMARY KEY,
            seen_at     TEXT NOT NULL,
            service     TEXT,
            severity    TEXT,
            title       TEXT,
            agent_routed TEXT,
            resolved    INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def alert_hash(alert: dict) -> str:
    key = f"{alert.get('service','')}|{alert.get('title','')}|{alert.get('ts','')}"
    return hashlib.md5(key.encode()).hexdigest()

def is_processed(h: str) -> bool:
    conn = sqlite3.connect(STATE_DB)
    row = conn.execute("SELECT 1 FROM processed_alerts WHERE alert_hash=?", (h,)).fetchone()
    conn.close()
    return row is not None

def mark_processed(alert: dict, h: str, agent: str):
    conn = sqlite3.connect(STATE_DB)
    conn.execute("""
        INSERT OR IGNORE INTO processed_alerts
        (alert_hash, seen_at, service, severity, title, agent_routed)
        VALUES (?,?,?,?,?,?)
    """, (h, datetime.datetime.now().isoformat(),
          alert.get("service","?"), alert.get("severity","?"),
          alert.get("title","?"), agent))
    conn.commit()
    conn.close()

# ── Telegram ──────────────────────────────────────────────────────────────────
def tg(chat_id: str, msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception:
        pass

# ── Bus dispatch ──────────────────────────────────────────────────────────────
def dispatch_to_agent(session_key: str, alert: dict):
    """Send alert to responsible agent via OpenClaw message bus."""
    service  = alert.get("service", "?")
    severity = alert.get("severity", "?")
    title    = alert.get("title", "?")
    details  = alert.get("details", "")
    ts       = alert.get("ts", "")
    msg = (
        f"🚨 ALERT MONITOR → {session_key.split(':')[1].upper()} — IMMEDIATE ACTION\n\n"
        f"Service: {service}\n"
        f"Severity: {severity}\n"
        f"Alert: {title}\n"
        f"Details: {details}\n"
        f"Time: {ts}\n\n"
        f"Mandate: diagnose root cause, fix immediately, report back to OMNI."
    )
    try:
        agent_name = session_key.split(":")[1]
        resp = requests.post(
            f"{BUS_URL}/send",
            json={"from": "alert-monitor", "to": agent_name, "message": msg},
            timeout=5,
        )
        return resp.status_code in (200, 201, 202)
    except Exception:
        return False

# ── Alert queue reader ────────────────────────────────────────────────────────
def read_alerts(path: str) -> list:
    alerts = []
    p = Path(path)
    if not p.exists():
        return alerts
    try:
        with open(p, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    alerts.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return alerts

# ── Main ──────────────────────────────────────────────────────────────────────
def run():
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
            if is_processed(h):
                continue

            # New unprocessed actionable alert
            service = alert.get("service", "DEFAULT")
            agent_key = AGENT_ROUTING.get(service, AGENT_ROUTING["DEFAULT"])

            print(f"[{now_str}] NEW {severity} alert: {service} — {alert.get('title','?')}")
            print(f"  → Routing to {agent_key}")

            dispatched = dispatch_to_agent(agent_key, alert)
            mark_processed(alert, h, agent_key if dispatched else "dispatch_failed")
            new_actioned += 1

            # Also post to Health Group for visibility
            tg(HEALTH_GROUP,
               f"🚨 <b>ALERT MONITOR</b> — {severity}\n"
               f"<b>{service}:</b> {alert.get('title','?')}\n"
               f"→ Routed to {agent_key.split(':')[1].upper()} for immediate fix")

    if new_actioned == 0:
        print(f"[{now_str}] No new actionable alerts.")
    else:
        print(f"[{now_str}] Actioned {new_actioned} alert(s).")

if __name__ == "__main__":
    run()
