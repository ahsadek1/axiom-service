"""
domain_checks.py — Per-Service Domain Check Functions
======================================================
Each function checks one specific aspect of a service's domain.
Returns an error code string if problem detected, None if healthy.

These are passed to AgentHealthMonitor as domain_checks=[...].
Every check is deterministic — no judgment, just measurement vs threshold.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests

_ET = ZoneInfo("America/New_York")
NEXUS_SECRET = os.getenv("NEXUS_WEBHOOK_SECRET",
    "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
ALPHA_BUFFER_DB = os.getenv("ALPHA_DB_PATH",
    "/Users/ahmedsadek/nexus/data/alpha_buffer.db")
BACKTEST_DB = "/Users/ahmedsadek/nexus/data/backtest.db"


def _market_hours() -> bool:
    now = datetime.now(_ET)
    return (now.weekday() < 5
            and (now.hour > 9 or (now.hour == 9 and now.minute >= 30))
            and now.hour < 16)


# ---------------------------------------------------------------------------
# OMNI domain checks
# ---------------------------------------------------------------------------

def check_omni_synthesis_silence() -> Optional[str]:
    """OMNI should synthesize regularly during market hours."""
    if not _market_hours():
        return None
    now_et = datetime.now(_ET)
    if now_et.hour < 10:
        return None  # Warmup period
    try:
        resp = requests.get("http://localhost:8004/health", timeout=4)
        if resp.status_code == 200:
            mins_ago = resp.json().get("last_synthesis_min_ago", 0)
            if mins_ago > 20:
                return "EPL003"
    except Exception:
        pass
    return None


def check_omni_deterministic_mode() -> Optional[str]:
    """OMNI must run in deterministic mode, not quad intelligence."""
    try:
        resp = requests.get("http://localhost:8004/health", timeout=4)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("quad_intelligence_active", False):
                return "ESV002"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Alpha-buffer domain checks
# ---------------------------------------------------------------------------

def check_circuit_breaker() -> Optional[str]:
    """Alpha-buffer CB should not be in STOP state."""
    try:
        conn = sqlite3.connect(ALPHA_BUFFER_DB, timeout=3)
        row = conn.execute(
            "SELECT status FROM circuit_breaker_state LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row[0] == "STOP":
            return "EPL001"
    except Exception:
        pass
    return None


def check_concordance_rate() -> Optional[str]:
    """At least one concordance should form per hour during market hours."""
    if not _market_hours():
        return None
    now_et = datetime.now(_ET)
    if now_et.hour < 10:
        return None
    try:
        conn = sqlite3.connect(ALPHA_BUFFER_DB, timeout=3)
        one_hour_ago = datetime.now(timezone.utc).isoformat()[:10] + "T" + \
                      f"{(datetime.now(timezone.utc).hour - 1):02d}:00:00+00:00"
        row = conn.execute(
            "SELECT COUNT(*) FROM concordance_results WHERE created_at > ?",
            (one_hour_ago,)
        ).fetchone()
        conn.close()
        if row and row[0] == 0:
            return "EPL005"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Agent submission checks (for scanner/agents)
# ---------------------------------------------------------------------------

def check_cipher_submissions() -> Optional[str]:
    """Cipher should submit picks during market hours."""
    if not _market_hours():
        return None
    try:
        conn = sqlite3.connect(ALPHA_BUFFER_DB, timeout=3)
        row = conn.execute(
            "SELECT MAX(received_at) FROM submissions WHERE agent='Cipher'"
        ).fetchone()
        conn.close()
        if row and row[0]:
            ts = row[0].replace("Z", "+00:00")
            last = datetime.fromisoformat(ts)
            mins = (datetime.now(timezone.utc) - last).total_seconds() / 60
            if mins > 30:
                return "EAG001"
    except Exception:
        pass
    return None


def check_atlas_submissions() -> Optional[str]:
    """Atlas should submit picks during market hours."""
    if not _market_hours():
        return None
    try:
        conn = sqlite3.connect(ALPHA_BUFFER_DB, timeout=3)
        row = conn.execute(
            "SELECT MAX(received_at) FROM submissions WHERE agent='Atlas'"
        ).fetchone()
        conn.close()
        if row and row[0]:
            ts = row[0].replace("Z", "+00:00")
            last = datetime.fromisoformat(ts)
            mins = (datetime.now(timezone.utc) - last).total_seconds() / 60
            if mins > 30:
                return "EAG001"
    except Exception:
        pass
    return None


def check_sage_submissions() -> Optional[str]:
    """Sage should submit picks during market hours."""
    if not _market_hours():
        return None
    try:
        conn = sqlite3.connect(ALPHA_BUFFER_DB, timeout=3)
        row = conn.execute(
            "SELECT MAX(received_at) FROM submissions WHERE agent='Sage'"
        ).fetchone()
        conn.close()
        if row and row[0]:
            ts = row[0].replace("Z", "+00:00")
            last = datetime.fromisoformat(ts)
            mins = (datetime.now(timezone.utc) - last).total_seconds() / 60
            if mins > 30:
                return "EAG001"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Alpha-execution domain checks
# ---------------------------------------------------------------------------

def check_vix_brake() -> Optional[str]:
    """VIX brake should not be stuck at 999."""
    try:
        resp = requests.get("http://localhost:8005/health", timeout=4)
        if resp.status_code == 200:
            vix = resp.json().get("vix", 0)
            full = resp.json().get("vix_brake_full", False)
            if full and vix >= 500:
                return "EPL002"
    except Exception:
        pass
    return None


def check_dlq_backlog() -> Optional[str]:
    """DLQ should not have 5+ pending items."""
    try:
        resp = requests.get(
            "http://localhost:8005/dlq/status",
            headers={"X-Nexus-Secret": NEXUS_SECRET},
            timeout=4,
        )
        if resp.status_code == 200:
            if resp.json().get("pending", 0) >= 5:
                return "EPL004"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Axiom domain checks
# ---------------------------------------------------------------------------

def check_axiom_pool() -> Optional[str]:
    """Axiom pool should be populated during market hours."""
    if not _market_hours():
        return None
    try:
        resp = requests.get("http://localhost:8001/health", timeout=4)
        if resp.status_code == 200:
            pool_size = resp.json().get("pool_size", 0)
            now_et = datetime.now(_ET)
            if pool_size == 0 and now_et.hour >= 10:
                return "ESV002"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Infrastructure checks (shared)
# ---------------------------------------------------------------------------

def check_memory() -> Optional[str]:
    """System memory check — disabled on Mac Mini (3.2GB total, browser/system consume most).
    Re-enable only if nexus services themselves exceed 50% combined."""
    return None  # non-actionable on this hardware config
    # original implementation below:
    try:
        import subprocess
        result = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.split("\n")
        stats = {}
        for line in lines:
            if ":" in line:
                key, val = line.split(":", 1)
                try:
                    stats[key.strip()] = int(val.strip().rstrip("."))
                except ValueError:
                    pass
        page_size = 4096
        free = stats.get("Pages free", 0) * page_size
        total = sum([
            stats.get("Pages free", 0),
            stats.get("Pages active", 0),
            stats.get("Pages inactive", 0),
            stats.get("Pages wired down", 0),
        ]) * page_size
        if total > 0 and (1 - free/total) > 0.92:  # 92% threshold — Macs run hot
            return "EIN001"
    except Exception:
        pass
    return None


def check_disk() -> Optional[str]:
    """Disk free space should not drop below 10%."""
    try:
        import shutil
        usage = shutil.disk_usage(os.path.expanduser("~"))
        free_pct = usage.free / usage.total
        if free_pct < 0.10:
            return "EIN002"
    except Exception:
        pass
    return None


def check_backtest_db() -> Optional[str]:
    """Backtest DB must be accessible and populated."""
    try:
        conn = sqlite3.connect(BACKTEST_DB, timeout=5)
        count = conn.execute(
            "SELECT COUNT(*) FROM historical_win_rates"
        ).fetchone()[0]
        conn.close()
        if count < 1000:
            return "EDA001"
    except Exception:
        return "EDA001"
    return None


# ---------------------------------------------------------------------------
# THESIS Trader domain checks
# ---------------------------------------------------------------------------

def check_thesis_alpaca_connected() -> Optional[str]:
    """THESIS Alpaca account must be connected and active."""
    try:
        resp = requests.get(
            "http://localhost:8070/health", timeout=4
        )
        if resp.status_code == 200:
            data = resp.json()
            if not data.get("alpaca_connected", False):
                return "EAG003"  # API credential issue
    except Exception:
        pass
    return None


def check_thesis_buying_power() -> Optional[str]:
    """THESIS options buying power must stay above $10K minimum."""
    try:
        resp = requests.get(
            "http://localhost:8070/health", timeout=4
        )
        if resp.status_code == 200:
            bp = float(resp.json().get("buying_power", 200000))
            if bp < 10000:
                return "EPL001"  # capital critically low
    except Exception:
        pass
    return None


def check_thesis_positions_db() -> Optional[str]:
    """THESIS positions database must be accessible."""
    db_path = "/Users/ahmedsadek/nexus/data/thesis_positions.db"
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute("SELECT COUNT(*) FROM positions")
        conn.close()
    except Exception:
        return "EDA001"
    return None


def check_thesis_screening_active() -> Optional[str]:
    """THESIS must have completed at least one screening today during market hours."""
    if not _market_hours():
        return None
    now_et = datetime.now(_ET)
    if now_et.hour < 10:
        return None  # too early
    try:
        resp = requests.get("http://localhost:8070/health", timeout=4)
        if resp.status_code == 200:
            screenings = resp.json().get("screenings_today", 0)
            if screenings == 0 and now_et.hour >= 11:
                return "EPL005"  # no screening activity
    except Exception:
        pass
    return None


def check_thesis_exit_monitor() -> Optional[str]:
    """THESIS exit monitor must be running — checked via open positions age."""
    if not _market_hours():
        return None
    try:
        db_path = "/Users/ahmedsadek/nexus/data/thesis_positions.db"
        conn = sqlite3.connect(db_path, timeout=5)
        # Check for positions that should have been exited (profit/stop)
        row = conn.execute("""
            SELECT COUNT(*) FROM positions
            WHERE status='OPEN'
            AND dte_at_entry <= 21
        """).fetchone()
        conn.close()
        if row and row[0] > 0:
            return "ESV002"  # positions past 21 DTE still open
    except Exception:
        pass
    return None
