"""
tier2_filter.py — Axiom Tier 2 Live Pool Filter

Filters ~100 anchor stocks down to 15-20 live candidates.
Runs every 15 minutes during market hours (9:15 AM – 3:30 PM ET).

Criteria (all must pass):
  - VIX regime not CRISIS
  - IV rank between 20 and 80
  - RSI between 20 and 85
  - No earnings within 7 days
  - Axiom risk score ≤ 6
  - Max 4 stocks per GICS sector
  - Pool target: 15-20 stocks
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from data_sources import (
    days_to_earnings,
    polygon_get_snapshot,
    polygon_get_iv_rank,
    yfinance_get_data,
    yfinance_get_rsi,
)
from regime import Regime

logger = logging.getLogger("axiom.tier2")

# ── Tier 2 Thresholds ─────────────────────────────────────────────────────────
MIN_IV_RANK            = 5.0
MAX_IV_RANK            = 80.0
MIN_RSI                = 20.0
MAX_RSI                = 85.0
EARNINGS_BUFFER_DAYS   = 7
MAX_RISK_SCORE         = 6.0
MAX_PER_SECTOR         = 4
POOL_TARGET_MIN        = 10
POOL_TARGET_MAX        = 20
MAX_WORKERS            = 15

# GICS sector classification — covers full 250-ticker Axiom universe
# Updated April 12, 2026 to match expanded universe in config.py
SECTOR_MAP: dict[str, str] = {
    # ── Technology ────────────────────────────────────────────────────────────
    "AAPL": "Technology",  "MSFT": "Technology",  "NVDA": "Technology",
    "AVGO": "Technology",  "ORCL": "Technology",  "AMD":  "Technology",
    "QCOM": "Technology",  "TXN":  "Technology",  "AMAT": "Technology",
    "LRCX": "Technology",  "KLAC": "Technology",  "SNPS": "Technology",
    "CDNS": "Technology",  "PANW": "Technology",  "FTNT": "Technology",
    "CRWD": "Technology",  "ADBE": "Technology",  "CRM":  "Technology",
    "NOW":  "Technology",  "INTU": "Technology",  "IBM":  "Technology",
    "DELL": "Technology",  "HPQ":  "Technology",  "HPE":  "Technology",
    "WDC":  "Technology",  "STX":  "Technology",  "NTAP": "Technology",
    "KEYS": "Technology",  "AKAM": "Technology",  "FFIV": "Technology",
    "GDDY": "Technology",  "GRMN": "Technology",  "SMH":  "Technology",
    "SOXX": "Technology",
    # ── Communication Services ────────────────────────────────────────────────
    "META":  "Communication", "GOOGL": "Communication", "GOOG":  "Communication",
    "NFLX":  "Communication", "DIS":   "Communication", "CMCSA": "Communication",
    "T":     "Communication", "VZ":    "Communication", "TMUS":  "Communication",
    "WBD":   "Communication", "FOXA":  "Communication", "FOX":   "Communication",
    "EA":    "Communication", "EBAY":  "Communication",
    # ── Consumer Discretionary ────────────────────────────────────────────────
    "AMZN": "Consumer Disc", "TSLA": "Consumer Disc", "HD":   "Consumer Disc",
    "MCD":  "Consumer Disc", "SBUX": "Consumer Disc", "NKE":  "Consumer Disc",
    "TGT":  "Consumer Disc", "LOW":  "Consumer Disc", "TJX":  "Consumer Disc",
    "BKNG": "Consumer Disc", "ORLY": "Consumer Disc", "CMG":  "Consumer Disc",
    "ULTA": "Consumer Disc", "DRI":  "Consumer Disc", "YUM":  "Consumer Disc",
    "HLT":  "Consumer Disc", "MAR":  "Consumer Disc", "H":    "Consumer Disc",
    "RCL":  "Consumer Disc", "TOL":  "Consumer Disc", "NVR":  "Consumer Disc",
    "PHM":  "Consumer Disc", "DHI":  "Consumer Disc", "AZO":  "Consumer Disc",
    "IHG":  "Consumer Disc",
    # ── Consumer Staples ──────────────────────────────────────────────────────
    "WMT":  "Consumer Staples", "COST": "Consumer Staples", "PG":   "Consumer Staples",
    "KO":   "Consumer Staples", "PEP":  "Consumer Staples", "PM":   "Consumer Staples",
    "MO":   "Consumer Staples", "MDLZ": "Consumer Staples", "CL":   "Consumer Staples",
    "KMB":  "Consumer Staples", "GIS":  "Consumer Staples", "HSY":  "Consumer Staples",
    "MNST": "Consumer Staples", "TSN":  "Consumer Staples", "SFM":  "Consumer Staples",
    "KR":   "Consumer Staples", "BUD":  "Consumer Staples", "SYY":  "Consumer Staples",
    "USFD": "Consumer Staples",
    # ── Healthcare ────────────────────────────────────────────────────────────
    "UNH":  "Healthcare", "LLY":  "Healthcare", "JNJ":  "Healthcare",
    "ABBV": "Healthcare", "MRK":  "Healthcare", "PFE":  "Healthcare",
    "BMY":  "Healthcare", "AMGN": "Healthcare", "GILD": "Healthcare",
    "BIIB": "Healthcare", "VRTX": "Healthcare", "REGN": "Healthcare",
    "ISRG": "Healthcare", "TMO":  "Healthcare", "DHR":  "Healthcare",
    "BSX":  "Healthcare", "MDT":  "Healthcare", "EW":   "Healthcare",
    "HCA":  "Healthcare", "WELL": "Healthcare",
    "VTR":  "Healthcare", "NVO":  "Healthcare", "AZN":  "Healthcare",
    "LDOS": "Healthcare", "SAIC": "Healthcare",
    # ── Financials ────────────────────────────────────────────────────────────
    "JPM":  "Financials", "BAC":  "Financials", "WFC":  "Financials",
    "C":    "Financials", "GS":   "Financials", "MS":   "Financials",
    "BLK":  "Financials", "SCHW": "Financials", "AXP":  "Financials",
    "V":    "Financials", "MA":   "Financials", "SPGI": "Financials",
    "MCO":  "Financials", "ICE":  "Financials", "CME":  "Financials",
    "CBOE": "Financials", "PRU":  "Financials", "MET":  "Financials",
    "AFL":  "Financials", "ALL":  "Financials", "PGR":  "Financials",
    "TRV":  "Financials", "CB":   "Financials", "AIG":  "Financials",
    "AON":  "Financials", "ACGL": "Financials",
    "KKR":  "Financials", "APO":  "Financials", "BK":   "Financials",
    "STT":  "Financials", "MTB":  "Financials", "PNC":  "Financials",
    "NTRS": "Financials", "RJF":  "Financials", "IBKR": "Financials",
    "COF":  "Financials", "SYF":  "Financials", "ARES": "Financials",
    "CG":   "Financials",
    # ── Energy ────────────────────────────────────────────────────────────────
    "XOM":  "Energy", "CVX":  "Energy", "COP":  "Energy",
    "EOG":  "Energy", "SLB":  "Energy", "BKR":  "Energy",
    "MPC":  "Energy", "PSX":  "Energy", "VLO":  "Energy",
    "OXY":  "Energy", "KMI":  "Energy", "WMB":  "Energy",
    "OKE":  "Energy", "TRGP": "Energy", "ET":   "Energy",
    "EPD":  "Energy", "PAA":  "Energy", "FTI":  "Energy",
    "NEM":  "Energy", "WPM":  "Energy", "AEM":  "Energy",
    "GDX":  "Energy", "XLE":  "Energy",
    # ── Industrials ───────────────────────────────────────────────────────────
    "CAT":  "Industrials", "DE":   "Industrials", "HON":  "Industrials",
    "BA":   "Industrials", "GE":   "Industrials", "RTX":  "Industrials",
    "LMT":  "Industrials", "NOC":  "Industrials", "GD":   "Industrials",
    "HII":  "Industrials", "TDG":  "Industrials", "CARR": "Industrials",
    "OTIS": "Industrials", "ETN":  "Industrials", "EMR":  "Industrials",
    "ROK":  "Industrials", "PH":   "Industrials", "IR":   "Industrials",
    "AME":  "Industrials", "ITW":  "Industrials", "FAST": "Industrials",
    "GWW":  "Industrials", "MLM":  "Industrials", "VMC":  "Industrials",
    "HWM":  "Industrials", "ATI":  "Industrials", "TXT":  "Industrials",
    "LDOS": "Industrials", "SAIC": "Industrials", "CACI": "Industrials",
    "J":    "Industrials", "ODFL": "Industrials", "CSX":  "Industrials",
    "WM":   "Industrials", "RSG":  "Industrials", "CWST": "Industrials",
    "XLI":  "Industrials", "AIT":  "Industrials",
    # ── Materials ─────────────────────────────────────────────────────────────
    "LIN":  "Materials", "APD":  "Materials", "ECL":  "Materials",
    "DD":   "Materials", "DOW":  "Materials", "LYB":  "Materials",
    "SHW":  "Materials", "PPG":  "Materials", "FCX":  "Materials",
    "NUE":  "Materials", "STLD": "Materials", "RS":   "Materials",
    "CMC":  "Materials", "CCK":  "Materials", "PKG":  "Materials",
    "ATI":  "Materials", "EXP":  "Materials", "XLB":  "Materials",
    # ── Real Estate ───────────────────────────────────────────────────────────
    "AMT":  "Real Estate", "PLD":  "Real Estate", "CCI":  "Real Estate",
    "EQIX": "Real Estate", "PSA":  "Real Estate", "AVB":  "Real Estate",
    "EQR":  "Real Estate", "INVH": "Real Estate", "DLR":  "Real Estate",
    "VTR":  "Real Estate", "SPG":  "Real Estate", "O":    "Real Estate",
    "NNN":  "Real Estate", "VICI": "Real Estate", "KIM":  "Real Estate",
    "REG":  "Real Estate", "FRT":  "Real Estate", "IRM":  "Real Estate",
    "MAA":  "Real Estate", "UDR":  "Real Estate", "CPT":  "Real Estate",
    "EGP":  "Real Estate", "FR":   "Real Estate", "CBRE": "Real Estate",
    "JLL":  "Real Estate", "XLRE": "Real Estate",
    # ── Utilities ─────────────────────────────────────────────────────────────
    "NEE":  "Utilities", "DUK":  "Utilities", "SO":   "Utilities",
    "D":    "Utilities", "AEP":  "Utilities", "EXC":  "Utilities",
    "SRE":  "Utilities", "PCG":  "Utilities", "ES":   "Utilities",
    "XEL":  "Utilities", "WEC":  "Utilities", "DTE":  "Utilities",
    "ED":   "Utilities", "CMS":  "Utilities", "ETR":  "Utilities",
    "CNP":  "Utilities", "NI":   "Utilities", "AEE":  "Utilities",
    "LNT":  "Utilities", "EVRG": "Utilities", "PNW":  "Utilities",
    "FE":   "Utilities", "XLU":  "Utilities",
    # ── Broad ETFs (no sector cap applied) ───────────────────────────────────
    "SPY":  "ETF", "QQQ":  "ETF", "IWM":  "ETF", "DIA":  "ETF",
    "XLK":  "ETF", "XLF":  "ETF", "XLV":  "ETF", "XLE":  "ETF",
    "XLP":  "ETF", "XLY":  "ETF", "XLC":  "ETF", "XLB":  "ETF",
    "GLD":  "ETF", "SLV":  "ETF", "HYG":  "ETF", "IEF":  "ETF",
    "AGG":  "ETF", "TLT":  "ETF", "VEA":  "ETF", "EFA":  "ETF",
    "EEM":  "ETF", "IEMG": "ETF", "SMH":  "ETF", "SOXX": "ETF",
    "IBB":  "ETF", "GDX":  "ETF",
}


def run_tier2_filter(
    anchor_stocks: list[str],
    regime: Regime,
    polygon_api_key: str,
    alpha_vantage_key: str,
    fred_api_key: str,
) -> list[str]:
    """
    Run Tier 2 filter on anchor stocks to produce the live candidate pool.

    Args:
        anchor_stocks:     Tickers that passed Tier 1 filter.
        regime:            Current market regime (from VIX classification).
        polygon_api_key:   Polygon.io API key.
        alpha_vantage_key: Alpha Vantage API key.
        fred_api_key:      FRED API key (unused here — regime already classified).

    Returns:
        List of 15-20 tickers that passed all Tier 2 criteria.
        Returns previous pool if fewer than POOL_TARGET_MIN pass.
    """
    if regime.classification == "CRISIS":
        logger.warning("Tier 2 skipped — CRISIS regime (VIX %.1f)", regime.vix)
        return []

    logger.info("Tier 2 filter starting — evaluating %d anchor stocks", len(anchor_stocks))
    start_time = datetime.now(timezone.utc)

    # Evaluate all anchor stocks in parallel
    candidates: list[tuple[str, float]] = []  # (ticker, composite_score)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_ticker = {
            executor.submit(
                _evaluate_tier2,
                ticker,
                polygon_api_key,
                alpha_vantage_key,
            ): ticker
            for ticker in anchor_stocks
        }

        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                passed, score, reason = future.result(timeout=20)
                if passed:
                    candidates.append((ticker, score))
                else:
                    logger.debug("Tier 2 REJECT %s: %s", ticker, reason)
            except Exception as e:
                logger.warning("Tier 2 evaluation error for %s: %s", ticker, e)

    # Sort by composite score (higher = better candidate)
    candidates.sort(key=lambda x: x[1], reverse=True)

    # Apply sector cap — max 4 per sector
    pool = _apply_sector_cap(candidates, max_per_sector=MAX_PER_SECTOR)

    # Apply pool size cap
    pool = pool[:POOL_TARGET_MAX]

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    logger.info(
        "Tier 2 complete in %.1fs — %d in pool / %d candidates / %d anchor stocks",
        elapsed,
        len(pool),
        len(candidates),
        len(anchor_stocks),
    )

    if len(pool) < POOL_TARGET_MIN:
        logger.warning(
            "Tier 2 pool size %d below minimum %d — caller should use previous pool",
            len(pool),
            POOL_TARGET_MIN,
        )

    return pool


def _evaluate_tier2(
    ticker: str,
    polygon_api_key: str,
    alpha_vantage_key: str,
) -> tuple[bool, float, Optional[str]]:
    """
    Evaluate a single ticker against all Tier 2 criteria.

    Args:
        ticker:            Ticker symbol.
        polygon_api_key:   Polygon.io API key.
        alpha_vantage_key: Alpha Vantage API key.

    Returns:
        Tuple of (passed, composite_score, reject_reason).
        reject_reason is None if passed.
    """
    # Fetch price data (Polygon primary, yfinance fallback)
    data = polygon_get_snapshot(ticker, polygon_api_key)
    if data is None:
        data = yfinance_get_data(ticker)
    if data is None:
        return False, 0.0, "no_data"

    # IV rank
    iv_rank = polygon_get_iv_rank(ticker, polygon_api_key)
    if iv_rank is None:
        # IV rank unknown — allow with neutral score, OMNI handles deeper analysis
        iv_rank = 50.0

    if not (MIN_IV_RANK <= iv_rank <= MAX_IV_RANK):
        return False, 0.0, f"iv_rank={iv_rank:.1f}"

    # RSI
    rsi = yfinance_get_rsi(ticker)
    if rsi is not None and not (MIN_RSI <= rsi <= MAX_RSI):
        return False, 0.0, f"rsi={rsi:.1f}"

    # Earnings proximity
    dte = days_to_earnings(ticker, alpha_vantage_key)
    if dte is not None and dte <= EARNINGS_BUFFER_DAYS:
        return False, 0.0, f"earnings_in_{dte}_days"

    # Composite score for ranking (higher = better candidate)
    # IV rank in sweet spot (35-65) scores highest
    iv_score  = _iv_rank_score(iv_rank)
    rsi_score = _rsi_score(rsi) if rsi is not None else 50.0
    composite = (iv_score * 0.6) + (rsi_score * 0.4)

    return True, composite, None


def _iv_rank_score(iv_rank: float) -> float:
    """
    Score IV rank for Tier 2 ranking. Sweet spot is 35-65.

    Args:
        iv_rank: IV rank value 0-100.

    Returns:
        Score 0-100 (higher = better candidate).
    """
    if 35 <= iv_rank <= 65:
        return 100.0
    elif 25 <= iv_rank < 35 or 65 < iv_rank <= 75:
        return 75.0
    elif 20 <= iv_rank < 25 or 75 < iv_rank <= 80:
        return 50.0
    return 25.0


def _rsi_score(rsi: float) -> float:
    """
    Score RSI for Tier 2 ranking. Neutral RSI (40-60) scores highest.

    Args:
        rsi: RSI value 0-100.

    Returns:
        Score 0-100 (higher = better candidate).
    """
    if 40 <= rsi <= 60:
        return 100.0
    elif 30 <= rsi < 40 or 60 < rsi <= 70:
        return 75.0
    elif 20 <= rsi < 30 or 70 < rsi <= 85:
        return 50.0
    return 25.0


def _apply_sector_cap(
    candidates: list[tuple[str, float]],
    max_per_sector: int,
) -> list[str]:
    """
    Apply sector concentration cap to candidate list.

    Keeps up to max_per_sector stocks per GICS sector.
    Preserves ranking order (best candidates preferred within each sector).

    Args:
        candidates:      List of (ticker, score) tuples, sorted by score descending.
        max_per_sector:  Maximum stocks allowed per sector.

    Returns:
        List of ticker symbols after applying sector cap.
    """
    sector_counts: dict[str, int] = {}
    pool: list[str] = []

    for ticker, _ in candidates:
        sector = SECTOR_MAP.get(ticker, "Other")

        # ETFs are exempt from sector cap — they provide diversification by design
        if sector == "ETF":
            pool.append(ticker)
            continue

        count = sector_counts.get(sector, 0)
        if count < max_per_sector:
            pool.append(ticker)
            sector_counts[sector] = count + 1

    return pool
