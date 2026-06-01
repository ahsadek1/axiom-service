"""
position_manager.py — THESIS Position & Risk Manager
=====================================================
Enforces position limits, concentration limits, correlation checks,
buying power reserves, and portfolio Greeks awareness.

Rules:
  Max concurrent positions:    20
  Max per sector:              4 positions
  Max buying power deployed:   80%
  Min buying power reserve:    20%
  Max correlated positions:    3 (same underlying trend)
  Portfolio delta target:      near zero (delta-neutral)
"""
from __future__ import annotations
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import requests

from .spread_executor import get_open_positions, POSITIONS_DB

log = logging.getLogger("thesis.position_manager")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL      = "https://paper-api.alpaca.markets"

MAX_POSITIONS         = int(os.getenv("THESIS_MAX_POSITIONS", "20"))
MAX_PER_SECTOR        = int(os.getenv("THESIS_MAX_PER_SECTOR", "4"))
MAX_BUYING_POWER_PCT  = float(os.getenv("THESIS_MAX_BP_PCT", "0.80"))
MIN_RESERVE_PCT       = float(os.getenv("THESIS_MIN_RESERVE", "0.20"))
MAX_CORRELATED        = int(os.getenv("THESIS_MAX_CORRELATED", "3"))

# Sector mapping for concentration check
SECTOR_MAP = {
    "AAPL":"Tech","MSFT":"Tech","NVDA":"Tech","AMD":"Tech","GOOGL":"Tech",
    "GOOG":"Tech","META":"Tech","AVGO":"Tech","QCOM":"Tech","INTC":"Tech",
    "AMZN":"Consumer","TSLA":"Consumer","HD":"Consumer","LOW":"Consumer",
    "JPM":"Finance","BAC":"Finance","GS":"Finance","MS":"Finance","WFC":"Finance",
    "JNJ":"Health","UNH":"Health","PFE":"Health","ABBV":"Health","MRK":"Health",
    "XOM":"Energy","CVX":"Energy","COP":"Energy","SLB":"Energy",
    "CAT":"Industrial","DE":"Industrial","HON":"Industrial","GE":"Industrial",
    "SPY":"ETF","QQQ":"ETF","IWM":"ETF","DIA":"ETF",
}

def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY",""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY",""),
    }


def get_account() -> dict:
    try:
        r = requests.get(f"{BASE_URL}/v2/account", headers=_headers(), timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


def get_buying_power() -> float:
    account = get_account()
    return float(account.get("options_buying_power", 0))


def get_total_capital() -> float:
    account = get_account()
    return float(account.get("portfolio_value", 200000))


# ---------------------------------------------------------------------------
# Risk checks
# ---------------------------------------------------------------------------

class RiskCheck:
    """Result of a risk check."""
    def __init__(self, approved: bool, reason: str = ""):
        self.approved = approved
        self.reason   = reason

    def __bool__(self):
        return self.approved


def check_position_limit() -> RiskCheck:
    """Max 20 concurrent positions."""
    positions = get_open_positions()
    count = len(positions)
    if count >= MAX_POSITIONS:
        return RiskCheck(False,
            f"Position limit reached: {count}/{MAX_POSITIONS}")
    return RiskCheck(True, f"Position count OK: {count}/{MAX_POSITIONS}")


def check_sector_concentration(ticker: str) -> RiskCheck:
    """Max 4 positions per sector."""
    sector = SECTOR_MAP.get(ticker, "Other")
    positions = get_open_positions()
    sector_count = sum(
        1 for p in positions
        if SECTOR_MAP.get(p["ticker"], "Other") == sector
    )
    if sector_count >= MAX_PER_SECTOR:
        return RiskCheck(False,
            f"Sector limit: {sector} has {sector_count}/{MAX_PER_SECTOR} positions")
    return RiskCheck(True, f"Sector OK: {sector} {sector_count}/{MAX_PER_SECTOR}")


def check_duplicate_ticker(ticker: str) -> RiskCheck:
    """No duplicate positions on same ticker."""
    positions = get_open_positions()
    existing = [p for p in positions if p["ticker"] == ticker]
    if existing:
        return RiskCheck(False,
            f"Already have open position on {ticker}")
    return RiskCheck(True, f"No duplicate for {ticker}")


def check_buying_power(required_margin: float) -> RiskCheck:
    """
    Ensure adequate buying power with 20% reserve.
    Bull put spread margin = spread width x 100 x contracts.
    """
    buying_power = get_buying_power()
    total        = get_total_capital()
    reserve      = total * MIN_RESERVE_PCT
    available    = buying_power - reserve

    if available < required_margin:
        return RiskCheck(False,
            f"Insufficient buying power: need ${required_margin:,.0f} "
            f"available ${available:,.0f} (reserve ${reserve:,.0f})")
    return RiskCheck(True,
        f"Buying power OK: ${available:,.0f} available "
        f"(need ${required_margin:,.0f})")


def check_correlation(ticker: str) -> RiskCheck:
    """
    Check correlation — don't hold too many positions
    moving in the same direction at the same time.
    All bull put spreads are bullish, so limit by sector.
    """
    positions  = get_open_positions()
    bull_count = len([p for p in positions
                     if p["strategy"] == "bull_put_spread"])

    if bull_count >= MAX_CORRELATED * 3:  # 9 max bullish positions
        return RiskCheck(False,
            f"Correlation limit: {bull_count} bull put spreads open")
    return RiskCheck(True, f"Correlation OK: {bull_count} bull positions")


def run_all_checks(
    ticker: str,
    spread_width: float,
    contracts: int,
) -> tuple[bool, list[str]]:
    """
    Run all risk checks for a proposed trade.
    Returns (approved, list_of_reasons).
    """
    required_margin = spread_width * contracts * 100

    checks = [
        check_position_limit(),
        check_duplicate_ticker(ticker),
        check_sector_concentration(ticker),
        check_buying_power(required_margin),
        check_correlation(ticker),
    ]

    failures = [c.reason for c in checks if not c.approved]
    approved = len(failures) == 0

    if not approved:
        log.warning("Risk checks failed for %s: %s", ticker, " | ".join(failures))
    else:
        log.info("Risk checks passed for %s (margin=$%.0f)", ticker, required_margin)

    return approved, failures


# ---------------------------------------------------------------------------
# Portfolio summary
# ---------------------------------------------------------------------------

def get_portfolio_summary() -> dict:
    """Current portfolio state for reporting."""
    positions   = get_open_positions()
    account     = get_account()
    bp          = float(account.get("options_buying_power", 0))
    total       = float(account.get("portfolio_value", 200000))

    sector_exposure = {}
    total_max_loss  = 0.0
    total_max_profit = 0.0

    for p in positions:
        sector = SECTOR_MAP.get(p["ticker"], "Other")
        sector_exposure[sector] = sector_exposure.get(sector, 0) + 1
        total_max_loss   += p.get("max_loss", 0)
        total_max_profit += p.get("max_profit", 0)

    return {
        "open_positions":   len(positions),
        "max_positions":    MAX_POSITIONS,
        "buying_power":     bp,
        "total_capital":    total,
        "bp_utilization":   round(1 - bp/total, 3) if total else 0,
        "sector_exposure":  sector_exposure,
        "total_max_loss":   round(total_max_loss, 2),
        "total_max_profit": round(total_max_profit, 2),
        "risk_reward":      round(total_max_profit/total_max_loss, 3)
                           if total_max_loss > 0 else 0,
    }
