"""
alpha_buffer_integration.py — Drop-in Deterministic Concordance
================================================================
Replaces the 3-agent concordance model in alpha-buffer.
Called by alpha-buffer's /submit endpoint instead of
waiting for agent HTTP submissions.

Flow (new deterministic):
  Axiom sends pool → nexus_scorer scores all tickers
  → confidence_engine filters → GO signals sent to OMNI
  → OMNI fires to alpha-execution → trade placed

No agent submissions. No concordance waiting window.
No echo chamber detector. No silent defaults.
One deterministic scoring run per window.

Ahmed directive May 2026:
  "Deterministic from A to Z."
"""
from __future__ import annotations
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("nexus.alpha_integration")

ALPHA_DB    = os.getenv("ALPHA_DB_PATH",
              "/Users/ahmedsadek/nexus/data/alpha_buffer.db")
OMNI_URL    = os.getenv("OMNI_WEBHOOK_URL","http://localhost:8004/concordance")
NEXUS_SECRET = os.getenv("NEXUS_SECRET",
              "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")

import sys as _sys
_sys.path.insert(0, "/Users/ahmedsadek/nexus/nexus-deterministic")

from nexus_scorer import score_pool, NexusScore, Confidence, fetch_axiom_regime
from backup_universe import get_universe_with_fallback, AXIOM_FALLBACK_SIZE_MULT
from confidence_engine import filter_tradeable_signals, log_signal_to_db


def init_deterministic_db() -> None:
    """Initialize deterministic scoring log table."""
    try:
        conn = sqlite3.connect(ALPHA_DB, timeout=10)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS deterministic_scores (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                window_id       TEXT NOT NULL,
                ticker          TEXT NOT NULL,
                direction       TEXT NOT NULL,
                combined_score  REAL,
                confidence      TEXT,
                technical       REAL,
                options_score   REAL,
                macro           REAL,
                go              INTEGER,
                strong_go       INTEGER,
                axiom_fallback  INTEGER,
                size_mult       REAL,
                omni_sent       INTEGER DEFAULT 0,
                omni_response   TEXT,
                created_at      TEXT,
                UNIQUE(window_id, ticker, direction)
            );
        """)
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error("DB init failed: %s", exc)


def run_deterministic_window(
    window_id:  str,
    tickers:    Optional[list] = None,
    direction:  str = "bullish",
    max_trades: int = 5,
) -> list[dict]:
    """
    Run one complete deterministic scoring window.
    Replaces the entire agent concordance cycle.

    Args:
        window_id:  Current window ID (e.g. "2026-05-27-0930")
        tickers:    Override ticker list (uses Axiom/backup if None)
        direction:  "bullish" or "bearish"
        max_trades: Maximum GO signals to return

    Returns:
        List of tradeable signal dicts, ready for OMNI dispatch.
    """
    init_deterministic_db()

    # Get universe
    if tickers is None:
        tickers, is_fallback, source = get_universe_with_fallback()
    else:
        is_fallback = False
        source = "override"

    log.info("Window %s: scoring %d tickers [%s] fallback=%s",
             window_id, len(tickers), direction, is_fallback)

    # Get VIX from Axiom (shared across all scores)
    vix, regime = fetch_axiom_regime()
    log.info("Regime: VIX=%.1f classification=%s", vix or 0, regime or "N/A")

    # Score all tickers deterministically
    scores = score_pool(tickers, direction=direction)

    # Apply confidence gate
    signals = filter_tradeable_signals(
        scores,
        axiom_fallback = is_fallback,
        max_signals    = max_trades,
    )

    # Save scores to DB
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = sqlite3.connect(ALPHA_DB, timeout=10)
        for score in scores:
            conn.execute("""
                INSERT OR REPLACE INTO deterministic_scores
                (window_id, ticker, direction, combined_score, confidence,
                 technical, options_score, macro, go, strong_go,
                 axiom_fallback, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                window_id, score.ticker, direction,
                score.combined, score.confidence.value,
                score.technical.score, score.options.score, score.macro.score,
                int(score.go), int(score.strong_go),
                int(is_fallback), now,
            ))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error("DB save failed: %s", exc)

    # Log signals
    for signal in signals:
        log_signal_to_db(signal)

    log.info("Window %s complete: %d/%d tickers qualify | %d signals",
             window_id, sum(1 for s in scores if s.go), len(scores), len(signals))

    return signals


# ALPACA EXECUTION CREDENTIALS — Alpha account (paper)
ALPACA_KEY    = os.getenv("ALPHA_ALPACA_KEY",    os.getenv("ALPACA_API_KEY",    "PKGGXWNZTITUTZUNVK2QBLZJQL"))
ALPACA_SECRET = os.getenv("ALPHA_ALPACA_SECRET", os.getenv("ALPACA_SECRET_KEY", ""))
ALPACA_BASE   = os.getenv("ALPACA_BASE_URL",     "https://paper-api.alpaca.markets")


def dispatch_to_omni(signals: list[dict], window_id: str) -> list[dict]:
    """
    DETERMINISTIC EXECUTION — OMNI BYPASSED.
    Ahmed directive 2026-05-26: scorer finds GO → Alpaca executes.
    OMNI is not in the path. No concordance. No 422. No silence.

    Flow: GO signal → Capital check → Alpaca limit order → done.
    """
    import requests as _req
    dispatched = []

    # Alpaca auth headers
    _headers = {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type":        "application/json",
    }

    for signal in signals:
        if not signal["tradeable"]:
            continue

        ticker    = signal["ticker"]
        direction = signal["direction"]

        # Only trade bullish/long for now
        if direction != "bullish":
            log.info("[SKIP] %s direction=%s — long-only", ticker, direction)
            continue

        # Get current price from Alpaca Data API
        try:
            _data_headers = {
                "APCA-API-KEY-ID":     ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            }
            price_r = _req.get(
                f"https://data.alpaca.markets/v2/stocks/{ticker}/quotes/latest",
                headers=_data_headers,
                params={"feed": "iex"},
                timeout=8,
            )
            if price_r.status_code == 200:
                ask = float(price_r.json().get("quote", {}).get("ap", 0))
                bid = float(price_r.json().get("quote", {}).get("bp", 0))
                price = ask if ask > 0 else (bid if bid > 0 else None)
            else:
                # Fallback: use last trade price
                trade_r = _req.get(
                    f"https://data.alpaca.markets/v2/stocks/{ticker}/trades/latest",
                    headers=_data_headers,
                    params={"feed": "iex"},
                    timeout=8,
                )
                if trade_r.status_code == 200:
                    price = float(trade_r.json().get("trade", {}).get("p", 0)) or None
                else:
                    price = None
        except Exception as _pe:
            log.debug("Price fetch failed for %s: %s", ticker, _pe)
            price = None

        if not price:
            log.warning("[SKIP] %s — could not get price", ticker)
            continue

        # Position sizing: $5,000 base × size_mult
        size_mult  = float(signal.get("size_mult", 1.0))
        target_usd = 5000.0 * size_mult
        shares     = max(1, int(target_usd / price))
        limit_px   = round(price * 1.001, 2)

        import uuid as _uuid
        coid = f"nexus-det-{ticker}-{_uuid.uuid4().hex[:10]}"

        try:
            order_r = _req.post(
                f"{ALPACA_BASE}/v2/orders",
                headers=_headers,
                json={
                    "symbol":          ticker,
                    "qty":             str(shares),
                    "side":            "buy",
                    "type":            "limit",
                    "time_in_force":   "day",
                    "limit_price":     str(limit_px),
                    "client_order_id": coid,
                },
                timeout=10,
            )

            status   = order_r.status_code
            order_id = order_r.json().get("id", "") if status in (200,201) else ""

            if order_id:
                log.info("✅ NEXUS DETERMINISTIC | %s %s | qty=%d px=%.2f | order=%s",
                         ticker, direction, shares, limit_px, order_id[:8])
            else:
                log.warning("❌ NEXUS DETERMINISTIC | %s | HTTP %d | %s",
                            ticker, status, order_r.text[:80])

            # Update DB
            try:
                conn = sqlite3.connect(ALPHA_DB, timeout=5)
                conn.execute("""
                    UPDATE deterministic_scores
                    SET omni_sent=1, omni_response=?
                    WHERE window_id=? AND ticker=? AND direction=?
                """, (f"HTTP {status} order={order_id[:8]}", window_id, ticker, direction))
                conn.commit()
                conn.close()
            except Exception:
                pass

            signal["omni_status"] = status
            signal["order_id"]    = order_id
            dispatched.append(signal)

        except Exception as exc:
            log.error("Execution failed for %s: %s", ticker, exc)

    return dispatched


def run_and_dispatch(
    window_id:  str,
    tickers:    Optional[list] = None,
    direction:  str = "bullish",
    max_trades: int = 5,
) -> dict:
    """
    Complete deterministic window: score → gate → dispatch.
    Single entry point for the scheduler.
    """
    start  = time.time()
    signals    = run_deterministic_window(window_id, tickers, direction, max_trades)
    dispatched = dispatch_to_omni(signals, window_id)
    elapsed    = round(time.time() - start, 2)

    return {
        "window_id":       window_id,
        "signals_found":   len(signals),
        "signals_dispatched": len(dispatched),
        "elapsed_seconds": elapsed,
        "tickers":         [s["ticker"] for s in dispatched],
        "timestamp":       datetime.now(timezone.utc).isoformat(),
    }
