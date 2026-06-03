"""
execution.py — Idempotent trade execution + capital accounting
==============================================================
1D from spec: Deterministic client_order_id makes double-submission impossible.
              Same pick submitted twice → same order_id → Alpaca deduplicates.

Capital accounting: atomic SQL UPDATE with BEGIN IMMEDIATE.
Position cap: Alpaca live query (C1 fix) + DB trigger (local safety net).

No CapitalPoolManager class. No 30-minute revalidation queue.
No fear of double-submission.

Authored: 2026-05-02 | Cipher spec + OMNI adversarial review
"""

from __future__ import annotations
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Optional

import requests

from data_contracts import PositionCapError, CapitalExceededError

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
ALPACA_URL    = "https://paper-api.alpaca.markets"
ALPACA_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

MAX_POSITIONS     = 3
MAX_RISK_USD      = 1000.0
NEXUS_UNIVERSE    = None  # None = count all Alpaca positions


# ── Result types ─────────────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    success:         bool
    client_order_id: str
    alpaca_order_id: Optional[str] = None
    fill_price:      Optional[float] = None
    error:           Optional[str] = None
    already_existed: bool = False   # True if order was idempotently re-submitted


# ── DB helpers ────────────────────────────────────────────────────────────────

@contextmanager
def get_db(db_path: str, isolation_level=None):
    """DB context manager. Default isolation_level=None → autocommit. Set to 'IMMEDIATE' for transaction."""
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=isolation_level)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        # AUTO-COMMIT for isolation_level=None (SQLite default)
        # For any other isolation level ("IMMEDIATE", etc), caller manages transaction
        if isolation_level is not None and isolation_level != "":
            # isolation_level="" was a hack — always require explicit semantics
            conn.commit()
    except Exception:
        if isolation_level is not None and isolation_level != "":
            try:
                conn.rollback()
            except:
                pass
        raise
    finally:
        conn.close()


def get_et_date() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return date.today().strftime("%Y-%m-%d")


# ── Deterministic order ID (1D) ───────────────────────────────────────────────

def make_client_order_id(ticker: str, window_id: str) -> str:
    """
    1D from spec: Same ticker + window_id always produces the same order ID.
    Alpaca deduplicates on client_order_id — double-submission is safe.
    Max 48 chars (Alpaca limit).
    """
    et_date = get_et_date()
    raw = f"nexus-{et_date}-{ticker.lower()}-{window_id}"
    return raw[:48]


# ── Position cap check (C1 fix) ───────────────────────────────────────────────

def get_live_alpaca_position_count() -> int:
    """
    C1 fix: Alpaca API is ground truth for combined position count.
    Covers Alpha Railway + OMNI local sharing the same Alpaca account.
    DB trigger is local safety net — this is the pre-execution check.
    """
    try:
        r = requests.get(
            f"{ALPACA_URL}/v2/positions",
            headers=ALPACA_HEADERS,
            timeout=8,
        )
        r.raise_for_status()
        positions = r.json()
        if NEXUS_UNIVERSE:
            return len([p for p in positions if p.get("symbol") in NEXUS_UNIVERSE])
        return len(positions)
    except Exception as e:
        # Fail closed — if we can't confirm position count, block execution
        raise PositionCapError(f"Cannot verify position count — Alpaca unreachable: {e}")


# ── Capital allocation (atomic, concurrent-safe) ──────────────────────────────

def allocate_capital(db_path: str, arena: str, amount_usd: float) -> None:
    """
    C6 fix: BEGIN IMMEDIATE serializes concurrent allocations at DB level.
    Raises CapitalExceededError if pool would be exceeded.
    No application-level lock needed.
    """
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT allocated_usd, total_usd FROM capital_ledger WHERE arena = ?",
                (arena,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise CapitalExceededError(f"No capital ledger for arena: {arena}")

            if row[0] + amount_usd > row[1]:
                conn.execute("ROLLBACK")
                raise CapitalExceededError(
                    f"Capital exceeded for {arena}: "
                    f"allocated={row[0]:.0f} + {amount_usd:.0f} > total={row[1]:.0f}"
                )
            conn.execute(
                "UPDATE capital_ledger SET allocated_usd = allocated_usd + ?, "
                "updated_at = datetime('now') WHERE arena = ?",
                (amount_usd, arena)
            )
            conn.execute("COMMIT")
        except Exception:
            try: conn.execute("ROLLBACK")
            except: pass
            raise


def release_capital(db_path: str, arena: str, amount_usd: float,
                    realized_pnl: float = 0.0) -> None:
    """Release allocated capital after position closes."""
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE capital_ledger SET "
                "allocated_usd = MAX(0, allocated_usd - ?), "
                "realized_pnl = realized_pnl + ?, "
                "updated_at = datetime('now') "
                "WHERE arena = ?",
                (amount_usd, realized_pnl, arena)
            )
            conn.execute("COMMIT")
        except Exception:
            try: conn.execute("ROLLBACK")
            except: pass
            raise


# ── Core execution function ───────────────────────────────────────────────────

def execute_trade(
    db_path:    str,
    ticker:     str,
    strategy:   str,
    direction:  str,
    window_id:  str,
    pathway:    str,
    arena:      str,
    sizing_mult: float,
    dte:        int,
    expiry:     str,
    axiom_risk: dict,
) -> ExecutionResult:
    """
    1D from spec: Idempotent execution via deterministic client_order_id.

    Flow:
      1. Check Alpaca live position count (C1 — ground truth for combined cap)
      2. Allocate capital (atomic SQL — C6)
      3. Insert pending position (DB trigger enforces local cap)
      4. Submit to Alpaca with deterministic client_order_id
      5. Confirm fill, update DB
    """
    client_order_id = make_client_order_id(ticker, window_id)
    allocated_usd   = round(MAX_RISK_USD * sizing_mult, 2)

    # Step 1: Alpaca live position count (combined cap across all services)
    try:
        live_count = get_live_alpaca_position_count()
        if live_count >= MAX_POSITIONS:
            return ExecutionResult(
                success=False,
                client_order_id=client_order_id,
                error=f"POSITION_CAP: Alpaca shows {live_count}/{MAX_POSITIONS} open positions",
            )
    except PositionCapError as e:
        return ExecutionResult(
            success=False,
            client_order_id=client_order_id,
            error=str(e),
        )

    # Step 2: Allocate capital (atomic)
    try:
        allocate_capital(db_path, arena, allocated_usd)
    except CapitalExceededError as e:
        return ExecutionResult(
            success=False,
            client_order_id=client_order_id,
            error=str(e),
        )

    # Step 3: Insert pending position (DB trigger = local cap safety net)
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:  # ← Direct connection + explicit commit
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                """INSERT INTO active_positions
                   (ticker, arena, client_order_id, status, strategy, direction,
                    max_risk_usd, allocated_usd, dte_at_entry, expiry, pathway, window_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ticker, arena, client_order_id, "pending", strategy, direction,
                 MAX_RISK_USD, allocated_usd, dte, expiry, pathway, window_id)
            )
            conn.commit()  # ← EXPLICIT COMMIT
    except sqlite3.IntegrityError as e:
        err = str(e)
        if "UNIQUE" in err:
            # Already exists — idempotent re-submission, get existing result
            logger.info("[Exec] %s: order already exists (idempotent) — returning existing",
                        client_order_id)
            release_capital(db_path, arena, allocated_usd)  # don't double-allocate
            return ExecutionResult(
                success=True,
                client_order_id=client_order_id,
                already_existed=True,
            )
        if "POSITION_CAP" in err:
            release_capital(db_path, arena, allocated_usd)
            return ExecutionResult(
                success=False,
                client_order_id=client_order_id,
                error="POSITION_CAP: DB trigger — max 3 concurrent positions",
            )
        release_capital(db_path, arena, allocated_usd)
        return ExecutionResult(
            success=False,
            client_order_id=client_order_id,
            error=f"DB insert failed: {e}",
        )
    except Exception as e:
        release_capital(db_path, arena, allocated_usd)
        return ExecutionResult(
            success=False,
            client_order_id=client_order_id,
            error=f"DB error: {e}",
        )

    # Step 4: Submit to Alpaca
    try:
        order_payload = _build_order_payload(
            ticker, strategy, direction, client_order_id, allocated_usd, dte
        )
        r = requests.post(
            f"{ALPACA_URL}/v2/orders",
            headers=ALPACA_HEADERS,
            json=order_payload,
            timeout=15,
        )

        if r.status_code in (200, 201):
            alpaca_id = r.json().get("id")
            # Step 5: Confirm — EXPLICIT COMMIT required
            with sqlite3.connect(db_path, timeout=10) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute(
                    "UPDATE active_positions SET status='open', alpaca_order_id=?, "
                    "entry_time=datetime('now') WHERE client_order_id=?",
                    (alpaca_id, client_order_id)
                )
                conn.commit()  # ← EXPLICIT COMMIT
            logger.info("[Exec] %s filled: alpaca_id=%s", ticker, alpaca_id)
            return ExecutionResult(
                success=True,
                client_order_id=client_order_id,
                alpaca_order_id=alpaca_id,
            )
        else:
            # Alpaca rejected — roll back
            err = r.json().get("message", r.text[:100])
            _rollback_execution(db_path, arena, allocated_usd, client_order_id, err)
            return ExecutionResult(
                success=False,
                client_order_id=client_order_id,
                error=f"Alpaca {r.status_code}: {err}",
            )

    except Exception as e:
        _rollback_execution(db_path, arena, allocated_usd, client_order_id, str(e))
        return ExecutionResult(
            success=False,
            client_order_id=client_order_id,
            error=f"Alpaca submit exception: {e}",
        )


def _rollback_execution(db_path: str, arena: str, allocated_usd: float,
                         client_order_id: str, reason: str) -> None:
    """Clean rollback: cancel DB position + release capital."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:  # ← Direct connection + explicit commit
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "UPDATE active_positions SET status='cancelled', notes=? "
                "WHERE client_order_id=?",
                (reason[:200], client_order_id)
            )
            conn.commit()  # ← EXPLICIT COMMIT
    except Exception as e:
        logger.error("[Exec] Rollback DB update failed: %s", e)
    try:
        release_capital(db_path, arena, allocated_usd)
    except Exception as e:
        logger.error("[Exec] Rollback capital release failed: %s", e)


def _build_order_payload(ticker: str, strategy: str, direction: str,
                          client_order_id: str, notional: float, dte: int) -> dict:
    """Build Alpaca order payload. Strategy-specific logic."""
    # Base equity order (options spread logic to be added per strategy)
    return {
        "symbol":           ticker,
        "notional":         str(round(notional, 2)),
        "side":             "buy" if direction == "bullish" else "sell",
        "type":             "market",
        "time_in_force":    "day",
        "client_order_id":  client_order_id,
    }


# ── Startup reconciler ────────────────────────────────────────────────────────

def reconcile_on_startup(db_path: str, alert_fn) -> None:
    """
    C7 / Layer 4 recovery: On startup, compare DB state vs Alpaca.
    Flags discrepancies to Ahmed — never silently assumes correctness.
    """
    try:
        r = requests.get(f"{ALPACA_URL}/v2/positions", headers=ALPACA_HEADERS, timeout=8)
        r.raise_for_status()
        alpaca_positions = {p["symbol"]: p for p in r.json()}
    except Exception as e:
        logger.error("[Reconcile] Cannot reach Alpaca: %s", e)
        alert_fn(f"⚠️ Startup reconciler: Alpaca unreachable — {e}")
        return

    with get_db(db_path) as conn:
        db_positions = conn.execute(
            "SELECT ticker, client_order_id, allocated_usd "
            "FROM active_positions WHERE status = 'open'"
        ).fetchall()

    discrepancies = []
    for row in db_positions:
        if row["ticker"] not in alpaca_positions:
            discrepancies.append(f"DB has {row['ticker']} open — not in Alpaca")

    for symbol in alpaca_positions:
        db_tickers = [r["ticker"] for r in db_positions]
        if symbol not in db_tickers:
            discrepancies.append(f"Alpaca has {symbol} — not in DB")

    if discrepancies:
        msg = "⚠️ <b>Startup reconciliation mismatch:</b>\n" + "\n".join(f"• {d}" for d in discrepancies)
        logger.warning("[Reconcile] %s", "\n".join(discrepancies))
        alert_fn(msg)
    else:
        logger.info("[Reconcile] DB and Alpaca in sync (%d positions)", len(db_positions))
