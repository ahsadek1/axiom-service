"""
tca_tracker.py — Transaction Cost Analysis Tracker
alpha-execution service (same module also used by prime-execution)

Measures slippage between theoretical price at OMNI GO decision time
and actual Alpaca fill price. Stored in tca_records table. Exposed via:
  - record_trade(...)     called immediately after fill confirmation
  - get_daily_summary()   called by EOD reporter
  - GET /tca              JSON endpoint (wired in main.py)

Alert threshold: avg daily slippage_pct > 15% → Telegram alert to Ahmed.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Alert threshold ────────────────────────────────────────────────────────────
SLIPPAGE_ALERT_THRESHOLD_PCT = 15.0   # fire Telegram if avg slippage > this

# ── Schema ────────────────────────────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tca_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            TEXT NOT NULL UNIQUE,
    ticker              TEXT NOT NULL,
    strategy            TEXT NOT NULL,
    theoretical_price   REAL,
    actual_fill_price   REAL,
    slippage_raw        REAL,
    slippage_pct        REAL,
    position_size_usd   REAL NOT NULL,
    slippage_dollar     REAL,
    decision_ts         TEXT NOT NULL,
    fill_ts             TEXT,
    fill_latency_ms     INTEGER,
    status              TEXT NOT NULL DEFAULT 'complete',
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
# status: 'complete' | 'incomplete_fill' | 'tca_unavailable'


def init_tca_schema(db_path: str) -> None:
    """
    Create tca_records table if it doesn't exist.
    Safe to call multiple times (idempotent).

    Args:
        db_path: Path to the service's SQLite database.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute(_CREATE_TABLE)
        conn.commit()
        logger.info("[TCA] Schema initialized in %s", db_path)
    finally:
        conn.close()


def record_trade(
    db_path:           str,
    trade_id:          str,
    ticker:            str,
    strategy:          str,
    position_size_usd: float,
    decision_ts:       str,
    theoretical_price: Optional[float] = None,
    actual_fill_price: Optional[float] = None,
    fill_ts:           Optional[str]   = None,
    bot_token:         Optional[str]   = None,
    chat_id:           Optional[str]   = None,
) -> dict:
    """
    Record a TCA entry for one executed trade.

    Determines status:
      - 'complete'         — both theoretical and fill prices available
      - 'incomplete_fill'  — fill price missing from Alpaca confirmation
      - 'tca_unavailable'  — theoretical price not captured at decision time

    Fires Telegram alert if |slippage_pct| > SLIPPAGE_ALERT_THRESHOLD_PCT.

    Args:
        db_path:            Path to service SQLite DB.
        trade_id:           Unique trade identifier (e.g. "alpha-20260411-NVDA").
        ticker:             Underlying ticker.
        strategy:           Human-readable strategy name (e.g. "Bull Put Spread").
        position_size_usd:  Dollar size of position.
        decision_ts:        ISO timestamp when OMNI GO verdict was issued.
        theoretical_price:  Mid-price at OMNI decision time. None = unavailable.
        actual_fill_price:  Alpaca confirmed fill price. None = fill confirmation missing.
        fill_ts:            ISO timestamp of fill confirmation.
        bot_token:          Telegram bot token for alerts. None = no alerts.
        chat_id:            Telegram chat id for alerts.

    Returns:
        The TCA record dict as stored.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Determine status + compute slippage
    if actual_fill_price is None:
        status = "incomplete_fill"
        slippage_raw = None
        slippage_pct = None
        slippage_dollar = None
        logger.warning("[TCA] trade_id=%s: fill price missing — marking INCOMPLETE", trade_id)
    elif theoretical_price is None:
        status = "tca_unavailable"
        slippage_raw = None
        slippage_pct = None
        slippage_dollar = None
        logger.warning("[TCA] trade_id=%s: theoretical price missing — marking TCA_UNAVAILABLE", trade_id)
    else:
        status = "complete"
        slippage_raw = actual_fill_price - theoretical_price
        if theoretical_price != 0:
            slippage_pct = (slippage_raw / theoretical_price) * 100.0
        else:
            slippage_pct = 0.0
        slippage_dollar = slippage_raw * position_size_usd  # approx: not contract-multiplier adjusted

    # Fill latency
    fill_latency_ms: Optional[int] = None
    if fill_ts and decision_ts:
        try:
            dt_decision = datetime.fromisoformat(decision_ts.replace("Z", "+00:00"))
            dt_fill     = datetime.fromisoformat(fill_ts.replace("Z", "+00:00"))
            fill_latency_ms = int((dt_fill - dt_decision).total_seconds() * 1000)
        except (ValueError, TypeError):
            pass

    record = {
        "trade_id":          trade_id,
        "ticker":            ticker,
        "strategy":          strategy,
        "theoretical_price": theoretical_price,
        "actual_fill_price": actual_fill_price,
        "slippage_raw":      slippage_raw,
        "slippage_pct":      round(slippage_pct, 4) if slippage_pct is not None else None,
        "position_size_usd": position_size_usd,
        "slippage_dollar":   round(slippage_dollar, 2) if slippage_dollar is not None else None,
        "decision_ts":       decision_ts,
        "fill_ts":           fill_ts,
        "fill_latency_ms":   fill_latency_ms,
        "status":            status,
        "created_at":        now,
    }

    # Persist
    try:
        _write_record(db_path, record)
    except Exception as e:
        # DB write failure → log to stderr but do NOT raise (never block execution)
        logger.error("[TCA] DB write failed for trade_id=%s: %s", trade_id, e)

    # Alert if slippage severe
    if (
        status == "complete"
        and slippage_pct is not None
        and abs(slippage_pct) > SLIPPAGE_ALERT_THRESHOLD_PCT
        and bot_token and chat_id
    ):
        _fire_slippage_alert(
            bot_token=bot_token, chat_id=chat_id,
            trade_id=trade_id, ticker=ticker,
            strategy=strategy, slippage_pct=slippage_pct,
            slippage_dollar=slippage_dollar,
        )

    logger.info(
        "[TCA] Recorded trade_id=%s status=%s slippage_pct=%s",
        trade_id, status,
        f"{slippage_pct:.2f}%" if slippage_pct is not None else "N/A",
    )
    return record


def get_daily_summary(db_path: str, date: Optional[str] = None) -> dict:
    """
    Compute TCA summary for a given date (default: today UTC).

    Returns:
        {
          date, trades_analyzed, avg_slippage_pct, total_slippage_cost_usd,
          worst_slippage_trade, best_execution_trade, 30d_avg_slippage_pct, alert
        }
    """
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row

        # Today's complete records
        today_rows = conn.execute(
            """SELECT ticker, strategy, slippage_pct, slippage_dollar
               FROM tca_records
               WHERE status='complete'
               AND DATE(created_at) = ?""",
            (date,)
        ).fetchall()

        # 30-day rolling average
        row_30d = conn.execute(
            """SELECT AVG(slippage_pct) as avg30
               FROM tca_records
               WHERE status='complete'
               AND DATE(created_at) >= DATE(?, '-30 days')""",
            (date,)
        ).fetchone()

        conn.close()
    except Exception as e:
        logger.error("[TCA] Daily summary DB read failed: %s", e)
        return {"date": date, "error": str(e)}

    if not today_rows:
        return {
            "date": date, "trades_analyzed": 0,
            "avg_slippage_pct": None, "total_slippage_cost_usd": None,
            "worst_slippage_trade": None, "best_execution_trade": None,
            "30d_avg_slippage_pct": round(row_30d["avg30"], 2) if row_30d and row_30d["avg30"] else None,
            "alert": None,
        }

    slippages    = [float(r["slippage_pct"]) for r in today_rows]
    costs        = [float(r["slippage_dollar"] or 0) for r in today_rows]
    avg_slip     = sum(slippages) / len(slippages)
    total_cost   = sum(costs)
    avg_30d      = round(row_30d["avg30"], 2) if row_30d and row_30d["avg30"] is not None else None

    # Worst (most negative slippage) and best (least negative)
    worst_idx = slippages.index(min(slippages))
    best_idx  = slippages.index(max(slippages))
    worst_str = f"{today_rows[worst_idx]['ticker']} {today_rows[worst_idx]['strategy']} ({min(slippages):.1f}%)"
    best_str  = f"{today_rows[best_idx]['ticker']} {today_rows[best_idx]['strategy']} ({max(slippages):.1f}%)"

    alert = None
    if abs(avg_slip) > SLIPPAGE_ALERT_THRESHOLD_PCT:
        alert = f"⚠️ Avg daily slippage {avg_slip:.1f}% exceeds {SLIPPAGE_ALERT_THRESHOLD_PCT}% threshold"

    return {
        "date":                   date,
        "trades_analyzed":        len(today_rows),
        "avg_slippage_pct":       round(avg_slip, 2),
        "total_slippage_cost_usd": round(total_cost, 2),
        "worst_slippage_trade":   worst_str,
        "best_execution_trade":   best_str,
        "30d_avg_slippage_pct":   avg_30d,
        "alert":                  alert,
    }


def get_recent_records(db_path: str, limit: int = 50) -> list:
    """
    Return the N most recent TCA records (all statuses).

    Args:
        db_path: Service SQLite DB path.
        limit:   Max records to return (default 50).

    Returns:
        List of record dicts, newest first.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM tca_records ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[TCA] get_recent_records failed: %s", e)
        return []


# ── Internal helpers ──────────────────────────────────────────────────────────

def _write_record(db_path: str, record: dict) -> None:
    """Upsert a TCA record (INSERT OR REPLACE by trade_id)."""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO tca_records
               (trade_id, ticker, strategy, theoretical_price, actual_fill_price,
                slippage_raw, slippage_pct, position_size_usd, slippage_dollar,
                decision_ts, fill_ts, fill_latency_ms, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record["trade_id"],    record["ticker"],          record["strategy"],
                record["theoretical_price"], record["actual_fill_price"],
                record["slippage_raw"],      record["slippage_pct"],
                record["position_size_usd"], record["slippage_dollar"],
                record["decision_ts"],       record["fill_ts"],
                record["fill_latency_ms"],   record["status"],
                record["created_at"],
            )
        )
        conn.commit()
    finally:
        conn.close()


def _fire_slippage_alert(
    bot_token: str, chat_id: str,
    trade_id: str, ticker: str, strategy: str,
    slippage_pct: float, slippage_dollar: Optional[float],
) -> None:
    """Send Telegram alert when slippage exceeds threshold."""
    cost_str = f"${slippage_dollar:,.2f}" if slippage_dollar is not None else "unknown"
    msg = (
        f"⚠️ HIGH SLIPPAGE ALERT\n"
        f"Trade: {ticker} {strategy}\n"
        f"Slippage: {slippage_pct:.1f}% | Cost: {cost_str}\n"
        f"Trade ID: {trade_id}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
        logger.info("[TCA] Slippage alert fired for trade_id=%s", trade_id)
    except Exception as e:
        logger.warning("[TCA] Alert send failed: %s", e)
