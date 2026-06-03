"""
sage_scorer.py — Deterministic Macro/Fundamental Scoring Engine
Nexus V1 | Sage Agent

Scores tickers using macro environment, sector positioning,
earnings proximity, and catalyst identification.
LLM reserved for genuine macro anomalies only.

SCORING FACTORS:
  Alpha (options):  VIX 30% | Sector 25% | Earnings 20% | Macro 15% | Catalyst 10%
  Prime (swing):    Catalyst 35% | Sector 30% | VIX 20% | Earnings 10% | Macro 5%

DATA SOURCES:
  Polygon : VIX level, SPY, Sector ETFs (XLK/XLF/XLE/XLV/XLY/XLI/XLB/XLC)
  FRED    : T10Y2Y yield curve, HY credit spreads
  ORATS   : Earnings dates, IV context
  Fallback: Neutral score (50) when API unavailable — never blocks
"""

import os
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger("sage.scorer")

# ── Configuration ──────────────────────────────────────────────────────────────

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "ZMEnZ_2GURw_UqbufU5npd49ZDeptMhl")
POLYGON_BASE    = "https://api.polygon.io"

ORATS_TOKEN     = os.getenv("ORATS_TOKEN", os.getenv("ORATS_API_KEY", ""))
ORATS_BASE      = "https://api.orats.io/datav2"

FRED_API_KEY    = os.getenv("FRED_API_KEY", "")
FRED_BASE       = "https://api.stlouisfed.org/fred"

# Submission floors
ALPHA_FLOOR = 45.0
PRIME_FLOOR = 45.0

# Alpha weights (options — macro context for strategy selection)
ALPHA_WEIGHTS = {
    "vix":      0.30,
    "sector":   0.25,
    "earnings": 0.20,
    "macro":    0.15,
    "catalyst": 0.10,
}

# Prime weights (swing — catalyst and sector momentum drive short moves)
PRIME_WEIGHTS = {
    "catalyst": 0.35,
    "sector":   0.30,
    "vix":      0.20,
    "earnings": 0.10,
    "macro":    0.05,
}

# Sector ETF map — ticker → sector ETF
SECTOR_MAP = {
    # Technology
    "AAPL":"XLK","MSFT":"XLK","NVDA":"XLK","GOOGL":"XLK","GOOG":"XLK",
    "META":"XLK","AMAT":"XLK","LRCX":"XLK","KEYS":"XLK","DELL":"XLK",
    # Financials
    "JPM":"XLF","GS":"XLF","MS":"XLF","AXP":"XLF","BAC":"XLF","WFC":"XLF",
    # Energy
    "XOM":"XLE","CVX":"XLE","COP":"XLE","VLO":"XLE","PSX":"XLE","MPC":"XLE",
    # Healthcare
    "UNH":"XLV","JNJ":"XLV","PFE":"XLV","ISRG":"XLV","HCA":"XLV","TMO":"XLV",
    # Consumer Discretionary
    "AMZN":"XLY","TSLA":"XLY","HD":"XLY","MCD":"XLY","BKNG":"XLY","RCL":"XLY",
    # Industrials
    "HON":"XLI","UPS":"XLI","GE":"XLI","ETN":"XLI","SAIC":"XLI",
    # Materials
    "LIN":"XLB","APD":"XLB","WDC":"XLB",
    # Communication Services
    "NFLX":"XLC","DIS":"XLC","T":"XLC","VZ":"XLC",
    # Real Estate
    "AMT":"XLRE","PLD":"XLRE","AVB":"XLRE",
    # Consumer Staples
    "PG":"XLP","KO":"XLP","WMT":"XLP","COST":"XLP",
    # Commodities / Special
    "GLD":"GLD","SLV":"SLV","TLT":"TLT","IWM":"IWM",
    # Default fallback → SPY
}

# VIX regime thresholds (from Sage's documented rules)
VIX_RISK_ON      = 18.0   # Full confidence directional
VIX_TRANSITIONAL = 25.0   # Reduce size, credit preferred
# VIX > 25 = Risk-Off, neutral/hedge only

# LLM triggers
LLM_TRIGGERS = [
    "vix_spike",        # VIX moved >20% in one day
    "yield_inversion",  # T10Y2Y deeply negative (< -0.5)
    "earnings_this_week", # Earnings within 5 days
    "sector_diverge",   # Sector ETF vs SPY spread > 5%
]


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class SageFactors:
    vix:      float = 0.0   # VIX regime score
    sector:   float = 0.0   # Sector ETF vs SPY momentum
    earnings: float = 0.0   # Earnings proximity (proximity = risk/opportunity)
    macro:    float = 0.0   # Yield curve + credit spread context
    catalyst: float = 0.0   # Catalyst identification proxy


@dataclass
class SageResult:
    ticker:       str
    direction:    str           = "bullish"
    alpha_score:  float         = 0.0
    prime_score:  float         = 0.0
    factors:      SageFactors   = field(default_factory=SageFactors)
    hard_block:   bool          = False
    block_reason: str           = ""
    llm_flag:     bool          = False
    llm_triggers: list          = field(default_factory=list)
    submit_alpha: bool          = False
    submit_prime: bool          = False
    reasoning:    str           = ""
    vix_level:    float         = 0.0
    sector_etf:   str           = "SPY"
    scored_at:    str           = field(default_factory=lambda: datetime.utcnow().isoformat())


# ── Data Fetching ──────────────────────────────────────────────────────────────

def _fetch_polygon_close(ticker: str, days: int = 10) -> list:
    """Fetch recent daily closes from Polygon."""
    end   = datetime.now()
    start = end - timedelta(days=days * 2)
    url   = (
        f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/"
        f"{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
        f"?adjusted=true&sort=asc&limit=50&apiKey={POLYGON_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            return r.json().get("results", [])
    except Exception as e:
        logger.debug("Polygon fetch %s: %s", ticker, e)
    return []


def _get_vix() -> Optional[float]:
    """Fetch latest VIX level from Polygon."""
    bars = _fetch_polygon_close("I:VIX", days=5)
    if bars:
        return bars[-1]["c"]
    # Fallback: try VIX ETF proxy
    bars = _fetch_polygon_close("VIXY", days=5)
    return bars[-1]["c"] if bars else None


def _get_vix_change_pct() -> Optional[float]:
    """VIX 1-day % change — for spike detection."""
    bars = _fetch_polygon_close("I:VIX", days=5)
    if len(bars) >= 2:
        return (bars[-1]["c"] - bars[-2]["c"]) / bars[-2]["c"]
    return None


def _get_sector_bars(etf: str, days: int = 25) -> list:
    """Fetch sector ETF bars."""
    return _fetch_polygon_close(etf, days=days)


def _get_fred_series(series_id: str) -> Optional[float]:
    """Fetch latest value of a FRED series."""
    if not FRED_API_KEY:
        return None
    url = (
        f"{FRED_BASE}/series/observations"
        f"?series_id={series_id}&api_key={FRED_API_KEY}"
        f"&file_type=json&limit=5&sort_order=desc"
    )
    try:
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            observations = r.json().get("observations", [])
            for obs in observations:
                if obs.get("value") and obs["value"] != ".":
                    return float(obs["value"])
    except Exception as e:
        logger.debug("FRED %s: %s", series_id, e)
    return None


def _get_earnings_dte(ticker: str) -> Optional[int]:
    """
    Fetch days-to-earnings from ORATS.
    Returns None if ORATS unavailable (fail-open).
    """
    if not ORATS_TOKEN:
        return None
    url = f"{ORATS_BASE}/earnings?token={ORATS_TOKEN}&ticker={ticker}"
    try:
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                next_earn = data[0].get("nextEarnDate")
                if next_earn:
                    earn_dt = datetime.strptime(next_earn, "%Y-%m-%d")
                    return (earn_dt - datetime.now()).days
    except Exception as e:
        logger.debug("ORATS earnings %s: %s", ticker, e)
    return None


# ── Factor Scoring ─────────────────────────────────────────────────────────────

def _score_vix(vix: Optional[float], direction: str) -> tuple:
    """
    VIX regime score (0–100) + LLM triggers.
    Based on Sage's documented thresholds:
      VIX < 18  = Risk-On  = full confidence directional
      VIX 18–25 = Transitional = reduce size
      VIX > 25  = Risk-Off = neutral/hedge only
    """
    triggers = []
    if vix is None:
        return 50.0, triggers   # Neutral fallback

    # Check for spike
    vix_chg = _get_vix_change_pct()
    if vix_chg and abs(vix_chg) > 0.20:
        triggers.append("vix_spike")

    if vix < 15:
        score = 95.0    # Very low vol — excellent for directional options
    elif vix < 18:
        score = 85.0    # Risk-On — full confidence
    elif vix < 22:
        score = 65.0    # Transitional — acceptable
    elif vix < 25:
        score = 45.0    # Transitional — reduce size
    elif vix < 30:
        score = 30.0    # Risk-Off — credit/neutral only
    elif vix < 35:
        score = 15.0    # High risk
    else:
        score = 5.0     # Extreme — hard block territory

    return round(score, 1), triggers


def _score_sector(ticker: str, spy_bars: list, direction: str) -> tuple:
    """
    Sector ETF vs SPY 20-day relative momentum (0–100).
    Bullish: sector outperforming SPY = good
    Bearish: sector underperforming SPY = good
    Returns (score, sector_etf, llm_triggers).
    """
    triggers = []
    etf = SECTOR_MAP.get(ticker.upper(), "SPY")

    if etf == "SPY":
        return 50.0, "SPY", triggers   # Unknown sector — neutral

    sector_bars = _get_sector_bars(etf, days=25)

    if len(sector_bars) < 6 or len(spy_bars) < 6:
        return 50.0, etf, triggers

    sector_ret = (sector_bars[-1]["c"] / sector_bars[-21]["c"] - 1) if len(sector_bars) >= 21 else \
                 (sector_bars[-1]["c"] / sector_bars[0]["c"] - 1)
    spy_ret    = (spy_bars[-1]["c"] / spy_bars[-21]["c"] - 1) if len(spy_bars) >= 21 else \
                 (spy_bars[-1]["c"] / spy_bars[0]["c"] - 1)

    diff = sector_ret - spy_ret

    if abs(diff) > 0.05:
        triggers.append("sector_diverge")

    if direction == "bullish":
        if   diff >=  0.05: score = 100.0
        elif diff >=  0.02: score = 80.0
        elif diff >=  0.00: score = 60.0
        elif diff >= -0.02: score = 35.0
        else:               score = 15.0
    else:
        if   diff <= -0.05: score = 100.0
        elif diff <= -0.02: score = 80.0
        elif diff <=  0.00: score = 60.0
        elif diff <=  0.02: score = 35.0
        else:               score = 15.0

    return round(score, 1), etf, triggers


def _score_earnings(ticker: str, direction: str) -> tuple:
    """
    Earnings proximity score (0–100).
    For Alpha: earnings are a risk — closer = lower score.
    For Prime: near-term earnings = potential catalyst = higher score.
    Returns (alpha_score, prime_score, llm_triggers).
    """
    triggers = []
    dte = _get_earnings_dte(ticker)

    if dte is None:
        return 70.0, 50.0, triggers   # Unknown — moderate score

    if dte < 0:
        return 80.0, 40.0, triggers   # Earnings just passed — IV crush risk for Alpha

    if dte <= 5:
        triggers.append("earnings_this_week")
        return 20.0, 90.0, triggers   # Imminent: Alpha risk, Prime opportunity

    if dte <= 14:
        return 40.0, 75.0, triggers

    if dte <= 30:
        return 65.0, 55.0, triggers

    if dte <= 60:
        return 85.0, 40.0, triggers

    return 90.0, 30.0, triggers       # Far out: safe for Alpha, no Prime catalyst


def _score_macro(direction: str) -> tuple:
    """
    Macro backdrop score (0–100) using FRED data.
    T10Y2Y yield curve + HY credit spreads.
    Returns (score, llm_triggers).
    """
    triggers = []
    score    = 50.0    # Neutral default

    t10y2y = _get_fred_series("T10Y2Y")   # 10Y minus 2Y spread
    hy_spread = _get_fred_series("BAMLH0A0HYM2")  # HY option-adjusted spread

    macro_score = 0.0
    weight_used = 0

    # Yield curve component (60%)
    if t10y2y is not None:
        if t10y2y <= -0.5:
            triggers.append("yield_inversion")
            yc_score = 20.0    # Deep inversion — macro stress
        elif t10y2y < 0:
            yc_score = 40.0    # Mild inversion
        elif t10y2y < 0.5:
            yc_score = 65.0    # Near flat — neutral
        elif t10y2y < 1.5:
            yc_score = 85.0    # Normal — healthy
        else:
            yc_score = 75.0    # Steep — growth expectations
        macro_score += yc_score * 0.6
        weight_used += 0.6

    # HY spread component (40%)
    if hy_spread is not None:
        if hy_spread < 3.0:
            hy_score = 95.0    # Tight — risk-on
        elif hy_spread < 4.0:
            hy_score = 80.0
        elif hy_spread < 5.0:
            hy_score = 60.0
        elif hy_spread < 7.0:
            hy_score = 35.0
        else:
            hy_score = 15.0    # Wide — stress
        macro_score += hy_score * 0.4
        weight_used += 0.4

    if weight_used > 0:
        score = macro_score / weight_used
    
    return round(score, 1), triggers


def _score_catalyst(ticker: str, direction: str, vix: Optional[float]) -> float:
    """
    Catalyst proxy score (0–100) for Prime swing trades.
    Uses volume momentum + price momentum as catalyst proxy.
    Real catalysts (news, earnings) are scored by other factors.
    """
    bars = _fetch_polygon_close(ticker, days=10)
    if len(bars) < 5:
        return 50.0

    closes  = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]

    # 5-day price change magnitude
    price_chg = abs(closes[-1] / closes[-5] - 1) if len(closes) >= 5 else 0

    # Volume trend (is volume increasing?)
    avg_vol_early = sum(volumes[:3]) / 3 if len(volumes) >= 3 else volumes[0]
    avg_vol_late  = sum(volumes[-2:]) / 2 if len(volumes) >= 2 else volumes[-1]
    vol_trend     = avg_vol_late / avg_vol_early if avg_vol_early > 0 else 1.0

    # Direction alignment
    price_direction = "bullish" if closes[-1] > closes[-3] else "bearish"
    direction_match = price_direction == direction

    score = 0.0

    # Price movement component (50%)
    if price_chg >= 0.08:   score += 50.0
    elif price_chg >= 0.05: score += 40.0
    elif price_chg >= 0.03: score += 28.0
    elif price_chg >= 0.01: score += 18.0
    else:                   score += 8.0

    # Volume trend component (30%)
    if vol_trend >= 1.5:    score += 30.0
    elif vol_trend >= 1.2:  score += 22.0
    elif vol_trend >= 1.0:  score += 15.0
    else:                   score += 5.0

    # Direction alignment (20%)
    if direction_match:     score += 20.0
    else:                   score += 5.0

    return round(min(100.0, score), 1)


# ── Direction Determination ────────────────────────────────────────────────────

def _determine_direction(ticker: str, vix: Optional[float]) -> str:
    """
    Sage determines direction from sector ETF trend vs SPY.
    Bullish if sector is outperforming and VIX is supportive.
    """
    etf   = SECTOR_MAP.get(ticker.upper(), "SPY")
    bars  = _get_sector_bars(etf, days=15)
    spy   = _fetch_polygon_close("SPY", days=15)

    if len(bars) < 5 or len(spy) < 5:
        return "bullish"    # Default

    sector_ret = bars[-1]["c"] / bars[-5]["c"] - 1
    spy_ret    = spy[-1]["c"] / spy[-5]["c"] - 1

    if sector_ret > spy_ret:
        return "bullish"
    return "bearish"


# ── Main Scoring Entry Point ───────────────────────────────────────────────────

def score_ticker(
    ticker: str,
    spy_bars: Optional[list] = None,
    vix: Optional[float] = None,
    macro_score_cache: Optional[tuple] = None,
) -> SageResult:
    """
    Score a single ticker for both Alpha and Prime.

    Args:
        ticker:             Stock symbol
        spy_bars:           Pre-fetched SPY bars (fetched if None)
        vix:                Pre-fetched VIX level (fetched if None)
        macro_score_cache:  Pre-computed (macro_score, triggers) to avoid repeat FRED calls

    Returns:
        SageResult with alpha_score, prime_score, submit flags, AILS data
    """
    result = SageResult(ticker=ticker)

    # ── Shared data ──
    if vix is None:
        vix = _get_vix()
    result.vix_level = vix or 0.0

    if spy_bars is None:
        spy_bars = _fetch_polygon_close("SPY", days=25)

    # ── Determine direction from sector + VIX ──
    result.direction = _determine_direction(ticker, vix)
    direction        = result.direction

    # ── Score all factors ──
    all_triggers = []
    factors      = SageFactors()

    # VIX regime
    factors.vix, vix_triggers = _score_vix(vix, direction)
    all_triggers.extend(vix_triggers)

    # Sector momentum
    factors.sector, etf, sector_triggers = _score_sector(ticker, spy_bars, direction)
    result.sector_etf = etf
    all_triggers.extend(sector_triggers)

    # Earnings proximity (returns alpha + prime scores separately)
    earn_alpha, earn_prime, earn_triggers = _score_earnings(ticker, direction)
    all_triggers.extend(earn_triggers)

    # Macro backdrop
    if macro_score_cache:
        macro_val, macro_triggers = macro_score_cache
    else:
        macro_val, macro_triggers = _score_macro(direction)
    factors.macro = macro_val
    all_triggers.extend(macro_triggers)

    # Catalyst proxy
    factors.catalyst = _score_catalyst(ticker, direction, vix)

    # ── Composite scores ──
    # Earnings contributes differently to Alpha vs Prime
    factors.earnings = earn_alpha  # Store Alpha earnings score in factors

    result.alpha_score = round(
        factors.vix      * ALPHA_WEIGHTS["vix"]      +
        factors.sector   * ALPHA_WEIGHTS["sector"]   +
        earn_alpha       * ALPHA_WEIGHTS["earnings"] +
        factors.macro    * ALPHA_WEIGHTS["macro"]    +
        factors.catalyst * ALPHA_WEIGHTS["catalyst"],
        1
    )

    result.prime_score = round(
        factors.catalyst * PRIME_WEIGHTS["catalyst"] +
        factors.sector   * PRIME_WEIGHTS["sector"]   +
        factors.vix      * PRIME_WEIGHTS["vix"]      +
        earn_prime       * PRIME_WEIGHTS["earnings"] +
        factors.macro    * PRIME_WEIGHTS["macro"],
        1
    )

    # ── LLM flag ──
    result.llm_triggers = list(set(all_triggers))
    result.llm_flag     = len(result.llm_triggers) > 0

    # ── Submission decisions ──
    result.submit_alpha = result.alpha_score >= ALPHA_FLOOR and not result.hard_block
    result.submit_prime = result.prime_score >= PRIME_FLOOR and not result.hard_block

    # ── Reasoning ──
    result.reasoning = (
        f"{ticker} [{direction.upper()}] "
        f"A={result.alpha_score:.1f} P={result.prime_score:.1f} | "
        f"VIX={factors.vix:.0f}({vix:.1f}) Sector={factors.sector:.0f}({etf}) "
        f"Earn={earn_alpha:.0f}/{earn_prime:.0f} Macro={factors.macro:.0f} "
        f"Cat={factors.catalyst:.0f}"
        + (f" | LLM:{','.join(result.llm_triggers)}" if result.llm_flag else "")
    )

    logger.info(result.reasoning)
    return result


def score_pool(tickers: list, window_id: str = "") -> list:
    """
    Score an entire Axiom pool.
    Fetches shared data (SPY, VIX, macro) once and reuses.
    """
    logger.info("[%s] Sage scoring pool of %d tickers", window_id or "manual", len(tickers))

    # Fetch shared data once
    spy_bars = _fetch_polygon_close("SPY", days=25)
    vix      = _get_vix()
    macro_cache = _score_macro("bullish")  # Macro is direction-independent

    if vix:
        logger.info("VIX: %.2f", vix)

    results = []
    for ticker in tickers:
        try:
            r = score_ticker(ticker, spy_bars=spy_bars, vix=vix,
                             macro_score_cache=macro_cache)
            results.append(r)
        except Exception as e:
            logger.error("Error scoring %s: %s", ticker, e)
            results.append(SageResult(
                ticker=ticker, hard_block=True,
                block_reason=str(e),
            ))
        time.sleep(0.1)

    results.sort(key=lambda r: r.alpha_score, reverse=True)

    qualifiers = [r for r in results if r.submit_alpha or r.submit_prime]
    logger.info(
        "[%s] Sage complete | %d/%d qualify | VIX=%.1f",
        window_id or "manual", len(qualifiers), len(tickers), vix or 0
    )
    return results


# ── Test Harness ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    test_pool = ["NVDA","AAPL","GOOGL","TSLA","AMAT",
                 "GLD","BKNG","AXP","ETN","VLO"]

    print("\n" + "=" * 70)
    print("SAGE SCORING ENGINE — TEST RUN")
    print(f"Pool: {test_pool}")
    print("=" * 70 + "\n")

    results = score_pool(test_pool, window_id="TEST-2026-05-19")

    print(f"\n{'TICKER':<8} {'DIR':<9} {'ALPHA':>6} {'PRIME':>6} {'A':>4} {'P':>4} {'SECTOR':<6} FLAGS")
    print("-" * 75)
    for r in results:
        flags = ",".join(r.llm_triggers) if r.llm_triggers else ""
        print(
            f"{r.ticker:<8} {r.direction:<9} "
            f"{r.alpha_score:>6.1f} {r.prime_score:>6.1f} "
            f"{'GO' if r.submit_alpha else '--':>4} "
            f"{'GO' if r.submit_prime else '--':>4} "
            f"{r.sector_etf:<6} {flags}"
        )

    qualifiers = [r for r in results if r.submit_alpha or r.submit_prime]
    print(f"\nQualifiers: {len(qualifiers)}/{len(test_pool)} | VIX: {results[0].vix_level:.1f}")
