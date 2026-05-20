"""
atlas_scorer.py — Deterministic Options Intelligence Scoring Engine
Nexus V1 | Atlas Agent

Scores tickers using IV structure, options flow, skew, and gamma positioning.
LLM reserved for anomalous flow or extreme IV dislocations only.

SCORING FACTORS:
  Alpha (options):  IVR 35% | Term Structure 25% | Skew 20% | Flow 15% | Gamma 5%
  Prime (swing):    Flow 40% | Skew 20% | IVR 15% | Gamma 15% | Term 10%

DATA SOURCES:
  ORATS          : IV rank, term structure, skew, earnings IV
  Unusual Whales : Options flow intelligence
  Polygon        : Fallback IV estimate via historical vs realized vol
  SpotGamma      : Gamma walls (optional, graceful fallback)

Atlas-specific rules (from documented constraints):
  IVR < 35  → Debit spreads preferred
  IVR 35–55 → Acceptable, reduce confidence 5–10 pts
  IVR > 55  → Credit/neutral only
  IVR > 70  → Neutral only, no directional plays
  DTE 25–60 → Optimal (30–45 ideal)
"""

import os
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger("atlas.scorer")

# ── Configuration ──────────────────────────────────────────────────────────────

POLYGON_API_KEY  = os.getenv("POLYGON_API_KEY", "ZMEnZ_2GURw_UqbufU5npd49ZDeptMhl")
POLYGON_BASE     = "https://api.polygon.io"

ORATS_TOKEN      = os.getenv("ORATS_TOKEN", os.getenv("ORATS_API_KEY", ""))
ORATS_BASE       = "https://api.orats.io/datav2"

UW_TOKEN         = os.getenv("UNUSUAL_WHALES_API_KEY", os.getenv("UW_API_KEY", ""))
UW_BASE          = "https://phx.unusualwhales.com/api"

SPOTGAMMA_KEY    = os.getenv("SPOTGAMMA_API_KEY", "")
SPOTGAMMA_BASE   = "https://api.spotgamma.com"

# Submission floors
ALPHA_FLOOR = 45.0
PRIME_FLOOR = 45.0

# Alpha weights (options — IV environment is primary)
ALPHA_WEIGHTS = {
    "ivr":      0.35,
    "term":     0.25,
    "skew":     0.20,
    "flow":     0.15,
    "gamma":    0.05,
}

# Prime weights (swing — flow is the signal, options activity predicts moves)
PRIME_WEIGHTS = {
    "flow":     0.40,
    "skew":     0.20,
    "ivr":      0.15,
    "gamma":    0.15,
    "term":     0.10,
}

# Atlas hard rules (documented constraints)
IVR_DEBIT_MAX    = 35.0   # Below: debit spreads preferred
IVR_CAUTION_MAX  = 55.0   # Above: credit/neutral only
IVR_NEUTRAL_MIN  = 70.0   # Above: neutral only, no directional
IVR_SWEET_SPOT_LO = 20.0  # Sweet spot for directional options
IVR_SWEET_SPOT_HI = 60.0  # Sweet spot for directional options

# LLM triggers
LLM_TRIGGERS = [
    "extreme_ivr",       # IVR > 80 or < 10
    "flow_anomaly",      # Unusual whale activity > 3x normal
    "skew_diverge",      # Skew strongly contradicts price direction
    "term_backwardation",# Front > back month (stress signal)
]


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class AtlasFactors:
    ivr:   float = 0.0   # IV Rank (0–100 scale scored)
    term:  float = 0.0   # Term structure score
    skew:  float = 0.0   # Put/call skew alignment
    flow:  float = 0.0   # Unusual options flow
    gamma: float = 0.0   # Gamma wall positioning


@dataclass
class AtlasResult:
    ticker:        str
    direction:     str          = "neutral"
    alpha_score:   float        = 0.0
    prime_score:   float        = 0.0
    factors:       AtlasFactors = field(default_factory=AtlasFactors)
    ivr_raw:       float        = 0.0   # Raw IVR value for logging
    hard_block:    bool         = False
    block_reason:  str          = ""
    llm_flag:      bool         = False
    llm_triggers:  list         = field(default_factory=list)
    submit_alpha:  bool         = False
    submit_prime:  bool         = False
    reasoning:     str          = ""
    scored_at:     str          = field(default_factory=lambda: datetime.utcnow().isoformat())


# ── Data Fetching ──────────────────────────────────────────────────────────────

def _fetch_orats_summary(ticker: str) -> Optional[dict]:
    """
    Fetch ORATS summary data for a ticker.
    Returns dict with ivRank, ivMean, smvVol, etc.
    """
    if not ORATS_TOKEN:
        return None
    url = f"{ORATS_BASE}/summaries?token={ORATS_TOKEN}&ticker={ticker}"
    try:
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                return data[0]
    except Exception as e:
        logger.debug("ORATS summary %s: %s", ticker, e)
    return None


def _fetch_polygon_bars(ticker: str, days: int = 60) -> list:
    """Fetch daily OHLCV bars from Polygon for vol estimation."""
    end   = datetime.now()
    start = end - timedelta(days=int(days * 1.5))
    url   = (
        f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/"
        f"{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
        f"?adjusted=true&sort=asc&limit=120&apiKey={POLYGON_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            return r.json().get("results", [])
    except Exception as e:
        logger.debug("Polygon bars %s: %s", ticker, e)
    return []


def _fetch_unusual_whales(ticker: str) -> Optional[dict]:
    """
    Fetch recent options flow from Unusual Whales.
    Returns flow summary or None.
    """
    if not UW_TOKEN:
        return None
    url = f"{UW_BASE}/flow/ticker?ticker={ticker}"
    headers = {"Authorization": f"Bearer {UW_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("Unusual Whales %s: %s", ticker, e)
    return None


def _estimate_historical_vol(bars: list, period: int = 21) -> Optional[float]:
    """
    Estimate 21-day historical realized volatility (annualized).
    Used as fallback IVR proxy when ORATS unavailable.
    """
    if len(bars) < period + 1:
        return None
    import math
    closes  = [b["c"] for b in bars[-(period + 1):]]
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean    = sum(returns) / len(returns)
    variance= sum((r - mean) ** 2 for r in returns) / len(returns)
    return math.sqrt(variance * 252) * 100  # Annualized %


def _estimate_ivr_from_polygon(ticker: str, bars: list) -> Optional[float]:
    """
    Estimate IV rank proxy from historical volatility.
    HV now vs HV range over past year = rough IVR proxy.
    Not as accurate as ORATS but functional fallback.
    """
    if len(bars) < 252:
        return None

    import math

    def hv(data, period):
        if len(data) < period + 1:
            return None
        cls = [b["c"] for b in data[-(period + 1):]]
        rets = [math.log(cls[i] / cls[i-1]) for i in range(1, len(cls))]
        m = sum(rets) / len(rets)
        v = sum((r - m)**2 for r in rets) / len(rets)
        return math.sqrt(v * 252) * 100

    # Current 21-day HV
    hv_current = hv(bars, 21)
    if not hv_current:
        return None

    # Rolling 21-day HV over past year
    hvs = []
    for i in range(0, min(252, len(bars) - 22), 5):  # Sample every 5 days
        window = bars[i:i + 22]
        h = hv(window, 21)
        if h:
            hvs.append(h)

    if not hvs:
        return None

    hv_min = min(hvs)
    hv_max = max(hvs)
    if hv_max == hv_min:
        return 50.0

    ivr_proxy = (hv_current - hv_min) / (hv_max - hv_min) * 100
    return round(min(100.0, max(0.0, ivr_proxy)), 1)


# ── Factor Scoring ─────────────────────────────────────────────────────────────

def _score_ivr(ivr: Optional[float], direction: str) -> tuple:
    """
    IV Rank score (0–100) + LLM triggers + hard block check.
    Based on Atlas's documented IVR constraints.
    Returns (score, hard_block, block_reason, triggers).
    """
    triggers = []

    if ivr is None:
        return 50.0, False, "", triggers   # Neutral fallback

    # Hard block: IVR > 70 = no directional (Atlas rule)
    if ivr > IVR_NEUTRAL_MIN and direction != "neutral":
        return 20.0, False, "", triggers   # Very low score but no hard block (POC: learn from it)

    if ivr > 80 or ivr < 10:
        triggers.append("extreme_ivr")

    # Score IVR for Alpha (options strategy)
    if direction in ("bullish", "bearish"):
        # Sweet spot: IVR 20–55 = good for directional debit/defined risk
        if   20 <= ivr <= 35:  score = 100.0   # Ideal for debit spreads
        elif 35 <  ivr <= 55:  score = 80.0    # Acceptable, slightly elevated
        elif 15 <= ivr <  20:  score = 65.0    # Low IV — cheap premiums
        elif 55 <  ivr <= 70:  score = 45.0    # High — credit territory
        elif 70 <  ivr <= 80:  score = 25.0    # Very high
        elif ivr > 80:         score = 10.0    # Extreme
        elif ivr < 15:         score = 55.0    # Very low
        else:                  score = 50.0

    return round(score, 1), False, "", triggers


def _score_term_structure(orats: Optional[dict], bars: list, direction: str) -> tuple:
    """
    Term structure score (0–100).
    Normal contango (front < back IV) = good for directional options.
    Backwardation (front > back) = stress signal.
    Returns (score, triggers).
    """
    triggers = []
    score    = 65.0   # Neutral default

    if orats:
        # ORATS fields: ivMean30 (30d IV), ivMean60 (60d IV)
        iv30 = orats.get("ivMean30", 0)
        iv60 = orats.get("ivMean60", 0)

        if iv30 > 0 and iv60 > 0:
            ratio = iv30 / iv60

            if ratio > 1.15:
                triggers.append("term_backwardation")
                score = 20.0    # Significant backwardation — stress
            elif ratio > 1.05:
                score = 40.0    # Mild backwardation
            elif 0.95 <= ratio <= 1.05:
                score = 70.0    # Near flat — normal
            elif ratio < 0.90:
                score = 100.0   # Strong contango — excellent
            else:
                score = 85.0    # Mild contango

    return round(score, 1), triggers


def _score_skew(orats: Optional[dict], direction: str) -> tuple:
    """
    Skew alignment score (0–100).
    Put skew > call skew = bearish market sentiment.
    Score based on whether skew aligns with trade direction.
    Returns (score, triggers).
    """
    triggers = []
    score    = 50.0   # Neutral default

    if orats:
        # ORATS: callVolume, putVolume, putCallVolRatio
        pc_ratio = orats.get("putCallVolRatio", None)

        if pc_ratio is not None:
            if direction == "bullish":
                # Bullish: want put/call ratio LOW (call buyers dominant)
                if   pc_ratio < 0.50:  score = 100.0   # Strong call buying
                elif pc_ratio < 0.70:  score = 80.0
                elif pc_ratio < 0.90:  score = 60.0
                elif pc_ratio < 1.10:  score = 45.0    # Neutral
                elif pc_ratio < 1.30:  score = 30.0    # Put heavy
                else:
                    score = 15.0                        # Strong put buying — bearish
                    triggers.append("skew_diverge")
            else:  # bearish
                # Bearish: want put/call ratio HIGH (put buyers dominant)
                if   pc_ratio > 1.50:  score = 100.0
                elif pc_ratio > 1.20:  score = 80.0
                elif pc_ratio > 1.00:  score = 65.0
                elif pc_ratio > 0.80:  score = 45.0
                elif pc_ratio > 0.60:  score = 30.0
                else:
                    score = 15.0
                    triggers.append("skew_diverge")

    return round(score, 1), triggers


def _score_flow(uw_data: Optional[dict], direction: str) -> tuple:
    """
    Unusual options flow score (0–100).
    Dominant call flow = bullish signal for Prime swing.
    Dominant put flow = bearish signal.
    Returns (score, triggers).
    """
    triggers = []
    score    = 50.0   # Neutral default (no API available)

    if uw_data is None:
        return score, triggers

    try:
        # Unusual Whales data structure varies — defensive parsing
        flow_data = uw_data.get("data", uw_data)

        if isinstance(flow_data, list) and len(flow_data) > 0:
            call_premium = sum(
                f.get("premium", 0) for f in flow_data
                if f.get("put_call", "").lower() == "call"
            )
            put_premium = sum(
                f.get("premium", 0) for f in flow_data
                if f.get("put_call", "").lower() == "put"
            )
            total = call_premium + put_premium

            if total > 0:
                call_pct = call_premium / total
                # Anomaly detection
                if total > 5_000_000 and (call_pct > 0.85 or call_pct < 0.15):
                    triggers.append("flow_anomaly")

                if direction == "bullish":
                    if   call_pct >= 0.75: score = 100.0
                    elif call_pct >= 0.60: score = 80.0
                    elif call_pct >= 0.50: score = 60.0
                    elif call_pct >= 0.40: score = 40.0
                    else:                  score = 20.0
                else:  # bearish
                    if   call_pct <= 0.25: score = 100.0
                    elif call_pct <= 0.40: score = 80.0
                    elif call_pct <= 0.50: score = 60.0
                    elif call_pct <= 0.60: score = 40.0
                    else:                  score = 20.0

    except Exception as e:
        logger.debug("Flow parsing error: %s", e)

    return round(score, 1), triggers


def _score_gamma(ticker: str, bars: list, direction: str) -> float:
    """
    Gamma wall positioning score (0–100).
    SpotGamma preferred. Fallback: proximity to key round numbers.
    Away from gamma walls = better directional move potential.
    """
    if not bars:
        return 50.0

    price = bars[-1]["c"]

    # Fallback: estimate gamma walls at round numbers
    # Stocks tend to pin near round numbers due to options clustering
    round_100 = round(price / 100) * 100
    round_50  = round(price / 50) * 50
    round_25  = round(price / 25) * 25

    # Distance from nearest round number (% of price)
    dist_100 = abs(price - round_100) / price
    dist_50  = abs(price - round_50) / price
    dist_25  = abs(price - round_25) / price

    min_dist = min(dist_100, dist_50, dist_25)

    # Farther from round number = less gamma pinning = better for directional
    if   min_dist >= 0.04: return 90.0   # Far from wall — good
    elif min_dist >= 0.02: return 70.0
    elif min_dist >= 0.01: return 50.0
    else:                  return 25.0   # Near gamma wall — pinning risk


def _determine_direction(orats: Optional[dict], bars: list) -> str:
    """
    Atlas determines direction from options market sentiment.
    Put/call ratio < 0.7 = bullish sentiment.
    Put/call ratio > 1.1 = bearish sentiment.
    Fallback: price trend.
    """
    if orats:
        pc = orats.get("putCallVolRatio")
        if pc is not None:
            if pc < 0.70:
                return "bullish"
            elif pc > 1.10:
                return "bearish"

    # Fallback: price trend
    if len(bars) >= 10:
        return "bullish" if bars[-1]["c"] > bars[-10]["c"] else "bearish"

    return "bullish"


# ── Main Scoring Entry Point ───────────────────────────────────────────────────

def score_ticker(ticker: str) -> AtlasResult:
    """
    Score a single ticker for both Alpha and Prime.
    Atlas is primarily options-focused — IV data is the core signal.

    Returns:
        AtlasResult with alpha_score, prime_score, submit flags, AILS data
    """
    result = AtlasResult(ticker=ticker)

    # ── Fetch data ──
    bars   = _fetch_polygon_bars(ticker, days=60)
    orats  = _fetch_orats_summary(ticker)
    uw     = _fetch_unusual_whales(ticker)

    # ── Get IVR ──
    ivr = None
    if orats:
        ivr = orats.get("ivRank")
    if ivr is None:
        ivr = _estimate_ivr_from_polygon(ticker, _fetch_polygon_bars(ticker, days=300))
    result.ivr_raw = ivr or 0.0

    # ── Determine direction from options market ──
    result.direction = _determine_direction(orats, bars)
    direction        = result.direction

    # ── Score all factors ──
    all_triggers = []
    factors      = AtlasFactors()

    factors.ivr, hard_block, block_reason, ivr_triggers = _score_ivr(ivr, direction)
    all_triggers.extend(ivr_triggers)

    if hard_block:
        result.hard_block   = True
        result.block_reason = block_reason
        return result

    factors.term, term_triggers = _score_term_structure(orats, bars, direction)
    all_triggers.extend(term_triggers)

    factors.skew, skew_triggers = _score_skew(orats, direction)
    all_triggers.extend(skew_triggers)

    factors.flow, flow_triggers = _score_flow(uw, direction)
    all_triggers.extend(flow_triggers)

    factors.gamma = _score_gamma(ticker, bars, direction)

    result.factors = factors

    # ── Composite scores ──
    result.alpha_score = round(
        factors.ivr   * ALPHA_WEIGHTS["ivr"]  +
        factors.term  * ALPHA_WEIGHTS["term"] +
        factors.skew  * ALPHA_WEIGHTS["skew"] +
        factors.flow  * ALPHA_WEIGHTS["flow"] +
        factors.gamma * ALPHA_WEIGHTS["gamma"],
        1
    )

    result.prime_score = round(
        factors.flow  * PRIME_WEIGHTS["flow"]  +
        factors.skew  * PRIME_WEIGHTS["skew"]  +
        factors.ivr   * PRIME_WEIGHTS["ivr"]   +
        factors.gamma * PRIME_WEIGHTS["gamma"] +
        factors.term  * PRIME_WEIGHTS["term"],
        1
    )

    # IVR adjustment: If IVR > 55, reduce Alpha score (Atlas constraint)
    if ivr and ivr > IVR_CAUTION_MAX:
        result.alpha_score = max(0.0, result.alpha_score - 8.0)

    # ── LLM flag ──
    result.llm_triggers = list(set(all_triggers))
    result.llm_flag     = len(result.llm_triggers) > 0

    # ── Submission decisions ──
    result.submit_alpha = result.alpha_score >= ALPHA_FLOOR and not result.hard_block
    result.submit_prime = result.prime_score >= PRIME_FLOOR and not result.hard_block

    # ── Reasoning ──
    ivr_str = f"{ivr:.0f}" if ivr else "N/A"
    result.reasoning = (
        f"{ticker} [{direction.upper()}] "
        f"A={result.alpha_score:.1f} P={result.prime_score:.1f} | "
        f"IVR={ivr_str} IVRsc={factors.ivr:.0f} Term={factors.term:.0f} "
        f"Skew={factors.skew:.0f} Flow={factors.flow:.0f} Gamma={factors.gamma:.0f}"
        + (f" | LLM:{','.join(result.llm_triggers)}" if result.llm_flag else "")
    )

    logger.info(result.reasoning)
    return result


def score_pool(tickers: list, window_id: str = "") -> list:
    """Score an entire Axiom pool. Returns results sorted by alpha_score."""
    logger.info("[%s] Atlas scoring pool of %d tickers", window_id or "manual", len(tickers))

    results = []
    for ticker in tickers:
        try:
            results.append(score_ticker(ticker))
        except Exception as e:
            logger.error("Error scoring %s: %s", ticker, e)
            results.append(AtlasResult(
                ticker=ticker, hard_block=True, block_reason=str(e)
            ))
        time.sleep(0.15)

    results.sort(key=lambda r: r.alpha_score, reverse=True)
    qualifiers = [r for r in results if r.submit_alpha or r.submit_prime]
    logger.info("[%s] Atlas complete | %d/%d qualify", window_id or "manual",
                len(qualifiers), len(tickers))
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
    print("ATLAS SCORING ENGINE — TEST RUN")
    print(f"Pool: {test_pool}")
    print("=" * 70 + "\n")

    results = score_pool(test_pool, window_id="TEST-2026-05-19")

    print(f"\n{'TICKER':<8} {'DIR':<9} {'ALPHA':>6} {'PRIME':>6} {'A':>4} {'P':>4} {'IVR':>6} FLAGS")
    print("-" * 70)
    for r in results:
        flags  = ",".join(r.llm_triggers) if r.llm_triggers else ""
        ivr_str = f"{r.ivr_raw:.0f}" if r.ivr_raw else "N/A"
        print(
            f"{r.ticker:<8} {r.direction:<9} "
            f"{r.alpha_score:>6.1f} {r.prime_score:>6.1f} "
            f"{'GO' if r.submit_alpha else '--':>4} "
            f"{'GO' if r.submit_prime else '--':>4} "
            f"{ivr_str:>6} {flags}"
        )

    qualifiers = [r for r in results if r.submit_alpha or r.submit_prime]
    print(f"\nQualifiers: {len(qualifiers)}/{len(test_pool)}")
    if not ORATS_TOKEN:
        print("⚠️  ORATS_TOKEN not set — IV scores using Polygon HV proxy")
    if not UW_TOKEN:
        print("⚠️  UW_API_KEY not set — flow scores defaulting to neutral 50")
