"""
db.py — Serialized writes, idempotency, and scan cycle tracking.
Spec: NEXUS_RESILIENCE_BASE v1.1 (Cipher, 2026-05-02)
DB Decision: Option A (Ahmed Sadek, 2026-05-02)

TABLE OWNERSHIP — NON-NEGOTIABLE:
  begin_immediate → execution tables ONLY:
      active_positions, capital_ledger, processed_picks, trade_log
  service_state.py (WAL + threading.Lock) → state/history tables:
      agent_state, daily_metrics, scan_windows, regime_history
  The rule: money movement → begin_immediate. State/history → service_state.py.

Do NOT call begin_immediate on service_state.py tables. Ever.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Generator


# ---------------------------------------------------------------------------
# begin_immediate — serialized write transaction
# ---------------------------------------------------------------------------

@contextmanager
def begin_immediate(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """
    Open a SQLite connection and begin a IMMEDIATE transaction.

    IMMEDIATE acquires a reserved lock at BEGIN time, preventing the
    concurrent read-then-write race (C6 from adversarial review). All
    writers queue; no partial reads from concurrent sessions.

    Use for ALL writes to execution tables:
        active_positions, capital_ledger, processed_picks, trade_log

    Do NOT use on tables owned by service_state.py.

    Args:
        db_path — absolute path to the SQLite database file.

    Yields:
        sqlite3.Connection with row_factory = sqlite3.Row

    Raises:
        Re-raises any exception after ROLLBACK. Caller sees the original error.

    Example:
        with begin_immediate(DB_PATH) as conn:
            existing = conn.execute(
                "SELECT qty FROM active_positions WHERE ticker=?", (ticker,)
            ).fetchone()
            if existing:
                conn.execute("UPDATE active_positions SET qty=? WHERE ticker=?",
                             (new_qty, ticker))
            else:
                conn.execute("INSERT INTO active_positions ...")
        # COMMIT happens here automatically
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# idempotent_insert — safe replay on restart
# ---------------------------------------------------------------------------

def idempotent_insert(
    db_path: str,
    table: str,
    unique_key: str,
    unique_val: str,
    row: dict,
) -> bool:
    """
    Insert *row* into *table* only if *unique_key* = *unique_val* doesn't exist.

    Safe to call multiple times on the same record (e.g. after crash/restart).
    Uses begin_immediate — safe for execution tables only.

    Args:
        db_path    — absolute path to the SQLite database file
        table      — table name (execution tables only)
        unique_key — column name that enforces uniqueness (must have UNIQUE constraint)
        unique_val — the unique value to check for
        row        — full row dict {column: value} to insert

    Returns:
        True  — row was inserted (new record)
        False — row already existed (safe replay, no-op)

    Raises:
        Any non-IntegrityError exception from the DB layer.

    Example:
        inserted = idempotent_insert(
            DB_PATH, "processed_picks",
            unique_key="pick_id", unique_val=pick_id,
            row={"pick_id": pick_id, "ticker": ticker, "processed_at": now}
        )
        if not inserted:
            logger.info("pick %s already processed — skipping", pick_id)
    """
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    try:
        with begin_immediate(db_path) as conn:
            conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                list(row.values()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


# ---------------------------------------------------------------------------
# ScanResult — universal scan cycle output
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    """
    Returned by every agent's scan function. Tracks verdicts, skips, and
    skip reasons for post-outage debugging.

    Usage:
        result = ScanResult()
        for ticker in pool:
            if vix_stale:
                result.skip(ticker, "vix_stale")
                continue
            verdict = score(ticker)
            result.verdicts.append(verdict)
        result.cycles += 1

        if result.check_skip_threshold(len(pool)):
            alert_ahmed("80%+ tickers skipped — data outage?")

    Fields:
        cycles         — number of full scan cycles completed this session
        verdicts       — list of outputs produced (agent-defined type)
        skip_reasons   — Counter of reason → count for diagnostics
        skipped_tickers — ticker → reason for per-ticker post-mortem
    """

    cycles: int = 0
    verdicts: list = field(default_factory=list)
    skip_reasons: dict = field(default_factory=lambda: defaultdict(int))
    skipped_tickers: dict = field(default_factory=dict)

    def skip(self, ticker: str, reason: str) -> None:
        """Record a skipped ticker with its reason."""
        self.skip_reasons[reason] += 1
        self.skipped_tickers[ticker] = reason

    @property
    def total_skips(self) -> int:
        """Total number of tickers skipped this cycle."""
        return sum(self.skip_reasons.values())

    def skip_rate(self, pool_size: int) -> float:
        """Fraction of the pool that was skipped. Returns 0.0 if pool is empty."""
        return self.total_skips / pool_size if pool_size else 0.0

    def check_skip_threshold(
        self, pool_size: int, threshold: float = 0.80
    ) -> bool:
        """
        Returns True if skip rate >= threshold.
        Caller should alert Ahmed — this typically signals a data source outage.

        Default threshold: 80% (configurable per agent).
        """
        return self.skip_rate(pool_size) >= threshold
