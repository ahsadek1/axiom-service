"""
backup_universe.py — Static Fallback Universe
===============================================
When Axiom is down or degraded, Prime V2 and Alpha-V2
need a pre-screened universe to score independently.

This is NOT as good as live Axiom screening.
It is infinitely better than zero trades.

The backup universe is the top 60 S&P500+NASDAQ100
tickers selected for:
  - Consistent liquidity (>$50M daily volume)
  - High historical concordance rate with nexus_scorer
  - Broad sector coverage (not concentrated in one sector)
  - Regularly updated by learning loop (weekly)

Position sizing penalty when Axiom is down:
  All positions sized at 70% of normal
  Flagged as AXIOM_FALLBACK in DB
  Tracked separately in learning loop
"""
from __future__ import annotations
import json
import logging
import os
import sqlite3
import time
from datetime import date, timedelta
from typing import Optional

log = logging.getLogger("nexus.backup_universe")

BACKUP_DB   = os.getenv("PRIME_V2_DB",
              "/Users/ahmedsadek/nexus/data/prime_v2.db")
AXIOM_URL   = os.getenv("AXIOM_URL", "http://localhost:8001")
AXIOM_SECRET = os.getenv("AXIOM_SECRET", "")

# Position size penalty when running on backup universe
AXIOM_FALLBACK_SIZE_MULT = 0.70

# Static backup universe — broad sector coverage
# Updated weekly by learning loop based on concordance performance
DEFAULT_BACKUP_UNIVERSE = [
    # Technology (14)
    "AAPL","MSFT","NVDA","AVGO","AMD","QCOM","TXN","INTC",
    "AMAT","LRCX","KLAC","ADI","CDNS","SNPS",
    # Communication (4)
    "GOOGL","META","NFLX","CRM",
    # Consumer Discretionary (6)
    "AMZN","TSLA","HD","MCD","BKNG","TJX",
    # Financials (6)
    "JPM","BAC","GS","V","MA","AXP",
    # Healthcare (6)
    "UNH","JNJ","ABBV","LLY","MRK","TMO",
    # Industrials (6)
    "GE","CAT","HON","RTX","UNP","ETN",
    # Energy (4)
    "XOM","CVX","COP","EOG",
    # Materials + Real Estate (4)
    "AMT","PLD","SHW","ECL",
    # Consumer Staples (4)
    "KO","PEP","PG","COST",
    # Utilities (2)
    "NEE","DUK",
    # High-momentum additions (4)
    "IBM","HPQ","NTAP","HPE",
]


def init_backup_table() -> None:
    """Initialize backup universe tracking table."""
    try:
        conn = sqlite3.connect(BACKUP_DB, timeout=5)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS backup_universe (
                ticker          TEXT PRIMARY KEY,
                sector          TEXT,
                concordance_rate REAL DEFAULT 0.0,
                avg_score       REAL DEFAULT 0.0,
                times_scored    INTEGER DEFAULT 0,
                last_updated    TEXT,
                active          INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS axiom_status_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                status      TEXT,
                reason      TEXT,
                ts          REAL
            );
        """)
        # Seed with defaults if empty
        count = conn.execute(
            "SELECT COUNT(*) FROM backup_universe"
        ).fetchone()[0]
        if count == 0:
            now = date.today().isoformat()
            for ticker in DEFAULT_BACKUP_UNIVERSE:
                conn.execute("""
                    INSERT OR IGNORE INTO backup_universe
                    (ticker, last_updated, active)
                    VALUES (?,?,1)
                """, (ticker, now))
            conn.commit()
            log.info("Backup universe seeded: %d tickers", len(DEFAULT_BACKUP_UNIVERSE))
        conn.close()
    except Exception as exc:
        log.error("Backup universe init failed: %s", exc)


def get_backup_universe() -> list[str]:
    """
    Get current backup universe.
    Returns list of active tickers sorted by concordance rate.
    """
    init_backup_table()
    try:
        conn = sqlite3.connect(BACKUP_DB, timeout=5)
        rows = conn.execute("""
            SELECT ticker FROM backup_universe
            WHERE active=1
            ORDER BY concordance_rate DESC, avg_score DESC
        """).fetchall()
        conn.close()
        tickers = [r[0] for r in rows]
        return tickers if tickers else DEFAULT_BACKUP_UNIVERSE
    except Exception:
        return DEFAULT_BACKUP_UNIVERSE


def check_axiom_health() -> tuple[bool, str]:
    """
    Check if Axiom is healthy and able to screen.
    Returns (is_healthy, reason).
    """
    import requests
    try:
        r = requests.get(
            f"{AXIOM_URL}/health",
            headers={"X-Axiom-Secret": AXIOM_SECRET},
            timeout=5,
        )
        if r.status_code == 200:
            data = r.json()
            status = data.get("status","")
            if status in ("healthy","ok","HEALTHY"):
                return True, "Axiom healthy"
            return False, f"Axiom status: {status}"
        return False, f"Axiom HTTP {r.status_code}"
    except Exception as exc:
        return False, f"Axiom unreachable: {exc}"


def get_universe_with_fallback() -> tuple[list[str], bool, str]:
    """
    Get ticker universe — Axiom live or backup static.

    Returns:
        (tickers, is_fallback, source_description)
    """
    axiom_ok, reason = check_axiom_health()

    if axiom_ok:
        # Try to get live pool from Axiom
        import requests
        try:
            r = requests.get(
                f"{AXIOM_URL}/pool",
                headers={"X-Axiom-Secret": AXIOM_SECRET},
                timeout=8,
            )
            if r.status_code == 200:
                data = r.json()
                tickers = data.get("tickers", [])
                if tickers:
                    log.info("Axiom pool: %d tickers (live)", len(tickers))
                    return tickers, False, "axiom_live"
        except Exception as exc:
            log.warning("Axiom pool fetch failed: %s", exc)

    # Fallback to backup universe
    backup = get_backup_universe()
    log.warning(
        "AXIOM FALLBACK: using backup universe (%d tickers). Reason: %s",
        len(backup), reason
    )

    # Log the fallback event
    try:
        conn = sqlite3.connect(BACKUP_DB, timeout=5)
        conn.execute("""
            INSERT INTO axiom_status_log (status, reason, ts)
            VALUES (?,?,?)
        """, ("FALLBACK", reason, time.time()))
        conn.commit()
        conn.close()
    except Exception:
        pass

    return backup, True, f"backup_static ({reason})"


def update_universe_performance(
    ticker:           str,
    score:            float,
    formed_concordance: bool,
) -> None:
    """
    Update backup universe performance tracking.
    Called by learning loop after each scored window.
    """
    try:
        conn = sqlite3.connect(BACKUP_DB, timeout=5)
        conn.execute("""
            INSERT INTO backup_universe (ticker, last_updated, active)
            VALUES (?,?,1)
            ON CONFLICT(ticker) DO UPDATE SET
                times_scored    = times_scored + 1,
                avg_score       = (avg_score * times_scored + ?) / (times_scored + 1),
                concordance_rate = (concordance_rate * times_scored + ?) / (times_scored + 1),
                last_updated    = ?
        """, (
            ticker, date.today().isoformat(),
            score,
            1.0 if formed_concordance else 0.0,
            date.today().isoformat(),
        ))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.debug("Performance update failed for %s: %s", ticker, exc)


def refresh_backup_universe(top_n: int = 60) -> None:
    """
    Weekly refresh — promote best-performing tickers.
    Called by learning loop every Friday.
    Keeps tickers with highest concordance rate and average score.
    """
    try:
        conn = sqlite3.connect(BACKUP_DB, timeout=5)
        # Deactivate all
        conn.execute("UPDATE backup_universe SET active=0")
        # Activate top performers (min 5 times scored)
        conn.execute("""
            UPDATE backup_universe SET active=1
            WHERE ticker IN (
                SELECT ticker FROM backup_universe
                WHERE times_scored >= 5
                ORDER BY concordance_rate DESC, avg_score DESC
                LIMIT ?
            )
        """, (top_n,))
        # Always keep defaults for new tickers
        for ticker in DEFAULT_BACKUP_UNIVERSE:
            conn.execute("""
                INSERT OR IGNORE INTO backup_universe
                (ticker, last_updated, active)
                VALUES (?,?,1)
            """, (ticker, date.today().isoformat()))

        active = conn.execute(
            "SELECT COUNT(*) FROM backup_universe WHERE active=1"
        ).fetchone()[0]
        conn.commit()
        conn.close()
        log.info("Backup universe refreshed: %d active tickers", active)
    except Exception as exc:
        log.error("Backup universe refresh failed: %s", exc)


def get_axiom_fallback_stats() -> dict:
    """Get statistics on how often Axiom fallback is used."""
    try:
        conn = sqlite3.connect(BACKUP_DB, timeout=5)
        rows = conn.execute("""
            SELECT status, COUNT(*), MIN(ts), MAX(ts)
            FROM axiom_status_log
            WHERE ts > ?
            GROUP BY status
        """, (time.time() - 86400 * 7,)).fetchall()
        conn.close()
        return {
            "fallback_events_7d": sum(r[1] for r in rows if r[0]=="FALLBACK"),
            "details": [{"status":r[0],"count":r[1]} for r in rows],
        }
    except Exception:
        return {"fallback_events_7d": 0}
