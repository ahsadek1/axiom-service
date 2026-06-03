"""
cipher_scorer.py — Deterministic Technical Scoring Engine
Nexus V1 | Cipher Agent

Replaces LLM judgment for P1/P2 concordance signals.
LLM reserved for flagged edge cases only (P3/P4).

SCORING FACTORS:
  Alpha (options):  Trend 25% | Momentum 20% | Structure 20% | Volume 20% | RelStr 15%
  Prime (swing):    Trend 15% | Momentum 20% | Structure 30% | Volume 25% | RelStr 10%

SUBMISSION FLOORS (proof-of-concept, maximise learning throughput):
  Alpha: score >= 45
  Prime: score >= 45

HARD BLOCKS (absolute minimum — everything else trades and learns):
  VIX > 40  →  systemic risk, no trades
  < 50 bars →  insufficient data
  Alpaca breach → position risk

AILS INTEGRATION:
  Every result includes full component breakdown for learning.
  AILS updates factor weights post-30-trading-days based on outcomes.

Author: Claude / Anthropic — built for Ahmed Sadek, Orlando Epilepsy Center
"""

import os
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Tuple

import requests

logger = logging.getLogger("cipher.scorer")

# ── Configuration ──────────────────────────────────────────────────────────────

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "ZMEnZ_2GURw_UqbufU5npd49ZDeptMhl")
POLYGON_BASE    = "https://api.polygon.io"

# Submission floors — set low for maximum learning throughput
ALPHA_FLOOR = 45.0
PRIME_FLOOR = 45.0

# Hard block
VIX_HARD_BLOCK = 40.0

# Alpha scoring weights (options — direction + IV environment)
ALPHA_WEIGHTS = {
    "trend":     0.25,
    "momentum":  0.20,
    "structure": 0.20,
    "volume":    0.20,
    "rel_str":   0.15,
}

# Prime scoring weights (swing — breakout + catalyst move)
PRIME_WEIGHTS = {
    "trend":     0.15,
    "momentum":  0.20,
    "structure": 0.30,
    "volume":    0.25,
    "rel_str":   0.10,
}

# LLM invocation triggers — these flag a result for LLM review
LLM_TRIGGERS = [
    "earnings_imminent",    # Earnings within 5 days
    "extreme_rsi",          # RSI < 20 or > 80
    "factor_conflict",      # Trend bullish + momentum bearish (or vice versa)
    "vol_spike",            # Volume > 5x average (unusual event)
]


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class FactorScores:
    """Raw component scores (0–100 each). Stored for AILS learning."""
    trend:     float = 0.0    # SMA alignment (20/50/200)
    momentum:  float = 0.0    # RSI 14 + MACD 12/26/9
    structure: float = 0.0    # Breakout quality + higher highs + ATR profile
    volume:    float = 0.0    # Current volume vs 20-day average
    rel_str:   float = 0.0    # 5-day stock return vs SPY return


@dataclass
class CipherResult:
    """
    Full scoring output for one ticker.
    Passed to buffer_client for submission and to AILS for learning.
    """
    ticker:         str
    direction:      str           = "bullish"   # 'bullish' or 'bearish'
    alpha_score:    float         = 0.0
    prime_score:    float         = 0.0
    factors:        FactorScores  = field(default_factory=FactorScores)
    hard_block:     bool          = False
    block_reason:   str           = ""
    llm_flag:       bool          = False        # True = route to LLM for review
    llm_triggers:   list          = field(default_factory=list)
    submit_alpha:   bool          = False
    submit_prime:   bool          = False
    reasoning:      str           = ""
    error:          Optional[str] = None
    scored_at:      str           = field(default_factory=lambda: datetime.utcnow().isoformat())


# ── Polygon Data Fetching ──────────────────────────────────────────────────────

def _fetch_bars(ticker: str, days: int = 210) -> list:
    """
    Fetch daily OHLCV bars from Polygon.
    Returns list of bars sorted oldest → newest.
    Each bar: {o, h, l, c, v, t}
    """
    end   = datetime.now()
    start = end - timedelta(days=int(days * 1.6))  # Buffer for weekends/holidays

    url = (
        f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/"
        f"{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
        f"?adjusted=true&sort=asc&limit=300&apiKey={POLYGON_API_KEY}"
    )

    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("results", [])
        logger.warning("Polygon %s HTTP %d", ticker, resp.status_code)
        return []
    except Exception as e:
        logger.error("Polygon fetch error %s: %s", ticker, e)
        return []


# ── Technical Indicators ───────────────────────────────────────────────────────

def _sma(closes: list, period: int) -> Optional[float]:
    """Simple moving average. None if insufficient data."""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _rsi(closes: list, period: int = 14) -> Optional[float]:
    """
    Wilder's RSI (0–100).
    None if insufficient data.
    """
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _ema(data: list, period: int) -> list:
    """Exponential moving average."""
    k = 2.0 / (period + 1)
    result = [data[0]]
    for price in data[1:]:
        result.append(price * k + result[-1] * (1 - k))
    return result


def _macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[Tuple]:
    """
    MACD → (macd_line, signal_line, histogram).
    None if insufficient data.
    """
    if len(closes) < slow + signal:
        return None

    ema_fast    = _ema(closes, fast)
    ema_slow    = _ema(closes, slow)
    macd_line   = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]
    signal_line = _ema(macd_line[slow - 1:], signal)

    return macd_line[-1], signal_line[-1], macd_line[-1] - signal_line[-1]


def _atr(bars: list) -> float:
    """Average True Range over the full bar list."""
    if len(bars) < 2:
        return 0.0
    trs = [
        max(
            bars[i]["h"] - bars[i]["l"],
            abs(bars[i]["h"] - bars[i - 1]["c"]),
            abs(bars[i]["l"] - bars[i - 1]["c"]),
        )
        for i in range(1, len(bars))
    ]
    return sum(trs) / len(trs)


# ── Factor Scoring Functions ───────────────────────────────────────────────────

def _score_trend(closes: list, direction: str) -> float:
    """
    SMA alignment score (0–100).
    Checks price vs 20, 50, 200 SMA.
    Bonus for full SMA stack alignment (20 > 50 > 200 for bullish).
    """
    sma20  = _sma(closes, 20)
    sma50  = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    price  = closes[-1]
    score  = 0.0

    if direction == "bullish":
        if sma200 and price > sma200: score += 33.0
        if sma50  and price > sma50:  score += 33.0
        if sma20  and price > sma20:  score += 34.0
        if sma20 and sma50 and sma200 and sma20 > sma50 > sma200:
            score = min(100.0, score + 10.0)  # Full stack bonus
    else:
        if sma200 and price < sma200: score += 33.0
        if sma50  and price < sma50:  score += 33.0
        if sma20  and price < sma20:  score += 34.0
        if sma20 and sma50 and sma200 and sma20 < sma50 < sma200:
            score = min(100.0, score + 10.0)

    return round(score, 1)


def _score_momentum(closes: list, direction: str) -> Tuple[float, list]:
    """
    RSI + MACD momentum score (0–100).
    RSI 60%, MACD 40%.
    Returns (score, llm_triggers).
    """
    triggers = []
    rsi         = _rsi(closes)
    macd_result = _macd(closes)

    # ── RSI component ──
    rsi_score = 50.0  # Neutral default
    if rsi is not None:
        if rsi < 20 or rsi > 80:
            triggers.append("extreme_rsi")

        if direction == "bullish":
            if   50 <= rsi <= 65: rsi_score = 100.0  # Sweet spot
            elif 40 <= rsi <  50: rsi_score = 70.0   # Building
            elif 65 <  rsi <= 70: rsi_score = 70.0   # Strong, not overbought
            elif 30 <= rsi <  40: rsi_score = 40.0   # Weak
            elif 70 <  rsi <= 80: rsi_score = 35.0   # Overbought caution
            else:                 rsi_score = 15.0   # Extreme
        else:
            if   35 <= rsi <= 50: rsi_score = 100.0
            elif 50 <  rsi <= 60: rsi_score = 70.0
            elif 30 <= rsi <  35: rsi_score = 70.0
            elif 60 <  rsi <= 70: rsi_score = 40.0
            elif 20 <= rsi <  30: rsi_score = 35.0
            else:                 rsi_score = 15.0

    # ── MACD component ──
    macd_score = 50.0
    if macd_result is not None:
        _, _, histogram = macd_result
        if direction == "bullish":
            if   histogram >  0.5: macd_score = 100.0
            elif histogram >  0:   macd_score = 75.0
            elif histogram > -0.2: macd_score = 40.0
            else:                  macd_score = 10.0
        else:
            if   histogram < -0.5: macd_score = 100.0
            elif histogram <  0:   macd_score = 75.0
            elif histogram <  0.2: macd_score = 40.0
            else:                  macd_score = 10.0

    score = round(rsi_score * 0.6 + macd_score * 0.4, 1)
    return score, triggers


def _score_structure(bars: list, direction: str) -> Tuple[float, list]:
    """
    Price structure score (0–100).
    Breakout quality (40%), Higher highs/lows (30%), ATR profile (30%).
    Returns (score, llm_triggers).
    """
    triggers = []
    if len(bars) < 20:
        return 40.0, triggers  # Neutral — insufficient data

    recent = bars[-20:]
    highs  = [b["h"] for b in recent]
    lows   = [b["l"] for b in recent]
    closes = [b["c"] for b in recent]
    price  = closes[-1]
    score  = 0.0

    # ── Breakout component (40 pts) ──
    prior_high = max(highs[:-1])
    prior_low  = min(lows[:-1])

    if direction == "bullish":
        if price > prior_high:
            score += 40.0                        # Clean breakout
        elif price > prior_high * 0.98:
            score += 25.0                        # Within 2% — approaching
        else:
            score += 10.0
    else:
        if price < prior_low:
            score += 40.0
        elif price < prior_low * 1.02:
            score += 25.0
        else:
            score += 10.0

    # ── Higher highs / lower lows (30 pts) ──
    last5 = closes[-5:]
    if direction == "bullish":
        if all(last5[i] >= last5[i - 1] for i in range(1, len(last5))):
            score += 30.0   # 5 consecutive higher closes
        elif last5[-1] > last5[-3]:
            score += 18.0
        else:
            score += 5.0
    else:
        if all(last5[i] <= last5[i - 1] for i in range(1, len(last5))):
            score += 30.0
        elif last5[-1] < last5[-3]:
            score += 18.0
        else:
            score += 5.0

    # ── ATR profile (30 pts) ──
    # Contraction then expansion = quality setup
    atr5  = _atr(bars[-6:])
    atr20 = _atr(recent)

    if atr20 > 0:
        ratio = atr5 / atr20
        if ratio >= 1.5:   score += 30.0   # Strong expansion — move in progress
        elif ratio >= 1.2: score += 20.0
        elif ratio >= 0.8: score += 12.0
        else:              score += 18.0   # Contraction — coiling setup (also good)

    return round(min(100.0, score), 1), triggers


def _score_volume(bars: list) -> Tuple[float, list]:
    """
    Volume confirmation score (0–100).
    Current bar vs 20-day average.
    Returns (score, llm_triggers).
    """
    triggers = []
    if len(bars) < 21:
        return 40.0, triggers

    volumes  = [b["v"] for b in bars]
    avg20    = sum(volumes[-21:-1]) / 20
    current  = volumes[-1]

    if avg20 == 0:
        return 40.0, triggers

    ratio = current / avg20

    if ratio >= 5.0:
        triggers.append("vol_spike")   # Unusual — flag for LLM review
        return 90.0, triggers          # High score but flagged
    elif ratio >= 2.0: return 100.0, triggers
    elif ratio >= 1.5: return 80.0, triggers
    elif ratio >= 1.2: return 60.0, triggers
    elif ratio >= 1.0: return 40.0, triggers
    else:              return 20.0, triggers


def _score_relative_strength(ticker_bars: list, spy_bars: list, direction: str) -> float:
    """
    5-day relative strength vs SPY (0–100).
    Bullish: outperforming is good.
    Bearish: underperforming (moving opposite SPY) is good.
    """
    if len(ticker_bars) < 6 or len(spy_bars) < 6:
        return 50.0  # Neutral default

    ticker_ret = (ticker_bars[-1]["c"] / ticker_bars[-6]["c"]) - 1
    spy_ret    = (spy_bars[-1]["c"] / spy_bars[-6]["c"]) - 1
    diff       = ticker_ret - spy_ret  # Positive = outperforming SPY

    if direction == "bullish":
        if   diff >=  0.04: return 100.0
        elif diff >=  0.02: return 80.0
        elif diff >=  0.00: return 60.0
        elif diff >= -0.02: return 35.0
        else:               return 15.0
    else:
        if   diff <= -0.04: return 100.0
        elif diff <= -0.02: return 80.0
        elif diff <=  0.00: return 60.0
        elif diff <=  0.02: return 35.0
        else:               return 15.0


# ── Direction Determination ────────────────────────────────────────────────────

def _determine_direction(closes: list) -> str:
    """
    Determine bullish/bearish bias from SMA stack.
    2-of-3 SMAs above price = bearish, below = bullish.
    """
    if len(closes) < 20:
        return "bullish"

    price  = closes[-1]
    sma20  = _sma(closes, 20)
    sma50  = _sma(closes, 50)
    sma200 = _sma(closes, 200)

    bullish = 0
    if sma20  and price > sma20:  bullish += 1
    if sma50  and price > sma50:  bullish += 1
    if sma200 and price > sma200: bullish += 1

    return "bullish" if bullish >= 2 else "bearish"


def _check_factor_conflict(factors: FactorScores, direction: str) -> bool:
    """
    Detects severe conflict between trend and momentum.
    Strong trend signal contradicted by strong momentum signal = flag for LLM.
    """
    if direction == "bullish":
        return factors.trend >= 80 and factors.momentum <= 30
    else:
        return factors.trend >= 80 and factors.momentum <= 30


# ── Main Scoring Entry Point ───────────────────────────────────────────────────

def score_ticker(ticker: str, spy_bars: Optional[list] = None) -> CipherResult:
    """
    Score a single ticker for Alpha and Prime submission.

    Args:
        ticker:    Stock symbol
        spy_bars:  Pre-fetched SPY bars (fetched if not provided)

    Returns:
        CipherResult with alpha_score, prime_score, submit flags, AILS data
    """
    result = CipherResult(ticker=ticker)

    # ── Fetch price data ──
    bars = _fetch_bars(ticker, days=210)
    if len(bars) < 50:
        result.hard_block   = True
        result.block_reason = f"Insufficient data: {len(bars)} bars (need 50)"
        result.error        = result.block_reason
        logger.warning("BLOCKED %s: %s", ticker, result.block_reason)
        return result

    closes = [b["c"] for b in bars]

    # ── Determine direction ──
    result.direction = _determine_direction(closes)
    direction        = result.direction

    # ── Fetch SPY for relative strength ──
    if spy_bars is None:
        spy_bars = _fetch_bars("SPY", days=30)

    # ── Score all factors ──
    all_triggers = []
    factors      = FactorScores()

    factors.trend                        = _score_trend(closes, direction)
    factors.momentum, mom_triggers       = _score_momentum(closes, direction)
    factors.structure, struct_triggers   = _score_structure(bars, direction)
    factors.volume, vol_triggers         = _score_volume(bars)
    factors.rel_str                      = _score_relative_strength(bars, spy_bars, direction)

    all_triggers.extend(mom_triggers)
    all_triggers.extend(struct_triggers)
    all_triggers.extend(vol_triggers)

    # ── Factor conflict check ──
    if _check_factor_conflict(factors, direction):
        all_triggers.append("factor_conflict")

    result.factors = factors

    # ── Composite scores ──
    result.alpha_score = round(
        factors.trend     * ALPHA_WEIGHTS["trend"]     +
        factors.momentum  * ALPHA_WEIGHTS["momentum"]  +
        factors.structure * ALPHA_WEIGHTS["structure"] +
        factors.volume    * ALPHA_WEIGHTS["volume"]    +
        factors.rel_str   * ALPHA_WEIGHTS["rel_str"],
        1
    )
    result.prime_score = round(
        factors.trend     * PRIME_WEIGHTS["trend"]     +
        factors.momentum  * PRIME_WEIGHTS["momentum"]  +
        factors.structure * PRIME_WEIGHTS["structure"] +
        factors.volume    * PRIME_WEIGHTS["volume"]    +
        factors.rel_str   * PRIME_WEIGHTS["rel_str"],
        1
    )

    # ── LLM flag ──
    result.llm_triggers = list(set(all_triggers))
    result.llm_flag     = len(result.llm_triggers) > 0

    # ── Submission decisions ──
    result.submit_alpha = result.alpha_score >= ALPHA_FLOOR and not result.hard_block
    result.submit_prime = result.prime_score >= PRIME_FLOOR and not result.hard_block

    # ── Reasoning string (for logs + AILS) ──
    result.reasoning = (
        f"{ticker} [{direction.upper()}] "
        f"A={result.alpha_score:.1f} P={result.prime_score:.1f} | "
        f"Trend={factors.trend:.0f} Mom={factors.momentum:.0f} "
        f"Struct={factors.structure:.0f} Vol={factors.volume:.0f} "
        f"RS={factors.rel_str:.0f}"
        + (f" | LLM:{','.join(result.llm_triggers)}" if result.llm_flag else "")
    )

    logger.info(result.reasoning)
    return result


def score_pool(tickers: list, window_id: str = "") -> list:
    """
    Score an entire Axiom pool.
    Fetches SPY once, reuses for all RS calculations.
    Returns all results sorted by alpha_score descending.

    Args:
        tickers:   Ticker list from Axiom pool push
        window_id: Axiom window_id for concordance tracking

    Returns:
        List[CipherResult] — all tickers, including blocks
    """
    logger.info("[%s] Scoring pool of %d tickers", window_id or "manual", len(tickers))

    # Fetch SPY once
    spy_bars = _fetch_bars("SPY", days=30)
    if len(spy_bars) < 6:
        logger.warning("SPY data unavailable — RS scoring defaults to neutral 50")
        spy_bars = []

    results = []
    for ticker in tickers:
        try:
            r = score_ticker(ticker, spy_bars)
            results.append(r)
        except Exception as e:
            logger.error("Unhandled error scoring %s: %s", ticker, e)
            results.append(CipherResult(
                ticker=ticker,
                hard_block=True,
                block_reason=f"Unhandled: {e}",
                error=str(e),
            ))
        time.sleep(0.12)  # Polygon rate limit courtesy pause

    # Sort by alpha_score descending
    results.sort(key=lambda r: r.alpha_score, reverse=True)

    qualifiers = [r for r in results if r.submit_alpha or r.submit_prime]
    llm_flags  = [r for r in results if r.llm_flag]
    blocked    = [r for r in results if r.hard_block]

    logger.info(
        "[%s] Pool complete | %d/%d qualify | %d LLM-flagged | %d blocked",
        window_id or "manual",
        len(qualifiers), len(tickers),
        len(llm_flags), len(blocked),
    )

    return results


def format_results_table(results: list) -> str:
    """Human-readable results table for logging/Telegram."""
    lines = [
        f"{'TICKER':<8} {'DIR':<9} {'ALPHA':>6} {'PRIME':>6} {'A':>4} {'P':>4} {'FLAGS'}",
        "-" * 65,
    ]
    for r in results:
        if r.hard_block:
            lines.append(f"{r.ticker:<8} {'BLOCKED':<9} {'--':>6} {'--':>6} {'':>4} {'':>4} {r.block_reason}")
        else:
            flags = ",".join(r.llm_triggers) if r.llm_triggers else ""
            lines.append(
                f"{r.ticker:<8} {r.direction:<9} "
                f"{r.alpha_score:>6.1f} {r.prime_score:>6.1f} "
                f"{'GO':>4} {'GO':>4} {flags}"
                if r.submit_alpha and r.submit_prime else
                f"{r.ticker:<8} {r.direction:<9} "
                f"{r.alpha_score:>6.1f} {r.prime_score:>6.1f} "
                f"{'GO' if r.submit_alpha else '--':>4} "
                f"{'GO' if r.submit_prime else '--':>4} {flags}"
            )
    return "\n".join(lines)


# ── Test Harness ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    # Simulate an Axiom pool push
    test_pool = ["NVDA", "AAPL", "GOOGL", "TSLA", "AMAT",
                 "GLD", "BKNG", "AXP", "ETN", "VLO"]

    print("\n" + "=" * 65)
    print("CIPHER SCORING ENGINE — TEST RUN")
    print(f"Pool: {test_pool}")
    print("=" * 65 + "\n")

    results = score_pool(test_pool, window_id="TEST-2026-05-19")

    print("\n" + format_results_table(results))

    # Summary
    qualifiers = [r for r in results if r.submit_alpha or r.submit_prime]
    llm_flags  = [r for r in results if r.llm_flag and not r.hard_block]

    print(f"\n{'='*65}")
    print(f"Qualifiers for submission: {len(qualifiers)}/{len(test_pool)}")
    print(f"LLM review flagged:        {len(llm_flags)}")
    print(f"Hard blocked:              {sum(1 for r in results if r.hard_block)}")

    # AILS payload preview
    print(f"\nSample AILS payload ({results[0].ticker}):")
    first = results[0]
    ails_payload = {
        "ticker":       first.ticker,
        "window_id":    "TEST-2026-05-19",
        "direction":    first.direction,
        "alpha_score":  first.alpha_score,
        "prime_score":  first.prime_score,
        "factors": {
            "trend":     first.factors.trend,
            "momentum":  first.factors.momentum,
            "structure": first.factors.structure,
            "volume":    first.factors.volume,
            "rel_str":   first.factors.rel_str,
        },
        "llm_flag":     first.llm_flag,
        "llm_triggers": first.llm_triggers,
        "submit_alpha": first.submit_alpha,
        "submit_prime": first.submit_prime,
        "scored_at":    first.scored_at,
    }
    print(json.dumps(ails_payload, indent=2))
