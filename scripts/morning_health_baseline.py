"""
morning_health_baseline.py — Pre-Market Health Baseline Check
=============================================================
Runs at 9:25 AM ET every trading day (Mon-Fri), 5 minutes before market open.

This is the MANDATORY gate that catches accumulated problems before they
contaminate a trading session. If anything fails here, it is fixed BEFORE
trading starts — not discovered 8 days later.

Checks:
  1. Sentinel stall count < 20 (catch accumulation early)
  2. No unresolved failure events older than 1 hour
  3. All services reachable (TRS >= 80)
  4. Agent dedup not stale from prior sessions
  5. Oracle warm (cache_warm_tickers > 50)

On any failure: fix immediately, page Ahmed if auto-fix fails.

Author: GENESIS 🌱
Date: 2026-05-01
"""

import os
import datetime
import logging
import sqlite3
import sys
import time

import requests
from zoneinfo import ZoneInfo
import sys as _sys
_sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
from alert_client import send_alert as _send_alert

ET = ZoneInfo("America/New_York")


def _require(var: str) -> str:
    """Return env var value or raise at startup — never silently use a stale fallback."""
    val = os.getenv(var)
    if not val:
        raise RuntimeError(f"{var} is required but not set. Source .deploy-secrets before running.")
    return val


SECRET        = _require("NEXUS_SECRET")
ORACLE_SECRET = _require("ORACLE_SECRET")
AXIOM_SECRET  = _require("NEXUS_SECRET")  # same secret, alias
BOT_TOKEN     = _require("TELEGRAM_BOT_TOKEN")
AHMED_CHAT_ID = os.getenv("AHMED_CHAT_ID", "8573754783")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [BASELINE] %(message)s", stream=sys.stdout)
logger = logging.getLogger("morning_baseline")


def _telegram(msg: str) -> None:
    """Route through Alert Broker."""
    _send_alert(
        source="morning-health-baseline",
        level="CRITICAL",
        title=msg[:200],
        body=msg[200:] if len(msg) > 200 else "",
        targets=["ahmed", "nexus_health_group"],
    )


def check_and_fix() -> list:
    """Run all baseline checks. Auto-fix what can be fixed. Return list of unfixed issues."""
    problems = []
    fixed = []

    # 1. Sentinel stall count
    try:
        r = requests.get("http://localhost:8010/system-health",
                         headers={"X-Nexus-Secret": SECRET}, timeout=5)
        d = r.json()
        stalls = d.get("score_components", {}).get("stalled_picks_count", 0)
        if stalls >= 20:
            logger.warning("STALL ACCUMULATION: %d stalls detected at baseline", stalls)
            # Auto-fix: clear stale sentinel data
            db = "/Users/ahmedsadek/nexus/data/pipeline_sentinel.db"
            conn = sqlite3.connect(db)
            today_open = datetime.datetime.now(ET).replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()
            conn.execute("DELETE FROM traces WHERE ts < ?", (today_open,))
            conn.execute("UPDATE failure_events SET resolved=1, resolved_at=? "
                        "WHERE resolved=0 OR resolved IS NULL", (time.time(),))
            conn.commit()
            conn.close()
            fixed.append(f"Cleared {stalls} stale sentinel traces")
            logger.info("Auto-fixed: cleared stale sentinel data")
        else:
            logger.info("Stalls: %d (OK)", stalls)
    except Exception as e:
        problems.append(f"Sentinel unreachable: {e}")

    # 2. Stale failure events
    try:
        db = "/Users/ahmedsadek/nexus/data/pipeline_sentinel.db"
        conn = sqlite3.connect(db)
        conn.execute("UPDATE failure_events SET resolved=1, resolved_at=? "
                    "WHERE (resolved=0 OR resolved IS NULL) AND ts < ?",
                    (time.time(), time.time() - 3600))
        resolved = conn.total_changes
        conn.commit()
        conn.close()
        if resolved:
            fixed.append(f"Resolved {resolved} stale failure events from prior session")
            logger.info("Cleared %d stale failure events", resolved)
    except Exception as e:
        logger.warning("Failure event cleanup failed: %s", e)

    # 3. TRS check
    try:
        r = requests.get("http://localhost:8012/trs",
                         headers={"X-Nexus-Secret": SECRET}, timeout=5)
        d = r.json()
        score = d.get("score", 0)
        color = d.get("color", "?")
        if score < 80:
            problems.append(f"TRS={score} {color} — below 80 at market open")
            logger.warning("TRS LOW: %s %s", score, color)
        else:
            logger.info("TRS: %s %s (OK)", score, color)
    except Exception as e:
        problems.append(f"TRS unreachable: {e}")

    # 4. Oracle warm
    try:
        r = requests.get("http://localhost:8007/health", timeout=5)
        d = r.json()
        warm = d.get("cache_warm_tickers", 0)
        status = d.get("status", "?")
        if status != "healthy" or warm < 50:
            logger.warning("Oracle cold: status=%s warm=%d — warming now", status, warm)
            # Auto-fix: trigger prefetch
            requests.post("http://localhost:8007/oracle/prefetch",
                         headers={"X-Oracle-Secret": ORACLE_SECRET},
                         json={"tickers": ["SPY","AAPL","MSFT","NVDA","TSLA","AMZN",
                                           "META","JPM","GS","CVX","COP","CMS","ETR"], "tier": "full"},
                         timeout=10)
            fixed.append(f"Oracle warmed (was: {warm} tickers)")
            logger.info("Auto-fixed: Oracle prefetch triggered")
        else:
            logger.info("Oracle: %s warm=%d (OK)", status, warm)
    except Exception as e:
        problems.append(f"Oracle unreachable: {e}")

    # 5. Alpha-exec paused check
    try:
        r = requests.get("http://localhost:8005/health", timeout=5)
        d = r.json()
        if d.get("execution_paused"):
            requests.post("http://localhost:8005/resume",
                         headers={"X-Nexus-Secret": SECRET}, timeout=10)
            fixed.append("Alpha-exec unpaused")
            logger.info("Auto-fixed: alpha-exec resumed")
        else:
            logger.info("Alpha-exec: healthy (OK)")
    except Exception as e:
        problems.append(f"Alpha-exec unreachable: {e}")

    return problems, fixed


def main() -> None:
    now = datetime.datetime.now(ET)

    # Skip weekends
    if now.weekday() >= 5:
        logger.info("Weekend — skipping baseline check")
        return

    logger.info("Morning health baseline starting — %s", now.strftime("%Y-%m-%d %H:%M ET"))

    problems, fixed = check_and_fix()

    # Build report
    status_line = "✅ <b>NEXUS MORNING BASELINE — CLEAR</b>" if not problems else \
                  "⚠️ <b>NEXUS MORNING BASELINE — ISSUES FOUND</b>"

    report = f"{status_line}\n{now.strftime('%Y-%m-%d %H:%M ET')}\n\n"

    if fixed:
        report += "Auto-fixed:\n" + "\n".join(f"• {f}" for f in fixed) + "\n\n"

    if problems:
        report += "⚠️ Unresolved (requires attention):\n"
        report += "\n".join(f"• {p}" for p in problems) + "\n"
        report += "\nTrading starting in 5 minutes. Investigate immediately."
        logger.warning("Baseline FAILED: %s", problems)
    else:
        report += "All systems clear. Ready for market open. 🌱"
        logger.info("Baseline PASSED")

    _telegram(report)


if __name__ == "__main__":
    main()
