"""
preflight.py — 9:25 AM hard gate before market open
=====================================================
3C from spec: Market does not open until this passes.
C5 fix: On failure, suspend with wait-retry — not SystemExit.

Authored: 2026-05-02 | Cipher spec + OMNI adversarial review
"""

from __future__ import annotations
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from typing import Optional

from data_contracts import NoVolatilityDataError, NoTechnicalDataError
from data_fetchers import get_volatility, get_technicals

logger = logging.getLogger(__name__)

PREFLIGHT_TICKERS = ["AAPL", "JPM", "NVDA"]


@dataclass
class PreflightResult:
    passed:      bool
    failures:    list = field(default_factory=list)
    warnings:    list = field(default_factory=list)
    run_at:      str  = ""
    duration_ms: int  = 0


def run_preflight(db_path: str) -> PreflightResult:
    """
    Hard gate. Checks that Oracle returns REAL data for 3 liquid tickers.
    Failures = pipeline does not open.
    Warnings = pipeline opens but Ahmed is notified.
    """
    import time
    start   = time.monotonic()
    now_str = datetime.now(timezone.utc).isoformat()
    failures = []
    warnings = []

    for ticker in PREFLIGHT_TICKERS:
        # Volatility check — iv_rank must be real
        try:
            vol = get_volatility(ticker)
            if vol.is_fallback:
                warnings.append(f"{ticker}: IVR on Polygon fallback (ORATS unavailable)")
            else:
                logger.info("Preflight %s: IVR=%.1f ✅", ticker, vol.iv_rank)
        except NoVolatilityDataError:
            failures.append(f"{ticker}: IVR unavailable — both ORATS and Polygon failed")
        except Exception as e:
            failures.append(f"{ticker}: volatility fetch error — {e}")

        # Technical check — RSI must be real
        try:
            tech = get_technicals(ticker)
            logger.info("Preflight %s: RSI=%.1f ✅", ticker, tech.rsi_14)
        except NoTechnicalDataError:
            failures.append(f"{ticker}: RSI unavailable — Polygon bars failed")
        except Exception as e:
            failures.append(f"{ticker}: technicals fetch error — {e}")

    duration_ms = int((time.monotonic() - start) * 1000)
    passed      = len(failures) == 0

    # Persist result
    try:
        et_date = datetime.now(__import__("zoneinfo").ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        et_date = date.today().strftime("%Y-%m-%d")

    try:
        import json
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO preflight_results "
                "(trading_date, passed, failures, run_at, duration_ms) VALUES (?,?,?,?,?)",
                (et_date, int(passed), json.dumps(failures), now_str, duration_ms)
            )
    except Exception as e:
        logger.warning("Preflight DB persist failed: %s", e)

    result = PreflightResult(
        passed      = passed,
        failures    = failures,
        warnings    = warnings,
        run_at      = now_str,
        duration_ms = duration_ms,
    )

    if passed:
        logger.info("Preflight PASSED (%dms) — pipeline cleared for trading", duration_ms)
    else:
        logger.critical("Preflight FAILED: %s", "; ".join(failures))

    return result
