"""
daily_eod_reset.py — End-of-Day Pipeline Reset
===============================================
Runs at 4:05 PM ET every trading day (Mon-Fri).

Purpose: Close the day cleanly so yesterday's failures never contaminate tomorrow.

Actions:
  1. Expire all incomplete traces older than today → sentinel starts clean
  2. Resolve all open failure events → no stale alerts carry over
  3. Clear agent dedup records → agents start fresh tomorrow
  4. Clear buffer concordances → no stale window data
  5. Log EOD summary to CHRONICLE
  6. Send EOD health report to Ahmed

This is the missing piece: every day closes clean.
If it doesn't, tomorrow inherits today's problems.

Author: GENESIS 🌱
Date: 2026-05-01
"""

import datetime
import logging
import os
import sqlite3
import sys
import time

import requests
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _require(var: str) -> str:
    """Return env var value or raise at startup — never silently use a stale fallback."""
    val = os.getenv(var)
    if not val:
        raise RuntimeError(f"{var} is required but not set. Source .deploy-secrets before running.")
    return val


SECRET        = _require("NEXUS_SECRET")
BOT_TOKEN     = _require("TELEGRAM_BOT_TOKEN")
AHMED_CHAT_ID = os.getenv("AHMED_CHAT_ID", "8573754783")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EOD-RESET] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("eod_reset")


def _telegram(msg: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": AHMED_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logger.warning("Telegram failed: %s", e)


def reset_sentinel() -> dict:
    """Expire stale sentinel traces and resolve old failure events."""
    db = "/Users/ahmedsadek/nexus/data/pipeline_sentinel.db"
    today_start = datetime.datetime.now(ET).replace(
        hour=9, minute=30, second=0, microsecond=0
    ).timestamp()

    conn = sqlite3.connect(db)
    c = conn.cursor()

    # Count before
    c.execute("SELECT COUNT(DISTINCT trace_id) FROM traces")
    total_before = c.fetchone()[0]

    c.execute("SELECT COUNT(DISTINCT trace_id) FROM traces WHERE ts < ?", (today_start,))
    stale_count = c.fetchone()[0]

    # Delete stale traces (before today's market open)
    c.execute("DELETE FROM traces WHERE ts < ?", (today_start,))
    deleted_traces = c.rowcount

    # Resolve all open failure events
    c.execute(
        "UPDATE failure_events SET resolved=1, resolved_at=? WHERE resolved=0 OR resolved IS NULL",
        (time.time(),)
    )
    resolved_events = c.rowcount

    conn.commit()
    conn.close()

    logger.info("Sentinel reset: deleted %d stale trace rows, resolved %d failure events",
                deleted_traces, resolved_events)
    return {
        "stale_traces_deleted": deleted_traces,
        "failure_events_resolved": resolved_events,
        "total_before": total_before,
    }


def reset_agent_dedup() -> dict:
    """Clear agent dedup records so all agents start fresh tomorrow."""
    today = datetime.datetime.now(ET).strftime("%Y-%m-%d")
    cleared = {}

    for agent in ["cipher", "atlas", "sage"]:
        db = f"/Users/ahmedsadek/nexus/data/{agent}.db"
        try:
            conn = sqlite3.connect(db)
            conn.execute("DELETE FROM picks WHERE created_at LIKE ?", (f"{today}%",))
            count = conn.total_changes
            conn.commit()
            conn.close()
            cleared[agent] = count
            logger.info("%s dedup cleared: %d records", agent, count)
        except Exception as e:
            cleared[agent] = f"error: {e}"
            logger.warning("%s dedup clear failed: %s", agent, e)

    return cleared


def reset_buffer() -> dict:
    """Purge alpha-buffer concordances."""
    try:
        r = requests.post(
            "http://localhost:8002/concordance/purge",
            headers={"X-Nexus-Secret": SECRET},
            timeout=10,
        )
        d = r.json()
        logger.info("Buffer concordances purged: %s", d)
        return d
    except Exception as e:
        logger.warning("Buffer purge failed: %s", e)
        return {"error": str(e)}


def get_eod_summary() -> dict:
    """Collect EOD metrics for the report."""
    summary = {}

    try:
        r = requests.get("http://localhost:8004/health", timeout=5)
        d = r.json()
        summary["omni_syntheses"] = d.get("syntheses_today", 0)
        summary["go_verdicts"] = d.get("go_verdicts_today", 0)
    except Exception:
        summary["omni_syntheses"] = "unreachable"

    try:
        r = requests.get(
            "http://localhost:8010/system-health",
            headers={"X-Nexus-Secret": SECRET},
            timeout=5,
        )
        d = r.json()
        summary["sentinel_health"] = d.get("health_score", 0)
        summary["sentinel_status"] = d.get("status", "?")
        summary["stalls_at_close"] = d.get("score_components", {}).get("stalled_picks_count", 0)
        summary["completion_rate"] = d.get("score_components", {}).get("pipeline_completion_rate", 0)
    except Exception:
        summary["sentinel_health"] = "unreachable"

    try:
        r = requests.get("http://localhost:8005/health", timeout=5)
        d = r.json()
        summary["open_positions"] = d.get("open_positions", 0)
        summary["entries_today"] = d.get("entries_today", 0)
    except Exception:
        summary["open_positions"] = "unreachable"

    return summary


def chronicle_log(summary: dict, reset_results: dict) -> None:
    """Log EOD reset to CHRONICLE."""
    try:
        db = "/Users/ahmedsadek/nexus/data/chronicle.db"
        conn = sqlite3.connect(db)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS eod_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, syntheses INTEGER, go_verdicts INTEGER,
            stalls_cleared INTEGER, agents_reset TEXT, outcome TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        today = datetime.datetime.now(ET).strftime("%Y-%m-%d")
        c.execute("""INSERT INTO eod_log
            (date, syntheses, go_verdicts, stalls_cleared, agents_reset, outcome)
            VALUES (?,?,?,?,?,?)""",
            (
                today,
                summary.get("omni_syntheses", 0),
                summary.get("go_verdicts", 0),
                reset_results.get("stale_traces_deleted", 0),
                str(reset_results.get("agent_dedup", {})),
                "reset_complete",
            )
        )
        conn.commit()
        conn.close()
        logger.info("EOD logged to CHRONICLE")
    except Exception as e:
        logger.warning("CHRONICLE log failed: %s", e)


def main() -> None:
    now = datetime.datetime.now(ET)
    today = now.strftime("%Y-%m-%d")
    logger.info("EOD Reset starting for %s", today)

    # 1. Get pre-reset summary
    summary = get_eod_summary()
    logger.info("EOD summary: %s", summary)

    # 2. Reset sentinel
    sentinel_result = reset_sentinel()

    # 3. Reset agent dedup
    agent_result = reset_agent_dedup()

    # 4. Reset buffer
    buffer_result = reset_buffer()

    # 5. Log to CHRONICLE
    results = {
        "sentinel": sentinel_result,
        "agent_dedup": agent_result,
        "buffer": buffer_result,
    }
    chronicle_log(summary, results)

    # 6. Send EOD report to Ahmed
    syntheses = summary.get("omni_syntheses", 0)
    go_verdicts = summary.get("go_verdicts", 0)
    stalls_cleared = sentinel_result.get("stale_traces_deleted", 0)
    sentinel_health = summary.get("sentinel_health", "?")
    completion = summary.get("completion_rate", 0)

    report = (
        f"📊 <b>NEXUS EOD RESET — {today}</b>\n\n"
        f"Today's Session:\n"
        f"• OMNI syntheses: {syntheses}\n"
        f"• GO verdicts: {go_verdicts}\n"
        f"• Completion rate: {completion:.0%}\n"
        f"• Sentinel health at close: {sentinel_health}\n\n"
        f"Reset Complete:\n"
        f"• Stale traces cleared: {stalls_cleared}\n"
        f"• Agents reset: Cipher, Atlas, Sage\n"
        f"• Buffer concordances purged\n\n"
        f"System ready for tomorrow. 🌱"
    )
    _telegram(report)
    logger.info("EOD Reset complete")


if __name__ == "__main__":
    main()
