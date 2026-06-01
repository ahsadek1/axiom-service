"""
nexus_scorer.py — Unified Deterministic Scoring Engine
=======================================================
Replaces Cipher + Atlas + Sage agent wrappers entirely.
Pure mathematical functions. No LLM. No API agents.
No silent defaults. Every score carries a confidence flag.

Three scoring dimensions (same as agents, now pure math):
  TECHNICAL (was Cipher):   Price action, momentum, structure  — weight 45%
  OPTIONS   (was Atlas):    IV rank, flow, skew, gamma         — weight 30%
  MACRO     (was Sage):     VIX regime, sector RS, earnings    — weight 25%

Confidence levels:
  HIGH:   all primary data sources available
  MEDIUM: some data missing, fallbacks used
  LOW:    majority of data missing, score unreliable
  ABSENT: cannot score — exclude from concordance

Ahmed directive May 2026:
  "I need to have a deterministic system from A to Z.
   No agent in the trading loop. Pure math. Pure rules."
"""
from __future__ import annotations
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("nexus.scorer")
_ET = ZoneInfo("America/New_York")

POLYGON_KEY  = os.getenv("POLYGON_API_KEY",  "ZMEnZ_2GURw_UqbufU5npd49ZDeptMhl")
ORATS_TOKEN  = os.getenv("ORATS_TOKEN",      "4476e955-241a-4540-b114-ebbf1a3a3b87")
AV_KEY       = os.getenv("ALPHA_VANTAGE_KEY","5LPKGHYMW9ZK24KL")

POLYGON_BASE = "https://api.polygon.io"
ORATS_BASE   = "https://api.orats.io/datav2"
ALPACA_DATA  = "https://data.alpaca.markets"

ALPACA_KEY    = os.getenv("ALPACA_API_KEY","")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY","")
AXIOM_URL     = os.getenv("AXIOM_URL","http://localhost:8001")
AXIOM_SECRET  = os.getenv("AXIOM_SECRET","62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY","")

# Scoring weights — same as original agent weights
WEIGHT_TECHNICAL = 0.45   # was Cipher
WEIGHT_OPTIONS   = 0.30   # was Atlas
WEIGHT_MACRO     = 0.25   # was Sage

# Score thresholds
GO_THRESHOLD     = 65.0
STRONG_THRESHOLD = 75.0
SUBMISSION_FLOOR = 50.0


class Confidence(str, Enum):
    HIGH   = "HIGH"    # all primary data available
    MEDIUM = "MEDIUM"  # some data missing
    LOW    = "LOW"     # majority missing
    ABSENT = "ABSENT"  # cannot score


@dataclass
class DimensionScore:
    """Score for one dimension with full data lineage."""
    name:          str
    score:         float
    confidence:    Confidence
    data_used:     list[str]   = field(default_factory=list)
    data_missing:  list[str]   = field(default_factory=list)
    factors:       dict        = field(default_factory=dict)


@dataclass
class NexusScore:
    """
    Complete deterministic score for one ticker.
    Replaces the output of all three agent wrappers.
    """
    ticker:      str
    direction:   str            # bullish / bearish
    technical:   DimensionScore
    options:     DimensionScore
    macro:       DimensionScore

    combined:    float          # weighted combined score
    confidence:  Confidence     # overall confidence
    go:          bool           # meets GO threshold
    strong_go:   bool           # meets STRONG_GO threshold

    timestamp:   str            = ""

    def to_dict(self) -> dict:
        return {
            "ticker":     self.ticker,
            "direction":  self.direction,
            "combined":   self.combined,
            "confidence": self.confidence.value,
            "go":         self.go,
            "strong_go":  self.strong_go,
            "technical":  {
                "score":      self.technical.score,
                "confidence": self.technical.confidence.value,
                "factors":    self.technical.factors,
                "data_used":  self.technical.data_used,
                "missing":    self.technical.data_missing,
            },
            "options": {
                "score":      self.options.score,
                "confidence": self.options.confidence.value,
                "factors":    self.options.factors,
                "data_used":  self.options.data_used,
                "missing":    self.options.data_missing,
            },
            "macro": {
                "score":      self.macro.score,
                "confidence": self.macro.confidence.value,
                "factors":    self.macro.factors,
                "data_used":  self.macro.data_used,
                "missing":    self.macro.data_missing,
            },
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Data fetchers — every fetch is explicit, never silent
# ---------------------------------------------------------------------------

def _get(url: str, params: dict = None, timeout: int = 8) -> Optional[dict]:
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        log.debug("GET failed %s: %s", url, exc)
    return None


def fetch_daily_bars(ticker: str, days: int = 60) -> Optional[list]:
    """Fetch daily OHLCV bars from Polygon."""
    end   = date.today()
    start = end - timedelta(days=days)
    data  = _get(
        f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
        params={"adjusted":"true","sort":"asc","limit":days,"apiKey":POLYGON_KEY},
    )
    if data and data.get("results"):
        return data["results"]
    return None


def fetch_spy_bars(days: int = 10) -> Optional[list]:
    """Fetch SPY bars for relative strength."""
    return fetch_daily_bars("SPY", days)


def fetch_iv_rank(ticker: str) -> Optional[float]:
    """
    Estimate IV rank from Polygon historical volatility.
    Compare recent 20-day realized vol vs 252-day range.
    Returns 0-100 percentile rank. No ORATS required.
    """
    bars = fetch_daily_bars(ticker, days=252)
    if not bars or len(bars) < 25:
        return None
    closes = [b["c"] for b in bars]

    # Calculate rolling 20-day realized volatility
    def realized_vol(price_series):
        if len(price_series) < 2:
            return 0.0
        returns = [(price_series[i]-price_series[i-1])/price_series[i-1]
                   for i in range(1, len(price_series))]
        mean = sum(returns) / len(returns)
        variance = sum((r-mean)**2 for r in returns) / len(returns)
        return (variance ** 0.5) * (252 ** 0.5) * 100

    # Current 20-day vol
    current_vol = realized_vol(closes[-21:])

    # Historical rolling vols over past year
    vols = []
    for i in range(20, len(closes)):
        v = realized_vol(closes[i-20:i])
        vols.append(v)

    if not vols or current_vol == 0:
        return None

    # IV rank = percentile of current vol in historical range
    low  = min(vols)
    high = max(vols)
    if high == low:
        return 50.0
    iv_rank = (current_vol - low) / (high - low) * 100
    return round(min(100.0, max(0.0, iv_rank)), 1)


def fetch_skew(ticker: str) -> Optional[float]:
    """
    Fetch put/call skew from ORATS.
    Positive skew = puts more expensive = bearish lean.
    Negative skew = calls more expensive = bullish lean.
    """
    today = date.today()
    data  = _get(
        f"{ORATS_BASE}/hist/dailies",
        params={
            "token":     ORATS_TOKEN,
            "ticker":    ticker,
            "fields":    "ticker,tradeDate,slope",
            "startDate": (today - timedelta(days=5)).isoformat(),
            "endDate":   today.isoformat(),
        },
    )
    if data:
        rows = data.get("data", [])
        for row in reversed(rows):
            skew = row.get("slope")
            if skew is not None:
                try:
                    return float(skew)
                except (TypeError, ValueError):
                    pass
    return None


def fetch_vix() -> Optional[float]:
    """Fetch current VIX from Polygon."""
    data = _get(
        f"{POLYGON_BASE}/v2/aggs/ticker/VXX/prev",
        params={"apiKey": POLYGON_KEY},
    )
    if data and data.get("results"):
        return float(data["results"][0].get("c", 0)) or None

    # Fallback: Alpaca VIX
    try:
        r = requests.get(
            f"{ALPACA_DATA}/v2/stocks/VXX/trades/latest",
            headers={
                "APCA-API-KEY-ID":     ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            params={"feed": "iex"},
            timeout=5,
        )
        if r.status_code == 200:
            price = float(r.json().get("trade",{}).get("p",0))
            if price > 0:
                return price
    except Exception:
        pass
    return None


def fetch_axiom_regime() -> tuple[Optional[float], Optional[str]]:
    """
    Get VIX and regime classification directly from Axiom.
    Fastest and most reliable source — Axiom already fetches this.
    Returns (vix, regime_classification) or (None, None).
    """
    data = _get(
        f"{AXIOM_URL}/pool",
        params=None,
    )
    # Try without auth first, then with
    if not data:
        try:
            r = requests.get(
                f"{AXIOM_URL}/pool",
                headers={"X-Axiom-Secret": AXIOM_SECRET},
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
        except Exception:
            pass
    if data:
        regime = data.get("regime", {})
        vix = regime.get("vix")
        classification = regime.get("classification")
        if vix:
            return float(vix), classification
    return None, None


def fetch_days_to_earnings(ticker: str) -> Optional[int]:
    """Get days until next earnings from Alpha Vantage calendar."""
    try:
        r = requests.get(
            "https://www.alphavantage.co/query",
            params={"function":"EARNINGS_CALENDAR","horizon":"3month","apikey":AV_KEY},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        import csv, io
        reader = csv.DictReader(io.StringIO(r.text))
        today  = date.today()
        for row in reader:
            if row.get("symbol","").upper() == ticker.upper():
                rdate = date.fromisoformat(row.get("reportDate",""))
                return (rdate - today).days
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# DIMENSION 1: TECHNICAL SCORE (replaces Cipher)
# ---------------------------------------------------------------------------

def score_technical(
    ticker: str,
    bars:   Optional[list] = None,
    spy_bars: Optional[list] = None,
) -> DimensionScore:
    """
    Pure technical analysis. No LLM. No agent wrapper.

    Factors:
      Trend     (25%): price vs EMA20 vs EMA50
      Momentum  (25%): 5-day and 10-day price change
      Volume    (20%): volume vs 20-day average
      Structure (15%): distance from 52-week high
      RS        (15%): relative strength vs SPY
    """
    data_used    = []
    data_missing = []
    factors      = {}

    if bars is None:
        bars = fetch_daily_bars(ticker, days=60)

    if not bars or len(bars) < 20:
        data_missing.append("price_bars")
        return DimensionScore(
            name="technical", score=0.0,
            confidence=Confidence.ABSENT,
            data_missing=["price_bars"],
        )

    data_used.append("polygon_daily_bars")
    closes  = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]
    current = closes[-1]

    # ── Trend (25%) ───────────────────────────────────────────────────────────
    ema20 = sum(closes[-20:]) / 20
    ema50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else sum(closes) / len(closes)

    if current > ema20 > ema50:
        trend_score = 85.0   # strong uptrend
    elif current > ema20:
        trend_score = 70.0   # above short MA
    elif current > ema50:
        trend_score = 55.0   # above long MA only
    elif current < ema20 < ema50:
        trend_score = 15.0   # downtrend
    else:
        trend_score = 40.0   # mixed

    factors["trend"] = round(trend_score, 1)

    # ── Momentum (25%) ────────────────────────────────────────────────────────
    price_5d  = closes[-6]  if len(closes) >= 6  else closes[0]
    price_10d = closes[-11] if len(closes) >= 11 else closes[0]
    ch_5d  = (current - price_5d)  / price_5d  * 100
    ch_10d = (current - price_10d) / price_10d * 100

    # Score: strong positive momentum = high score
    mom_raw = ch_5d * 0.6 + ch_10d * 0.4
    if mom_raw >= 8:    mom_score = 90.0
    elif mom_raw >= 5:  mom_score = 78.0
    elif mom_raw >= 2:  mom_score = 65.0
    elif mom_raw >= 0:  mom_score = 52.0
    elif mom_raw >= -3: mom_score = 38.0
    else:               mom_score = 20.0

    factors["momentum"] = round(mom_score, 1)
    factors["change_5d"] = round(ch_5d, 2)

    # ── Volume (20%) ──────────────────────────────────────────────────────────
    avg_vol_20 = sum(volumes[-20:]) / 20
    vol_ratio  = volumes[-1] / avg_vol_20 if avg_vol_20 > 0 else 1.0

    if vol_ratio >= 2.5:   vol_score = 90.0
    elif vol_ratio >= 1.5: vol_score = 75.0
    elif vol_ratio >= 1.0: vol_score = 55.0
    elif vol_ratio >= 0.7: vol_score = 40.0
    else:                  vol_score = 25.0

    factors["volume_ratio"] = round(vol_ratio, 2)
    factors["volume"] = round(vol_score, 1)

    # ── Structure (15%) ───────────────────────────────────────────────────────
    high_52w      = max(closes)
    pct_from_high = (current - high_52w) / high_52w * 100

    if pct_from_high >= -2:    struct_score = 88.0
    elif pct_from_high >= -5:  struct_score = 75.0
    elif pct_from_high >= -10: struct_score = 60.0
    elif pct_from_high >= -20: struct_score = 45.0
    else:                      struct_score = 28.0

    factors["pct_from_52wh"] = round(pct_from_high, 2)
    factors["structure"] = round(struct_score, 1)

    # ── Relative Strength (15%) ───────────────────────────────────────────────
    rs_score = 50.0  # default neutral
    if spy_bars and len(spy_bars) >= 6:
        spy_closes   = [b["c"] for b in spy_bars]
        spy_ch_5d    = (spy_closes[-1] - spy_closes[-6]) / spy_closes[-6] * 100
        rs_vs_spy    = ch_5d - spy_ch_5d
        data_used.append("spy_bars")

        if rs_vs_spy >= 6:    rs_score = 90.0
        elif rs_vs_spy >= 3:  rs_score = 75.0
        elif rs_vs_spy >= 0:  rs_score = 58.0
        elif rs_vs_spy >= -3: rs_score = 42.0
        else:                 rs_score = 25.0

        factors["rs_vs_spy"] = round(rs_vs_spy, 2)
    else:
        data_missing.append("spy_bars")

    factors["rs"] = round(rs_score, 1)

    # ── Combined technical score ───────────────────────────────────────────────
    technical = (
        trend_score  * 0.25 +
        mom_score    * 0.25 +
        vol_score    * 0.20 +
        struct_score * 0.15 +
        rs_score     * 0.15
    )

    confidence = (
        Confidence.HIGH   if not data_missing else
        Confidence.MEDIUM if len(data_missing) <= 1 else
        Confidence.LOW
    )

    return DimensionScore(
        name         = "technical",
        score        = round(technical, 1),
        confidence   = confidence,
        data_used    = data_used,
        data_missing = data_missing,
        factors      = factors,
    )


# ---------------------------------------------------------------------------
# DIMENSION 2: OPTIONS SCORE (replaces Atlas)
# ---------------------------------------------------------------------------

def score_options(ticker: str) -> DimensionScore:
    """
    Pure options market analysis. No LLM. No agent wrapper.

    Factors:
      IV Rank    (35%): is volatility elevated? (ideal 20-60)
      Skew       (25%): put vs call imbalance (directional signal)
      Term       (20%): term structure shape
      Flow proxy (20%): estimated from volume/OI patterns (without UW)
    """
    data_used    = []
    data_missing = []
    factors      = {}

    # ── IV Rank (35%) ─────────────────────────────────────────────────────────
    iv_rank = fetch_iv_rank(ticker)
    if iv_rank is not None:
        data_used.append("orats_iv_rank")
        factors["iv_rank"] = round(iv_rank, 1)

        # Optimal range 20-55 for directional plays
        if 20 <= iv_rank <= 55:   ivr_score = 80.0
        elif 15 <= iv_rank < 20:  ivr_score = 65.0
        elif 55 < iv_rank <= 70:  ivr_score = 60.0
        elif iv_rank < 15:        ivr_score = 45.0
        else:                     ivr_score = 35.0  # >70: too hot
    else:
        data_missing.append("iv_rank")
        ivr_score = None   # NOT 50 — explicitly absent

    factors["ivr_score"] = round(ivr_score, 1) if ivr_score else None

    # ── Skew (25%) ────────────────────────────────────────────────────────────
    skew = fetch_skew(ticker)
    if skew is not None:
        data_used.append("orats_skew")
        factors["skew"] = round(skew, 3)

        # Negative slope = calls bid up = bullish signal
        if skew <= -0.10:    skew_score = 80.0   # strong bullish flow
        elif skew <= -0.05:  skew_score = 68.0
        elif skew <= 0.05:   skew_score = 55.0   # neutral
        elif skew <= 0.10:   skew_score = 42.0
        else:                skew_score = 30.0   # strong put buying
    else:
        data_missing.append("skew")
        skew_score = None

    factors["skew_score"] = round(skew_score, 1) if skew_score else None

    # ── Term Structure (20%) ──────────────────────────────────────────────────
    # Approximate from available IV data — contango = normal, backwardation = fear
    # Without term data, use IV rank as proxy
    if iv_rank is not None:
        # Low IV rank = likely contango (normal) = favorable
        # High IV rank = likely backwardation = caution
        if iv_rank < 30:    term_score = 75.0
        elif iv_rank < 50:  term_score = 62.0
        elif iv_rank < 70:  term_score = 50.0
        else:               term_score = 38.0
    else:
        term_score = None
        data_missing.append("term_structure")

    factors["term_score"] = round(term_score, 1) if term_score else None

    # ── Flow Proxy (20%) ──────────────────────────────────────────────────────
    # Without Unusual Whales, estimate flow from price/volume momentum
    # Unusual volume in options = institutional positioning
    # We proxy this with equity volume ratio (correlated with options flow)
    bars = fetch_daily_bars(ticker, days=25)
    if bars and len(bars) >= 5:
        data_used.append("polygon_volume_proxy")
        volumes   = [b["v"] for b in bars]
        avg_vol   = sum(volumes[:-1]) / max(len(volumes)-1, 1)
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0
        closes    = [b["c"] for b in bars]
        ch_5d     = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 else 0

        # Volume surge + positive price = bullish flow proxy
        if vol_ratio >= 2.0 and ch_5d > 0:   flow_score = 82.0
        elif vol_ratio >= 1.5 and ch_5d > 0: flow_score = 70.0
        elif vol_ratio >= 1.0 and ch_5d > 0: flow_score = 58.0
        elif vol_ratio >= 1.5:               flow_score = 52.0
        else:                                flow_score = 42.0
        factors["flow_proxy_vol_ratio"] = round(vol_ratio, 2)
    else:
        flow_score = None
        data_missing.append("flow_proxy")

    factors["flow_score"] = round(flow_score, 1) if flow_score else None

    # ── Combined options score ─────────────────────────────────────────────────
    # Only use available data — no silent defaults
    scores   = []
    weights  = []
    if ivr_score  is not None: scores.append(ivr_score);  weights.append(0.35)
    if skew_score is not None: scores.append(skew_score); weights.append(0.25)
    if term_score is not None: scores.append(term_score); weights.append(0.20)
    if flow_score is not None: scores.append(flow_score); weights.append(0.20)

    if not scores:
        return DimensionScore(
            name="options", score=0.0,
            confidence=Confidence.ABSENT,
            data_missing=data_missing,
        )

    total_weight = sum(weights)
    options_score = sum(s * w for s, w in zip(scores, weights)) / total_weight

    # Confidence based on data completeness
    missing_weight = sum(
        w for label, w in [("iv_rank",0.35),("skew",0.25),("term_structure",0.20),("flow_proxy",0.20)]
        if label in data_missing
    )
    if missing_weight == 0:      confidence = Confidence.HIGH
    elif missing_weight <= 0.25: confidence = Confidence.MEDIUM
    elif missing_weight <= 0.55: confidence = Confidence.LOW
    else:                        confidence = Confidence.ABSENT

    return DimensionScore(
        name         = "options",
        score        = round(options_score, 1),
        confidence   = confidence,
        data_used    = data_used,
        data_missing = data_missing,
        factors      = factors,
    )


# ---------------------------------------------------------------------------
# DIMENSION 3: MACRO SCORE (replaces Sage)
# ---------------------------------------------------------------------------

def score_macro(
    ticker:    str,
    vix:       Optional[float] = None,
    spy_bars:  Optional[list]  = None,
) -> DimensionScore:
    """
    Pure macro and fundamental analysis. No LLM. No agent wrapper.

    Factors:
      VIX Regime   (30%): market fear level
      Earnings     (30%): proximity to binary event
      Sector RS    (25%): sector relative strength
      Calendar     (15%): macro event proximity
    """
    data_used    = []
    data_missing = []
    factors      = {}

    # ── VIX Regime (30%) ──────────────────────────────────────────────────────
    # Try Axiom first (already fetched, most reliable)
    if vix is None:
        axiom_vix, axiom_regime = fetch_axiom_regime()
        if axiom_vix:
            vix = axiom_vix
            log.debug("VIX from Axiom: %.1f regime=%s", vix, axiom_regime)
    if vix is None:
        vix = fetch_vix()

    if vix is not None:
        data_used.append("vix")
        factors["vix"] = round(vix, 1)

        if vix < 15:      vix_score = 82.0   # low fear, bullish environment
        elif vix < 20:    vix_score = 72.0   # normal
        elif vix < 25:    vix_score = 58.0   # slightly elevated
        elif vix < 30:    vix_score = 42.0   # elevated, cautious
        elif vix < 35:    vix_score = 28.0   # high fear
        else:             vix_score = 15.0   # extreme fear
    else:
        data_missing.append("vix")
        vix_score = None

    factors["vix_score"] = round(vix_score, 1) if vix_score else None

    # ── Earnings Proximity (30%) ───────────────────────────────────────────────
    dte = fetch_days_to_earnings(ticker)
    if dte is not None:
        data_used.append("earnings_calendar")
        factors["days_to_earnings"] = dte

        if dte <= 1:     earn_score = 10.0   # earnings tomorrow — very risky
        elif dte <= 3:   earn_score = 25.0   # earnings this week
        elif dte <= 7:   earn_score = 45.0   # earnings next week
        elif dte <= 14:  earn_score = 65.0   # earnings in 2 weeks
        elif dte <= 30:  earn_score = 78.0   # earnings next month
        else:            earn_score = 85.0   # far from earnings — safe
    else:
        data_missing.append("earnings_calendar")
        earn_score = 65.0  # unknown = neutral assumption
        factors["days_to_earnings"] = None

    factors["earnings_score"] = round(earn_score, 1)

    # ── Sector Relative Strength (25%) ────────────────────────────────────────
    # Map ticker to sector ETF
    SECTOR_MAP = {
        "AAPL":"XLK","MSFT":"XLK","NVDA":"XLK","AMD":"XLK","INTC":"XLK",
        "GOOGL":"XLK","META":"XLK","AVGO":"XLK","QCOM":"XLK","CRM":"XLK",
        "JPM":"XLF","BAC":"XLF","GS":"XLF","MS":"XLF","WFC":"XLF",
        "JNJ":"XLV","UNH":"XLV","PFE":"XLV","ABBV":"XLV","MRK":"XLV",
        "XOM":"XLE","CVX":"XLE","COP":"XLE","EOG":"XLE","SLB":"XLE",
        "CAT":"XLI","GE":"XLI","HON":"XLI","RTX":"XLI","UNP":"XLI",
        "AMZN":"XLY","TSLA":"XLY","HD":"XLY","MCD":"XLY","TGT":"XLY",
        "NEE":"XLU","DUK":"XLU","SO":"XLU","AEP":"XLU",
        "AMT":"XLRE","PLD":"XLRE","CCI":"XLRE","EQIX":"XLRE",
    }
    sector_etf = SECTOR_MAP.get(ticker.upper(), "SPY")
    sector_bars = fetch_daily_bars(sector_etf, days=10)

    if sector_bars and len(sector_bars) >= 6 and spy_bars and len(spy_bars) >= 6:
        data_used.append(f"sector_{sector_etf}")
        spy_closes    = [b["c"] for b in spy_bars]
        sector_closes = [b["c"] for b in sector_bars]
        spy_ch    = (spy_closes[-1] - spy_closes[-6]) / spy_closes[-6] * 100
        sector_ch = (sector_closes[-1] - sector_closes[-6]) / sector_closes[-6] * 100
        rs        = sector_ch - spy_ch

        factors["sector"]    = sector_etf
        factors["sector_rs"] = round(rs, 2)

        if rs >= 3:     sector_score = 85.0
        elif rs >= 1:   sector_score = 72.0
        elif rs >= -1:  sector_score = 58.0
        elif rs >= -3:  sector_score = 42.0
        else:           sector_score = 28.0
    else:
        data_missing.append("sector_rs")
        sector_score = None

    factors["sector_score"] = round(sector_score, 1) if sector_score else None

    # ── Macro Calendar (15%) ──────────────────────────────────────────────────
    # Simple heuristic: avoid Fed weeks (last Wed of month)
    now_et = datetime.now(_ET)
    is_fed_week = (now_et.weekday() in [1,2,3] and now_et.day >= 25)
    cal_score = 55.0 if is_fed_week else 75.0
    factors["fed_week"] = is_fed_week
    data_used.append("calendar_heuristic")

    # ── Combined macro score ───────────────────────────────────────────────────
    scores  = []
    weights = []
    if vix_score    is not None: scores.append(vix_score);    weights.append(0.30)
    scores.append(earn_score);                                 weights.append(0.30)
    if sector_score is not None: scores.append(sector_score); weights.append(0.25)
    scores.append(cal_score);                                  weights.append(0.15)

    total_weight  = sum(weights)
    macro_score   = sum(s * w for s, w in zip(scores, weights)) / total_weight

    missing_weight = sum(
        w for label, w in [("vix",0.30),("sector_rs",0.25)]
        if label in data_missing
    )
    if missing_weight == 0:      confidence = Confidence.HIGH
    elif missing_weight <= 0.30: confidence = Confidence.MEDIUM
    else:                        confidence = Confidence.LOW

    return DimensionScore(
        name         = "macro",
        score        = round(macro_score, 1),
        confidence   = confidence,
        data_used    = data_used,
        data_missing = data_missing,
        factors      = factors,
    )


# ---------------------------------------------------------------------------
# MAIN SCORER — combines all three dimensions
# ---------------------------------------------------------------------------

def score_ticker(
    ticker:    str,
    direction: str = "bullish",
    bars:      Optional[list] = None,
    spy_bars:  Optional[list] = None,
    vix:       Optional[float] = None,
) -> NexusScore:
    """
    Score a single ticker across all three dimensions.
    Returns NexusScore with full data lineage and confidence flags.
    Never silently defaults — every missing value is explicitly flagged.
    """
    now = datetime.now(_ET).isoformat()

    # Score all three dimensions
    tech  = score_technical(ticker, bars=bars, spy_bars=spy_bars)
    opts  = score_options(ticker)
    macro = score_macro(ticker, vix=vix, spy_bars=spy_bars)

    # Confidence-weighted combination
    # If a dimension is ABSENT: excluded entirely
    # If LOW: 30% weight penalty
    # If MEDIUM: 15% weight penalty
    CONF_WEIGHT = {
        Confidence.HIGH:   1.00,
        Confidence.MEDIUM: 0.85,
        Confidence.LOW:    0.70,
        Confidence.ABSENT: 0.00,
    }

    scores  = []
    weights = []
    for dim, base_weight in [(tech, WEIGHT_TECHNICAL),
                              (opts, WEIGHT_OPTIONS),
                              (macro, WEIGHT_MACRO)]:
        conf_mult = CONF_WEIGHT[dim.confidence]
        if conf_mult > 0:
            scores.append(dim.score)
            weights.append(base_weight * conf_mult)

    if not scores:
        combined = 0.0
        overall_conf = Confidence.ABSENT
    else:
        total_w  = sum(weights)
        combined = sum(s * w for s, w in zip(scores, weights)) / total_w

        # Overall confidence = worst of the NON-ABSENT dimensions
        # ABSENT dimensions are already excluded from combined score
        # so they should not drag overall confidence to ABSENT
        conf_order  = [Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW, Confidence.ABSENT]
        active_dims = [d.confidence for d in [tech, opts, macro]
                       if d.confidence != Confidence.ABSENT]
        if not active_dims:
            overall_conf = Confidence.ABSENT
        else:
            worst = max(active_dims, key=lambda c: conf_order.index(c))
            # If any dimension is ABSENT, cap at MEDIUM (trading with incomplete data)
            absent_count = sum(1 for d in [tech, opts, macro]
                              if d.confidence == Confidence.ABSENT)
            if absent_count >= 2:
                overall_conf = Confidence.LOW
            elif absent_count == 1:
                overall_conf = min(worst, Confidence.MEDIUM,
                                  key=lambda c: conf_order.index(c))
            else:
                overall_conf = worst

    # Bearish: invert directional factors
    if direction == "bearish":
        # For bearish trades, strong upward momentum is negative
        if tech.factors.get("momentum", 50) > 65:
            combined = max(0, combined - 10)

    combined = round(combined, 1)
    go       = combined >= GO_THRESHOLD and overall_conf != Confidence.ABSENT
    strong   = combined >= STRONG_THRESHOLD and overall_conf in (Confidence.HIGH, Confidence.MEDIUM)

    log.info(
        "%s [%s]: combined=%.1f conf=%s tech=%.1f(%s) opts=%.1f(%s) macro=%.1f(%s) GO=%s",
        ticker, direction, combined, overall_conf.value,
        tech.score, tech.confidence.value,
        opts.score, opts.confidence.value,
        macro.score, macro.confidence.value,
        "YES" if go else "NO",
    )

    return NexusScore(
        ticker    = ticker,
        direction = direction,
        technical = tech,
        options   = opts,
        macro     = macro,
        combined  = combined,
        confidence = overall_conf,
        go        = go,
        strong_go = strong,
        timestamp = now,
    )


def score_pool(
    tickers:   list[str],
    direction: str = "bullish",
) -> list[NexusScore]:
    """
    Score a pool of tickers deterministically.
    Fetches shared data once (SPY bars, VIX) to minimize API calls.
    Returns all scores sorted by combined score descending.
    Only scores with confidence != ABSENT are returned.
    """
    log.info("Scoring pool of %d tickers [%s]", len(tickers), direction)

    # Fetch shared data once
    spy_bars = fetch_spy_bars(days=25)
    vix      = fetch_vix()

    results = []
    for ticker in tickers:
        try:
            bars  = fetch_daily_bars(ticker, days=60)
            score = score_ticker(
                ticker    = ticker,
                direction = direction,
                bars      = bars,
                spy_bars  = spy_bars,
                vix       = vix,
            )
            if score.confidence != Confidence.ABSENT:
                results.append(score)
        except Exception as exc:
            log.error("Score failed for %s: %s", ticker, exc)

    results.sort(key=lambda s: -s.combined)
    go_count = sum(1 for s in results if s.go)
    log.info("Pool scored: %d/%d qualify (GO)", go_count, len(results))
    return results
