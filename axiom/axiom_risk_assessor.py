"""
axiom_risk_assessor.py — Deterministic 10-Layer Risk Assessment Engine
Nexus V1 | Axiom Agent

Axiom sits between the screening agents and OMNI.
Every pick submitted by Cipher, Sage, or Atlas passes through
all 10 layers before reaching OMNI's concordance engine.

AXIOM'S ROLE IN V1:
  1. Receive pick from agent via /assess
  2. Run all 10 risk layers deterministically
  3. Return risk_score + flags + sizing_mult to OMNI
  4. Never vetoes — informs. OMNI decides.

THE 10 LAYERS:
  Layer 1:  Liquidity          Volume ≥ 500K avg daily, price $10–$1,500
  Layer 2:  IV Environment     IVR suitability for strategy type
  Layer 3:  Earnings Proximity Days to next earnings — flag and penalize
  Layer 4:  Sector Concentration Max exposure per sector in open positions
  Layer 5:  Correlation        Overlap with existing open positions
  Layer 6:  Gamma Proximity    Distance from major gamma walls
  Layer 7:  Dividend           Ex-dividend within DTE window
  Layer 8:  Macro Alignment    Pick direction vs current regime
  Layer 9:  Technical Floor    Minimum technical quality bar
  Layer 10: Circuit Breaker    VIX, drawdown, account-level hard stops

OUTPUT to OMNI:
  risk_score     0–100  (0 = clean, 100 = maximum risk)
  sizing_mult    0.25–1.0 (position sizing adjustment)
  flags          List of risk warnings (non-blocking)
  hard_stops     List of hard blocks (OMNI should reject)
  passed         True if no hard stops triggered

All layers are pure rules — no LLM judgment anywhere in this module.
"""

import os
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger("axiom.risk")

# ── Configuration ──────────────────────────────────────────────────────────────

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "ZMEnZ_2GURw_UqbufU5npd49ZDeptMhl")
POLYGON_BASE    = "https://api.polygon.io"

ORATS_TOKEN     = os.getenv("ORATS_TOKEN", os.getenv("ORATS_API_KEY", ""))
ORATS_BASE      = "https://api.orats.io/datav2"

FRED_API_KEY    = os.getenv("FRED_API_KEY", "")
FRED_BASE       = "https://api.stlouisfed.org/fred"

NEXUS_SECRET    = os.getenv("NEXUS_SECRET",
    "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
ALPACA_BASE     = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_KEY      = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET   = os.getenv("ALPACA_SECRET_KEY", "")

# ── Layer Thresholds ───────────────────────────────────────────────────────────

# Layer 1: Liquidity
L1_MIN_AVG_VOLUME  = 500_000    # Minimum average daily volume
L1_MIN_PRICE       = 10.0       # Minimum price
L1_MAX_PRICE       = 1_500.0    # Maximum price

# Layer 2: IV Environment
L2_IVR_DEBIT_MAX   = 55.0       # IVR above this → no debit spreads
L2_IVR_NEUTRAL_MIN = 75.0       # IVR above this → neutral only

# Layer 3: Earnings
L3_HARD_BLOCK_DAYS = 2          # Hard stop: earnings within 2 days
L3_WARN_DAYS       = 14         # Warning: earnings within 14 days
L3_PENALTY_5D      = 25         # Risk score penalty: earnings ≤ 5 days
L3_PENALTY_14D     = 12         # Risk score penalty: earnings ≤ 14 days

# Layer 4: Sector Concentration
L4_MAX_SECTOR_PCT  = 0.40       # Max 40% portfolio in one sector

# Layer 5: Correlation
L5_HIGH_CORR       = 0.85       # Flag if correlated > 85% to open position

# Layer 6: Gamma
L6_WALL_PCT        = 0.02       # Flag if within 2% of gamma wall

# Layer 7: Dividend
L7_EX_DIV_DAYS     = 5          # Flag if ex-div within 5 days of DTE

# Layer 8: Macro
L8_VIX_CAUTION     = 25.0       # Flag if VIX > 25
L8_VIX_HARD_BLOCK  = 40.0       # Hard stop if VIX ≥ 40

# Layer 9: Technical Floor
L9_MIN_SCORE       = 40.0       # Minimum technical score to proceed

# Layer 10: Circuit Breaker
L10_DAILY_LOSS_PCT = 0.05       # Halt if daily portfolio loss > 5%
L10_MAX_POSITIONS  = 20         # Flag if open positions ≥ 20


# ── Sector Map ─────────────────────────────────────────────────────────────────

SECTOR_MAP = {
    "AAPL":"XLK","MSFT":"XLK","NVDA":"XLK","GOOGL":"XLK","GOOG":"XLK",
    "META":"XLK","AMAT":"XLK","LRCX":"XLK","KEYS":"XLK","DELL":"XLK",
    "JPM":"XLF","GS":"XLF","MS":"XLF","AXP":"XLF","BAC":"XLF","WFC":"XLF",
    "XOM":"XLE","CVX":"XLE","COP":"XLE","VLO":"XLE","PSX":"XLE","MPC":"XLE",
    "UNH":"XLV","JNJ":"XLV","PFE":"XLV","ISRG":"XLV","HCA":"XLV","TMO":"XLV",
    "AMZN":"XLY","TSLA":"XLY","HD":"XLY","MCD":"XLY","BKNG":"XLY","RCL":"XLY",
    "HON":"XLI","UPS":"XLI","GE":"XLI","ETN":"XLI","SAIC":"XLI",
    "LIN":"XLB","APD":"XLB","WDC":"XLB",
    "NFLX":"XLC","DIS":"XLC","T":"XLC","VZ":"XLC",
    "AMT":"XLRE","PLD":"XLRE","AVB":"XLRE",
    "PG":"XLP","KO":"XLP","WMT":"XLP","COST":"XLP",
    "GLD":"COMMODITY","SLV":"COMMODITY","TLT":"BONDS","IWM":"ETF",
}


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class LayerResult:
    """Result of a single risk layer."""
    name:        str
    passed:      bool   = True
    hard_stop:   bool   = False
    risk_delta:  float  = 0.0    # Risk score contribution (0–25)
    size_delta:  float  = 0.0    # Sizing multiplier reduction
    flag:        str    = ""
    detail:      str    = ""


@dataclass
class RiskAssessment:
    """
    Complete 10-layer risk assessment result.
    Passed to OMNI as context alongside the pick.
    OMNI makes the final GO/NO-GO — Axiom informs, not vetoes.
    """
    ticker:       str
    direction:    str           = "bullish"
    agent:        str           = "unknown"
    score:        float         = 0.0         # Agent's confidence score
    passed:       bool          = True         # No hard stops triggered
    risk_score:   float         = 0.0          # 0=clean, 100=max risk
    sizing_mult:  float         = 1.0          # Position size adjustment
    flags:        list          = field(default_factory=list)    # Warnings
    hard_stops:   list          = field(default_factory=list)    # Blockers
    layers:       dict          = field(default_factory=dict)    # Layer detail
    assessed_at:  str           = field(default_factory=lambda: datetime.utcnow().isoformat())


# ── Data Fetching ──────────────────────────────────────────────────────────────

def _fetch_bars(ticker: str, days: int = 30) -> list:
    """Fetch daily OHLCV bars from Polygon."""
    end   = datetime.now()
    start = end - timedelta(days=int(days * 1.6))
    url   = (
        f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/"
        f"{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
        f"?adjusted=true&sort=asc&limit=60&apiKey={POLYGON_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            return r.json().get("results", [])
    except Exception as e:
        logger.debug("Polygon %s: %s", ticker, e)
    return []


def _get_vix() -> Optional[float]:
    """Current VIX level."""
    bars = _fetch_bars("I:VIX", days=5)
    return bars[-1]["c"] if bars else None


def _get_orats_summary(ticker: str) -> Optional[dict]:
    """ORATS summary data (IV rank, put/call ratio, earnings)."""
    if not ORATS_TOKEN:
        return None
    try:
        r = requests.get(
            f"{ORATS_BASE}/summaries?token={ORATS_TOKEN}&ticker={ticker}",
            timeout=8
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            return data[0] if data else None
    except Exception as e:
        logger.debug("ORATS %s: %s", ticker, e)
    return None


def _get_earnings_dte(ticker: str, orats: Optional[dict] = None) -> Optional[int]:
    """Days until next earnings."""
    if orats and orats.get("nextEarnDate"):
        try:
            earn_dt = datetime.strptime(orats["nextEarnDate"], "%Y-%m-%d")
            return (earn_dt - datetime.now()).days
        except Exception:
            pass

    if not ORATS_TOKEN:
        return None
    try:
        r = requests.get(
            f"{ORATS_BASE}/earnings?token={ORATS_TOKEN}&ticker={ticker}",
            timeout=8
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data and data[0].get("nextEarnDate"):
                earn_dt = datetime.strptime(data[0]["nextEarnDate"], "%Y-%m-%d")
                return (earn_dt - datetime.now()).days
    except Exception:
        pass
    return None


def _get_alpaca_positions() -> list:
    """Fetch current open positions from Alpaca."""
    if not ALPACA_KEY:
        return []
    try:
        r = requests.get(
            f"{ALPACA_BASE}/v2/positions",
            headers={
                "APCA-API-KEY-ID":     ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            timeout=8
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("Alpaca positions: %s", e)
    return []


def _get_alpaca_account() -> Optional[dict]:
    """Fetch Alpaca account info."""
    if not ALPACA_KEY:
        return None
    try:
        r = requests.get(
            f"{ALPACA_BASE}/v2/account",
            headers={
                "APCA-API-KEY-ID":     ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            timeout=8
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("Alpaca account: %s", e)
    return None


def _get_fred(series_id: str) -> Optional[float]:
    """Latest FRED series value."""
    if not FRED_API_KEY:
        return None
    try:
        r = requests.get(
            f"{FRED_BASE}/series/observations"
            f"?series_id={series_id}&api_key={FRED_API_KEY}"
            f"&file_type=json&limit=5&sort_order=desc",
            timeout=8
        )
        if r.status_code == 200:
            for obs in r.json().get("observations", []):
                if obs.get("value") not in (".", None, ""):
                    return float(obs["value"])
    except Exception:
        pass
    return None


# ── The 10 Layers ──────────────────────────────────────────────────────────────

def _layer1_liquidity(ticker: str, bars: list) -> LayerResult:
    """
    Layer 1: Liquidity
    Hard stops: insufficient volume, price out of range.
    """
    layer = LayerResult(name="L1_liquidity")

    if not bars:
        layer.passed    = False
        layer.hard_stop = True
        layer.flag      = "NO_PRICE_DATA"
        layer.detail    = "No price data available from Polygon"
        layer.risk_delta = 25.0
        return layer

    vols     = [b["v"] for b in bars]
    avg_vol  = sum(vols[-20:]) / min(20, len(vols))
    price    = bars[-1]["c"]

    if price < L1_MIN_PRICE:
        layer.passed    = False
        layer.hard_stop = True
        layer.flag      = "PRICE_TOO_LOW"
        layer.detail    = f"Price ${price:.2f} below minimum ${L1_MIN_PRICE}"
        layer.risk_delta = 20.0

    elif price > L1_MAX_PRICE:
        layer.passed    = False
        layer.hard_stop = True
        layer.flag      = "PRICE_TOO_HIGH"
        layer.detail    = f"Price ${price:.2f} above maximum ${L1_MAX_PRICE}"
        layer.risk_delta = 10.0

    elif avg_vol < L1_MIN_AVG_VOLUME:
        layer.passed    = False
        layer.hard_stop = True
        layer.flag      = "LOW_LIQUIDITY"
        layer.detail    = f"Avg volume {avg_vol:,.0f} below minimum {L1_MIN_AVG_VOLUME:,.0f}"
        layer.risk_delta = 20.0

    else:
        # Volume quality bonus/penalty
        if avg_vol >= 5_000_000:
            layer.detail = f"High liquidity: {avg_vol/1e6:.1f}M avg vol"
        elif avg_vol >= 1_000_000:
            layer.detail = f"Good liquidity: {avg_vol/1e6:.1f}M avg vol"
        else:
            layer.flag       = "THIN_VOLUME"
            layer.risk_delta = 5.0
            layer.size_delta = 0.1
            layer.detail     = f"Thin volume: {avg_vol:,.0f} avg (above min but low)"

    return layer


def _layer2_iv_environment(
    ticker:    str,
    direction: str,
    orats:     Optional[dict],
) -> LayerResult:
    """
    Layer 2: IV Environment
    IVR suitability for the trade direction and strategy type.
    """
    layer = LayerResult(name="L2_iv_environment")

    if orats is None:
        layer.flag   = "NO_IV_DATA"
        layer.detail = "ORATS unavailable — IV layer skipped (fail-open)"
        layer.risk_delta = 5.0
        return layer

    ivr = orats.get("ivRank", 0)
    layer.detail = f"IVR={ivr:.0f}"

    if ivr >= L2_IVR_NEUTRAL_MIN:
        # IVR > 75: only neutral/credit trades — directional is risky
        if direction in ("bullish", "bearish"):
            layer.flag       = "HIGH_IVR_DIRECTIONAL"
            layer.risk_delta = 15.0
            layer.size_delta = 0.25
            layer.detail     = f"IVR={ivr:.0f} > {L2_IVR_NEUTRAL_MIN} — directional risky, prefer neutral"

    elif ivr >= L2_IVR_DEBIT_MAX:
        # IVR 55–75: caution on debit
        layer.flag       = "ELEVATED_IVR"
        layer.risk_delta = 8.0
        layer.size_delta = 0.10
        layer.detail     = f"IVR={ivr:.0f} — elevated, credit structures preferred"

    elif ivr < 15:
        # Very low IV — cheap premiums but limited edge
        layer.flag       = "LOW_IVR"
        layer.risk_delta = 5.0
        layer.detail     = f"IVR={ivr:.0f} — very low IV, limited premium edge"

    else:
        layer.detail = f"IVR={ivr:.0f} — suitable for directional trade"

    return layer


def _layer3_earnings(
    ticker:    str,
    direction: str,
    orats:     Optional[dict],
) -> LayerResult:
    """
    Layer 3: Earnings Proximity
    Hard stop if earnings within 2 days.
    Flag and penalize if within 14 days.
    """
    layer = LayerResult(name="L3_earnings")

    dte = _get_earnings_dte(ticker, orats)

    if dte is None:
        layer.flag   = "EARNINGS_UNKNOWN"
        layer.detail = "Earnings date unavailable — flagged"
        layer.risk_delta = 8.0
        return layer

    if dte < 0:
        # Earnings just passed
        layer.detail = f"Earnings passed {abs(dte)}d ago — IV crush risk"
        layer.risk_delta = 5.0
        return layer

    if dte <= L3_HARD_BLOCK_DAYS:
        layer.passed    = False
        layer.hard_stop = True
        layer.flag      = "EARNINGS_IMMINENT"
        layer.detail    = f"Earnings in {dte}d — hard stop (< {L3_HARD_BLOCK_DAYS}d)"
        layer.risk_delta = 25.0

    elif dte <= 5:
        layer.flag       = "EARNINGS_THIS_WEEK"
        layer.risk_delta = L3_PENALTY_5D
        layer.size_delta = 0.25
        layer.detail     = f"Earnings in {dte}d — significant risk, size reduced"

    elif dte <= L3_WARN_DAYS:
        layer.flag       = "EARNINGS_APPROACHING"
        layer.risk_delta = L3_PENALTY_14D
        layer.size_delta = 0.10
        layer.detail     = f"Earnings in {dte}d — flagged"

    else:
        layer.detail = f"Earnings in {dte}d — safe window"

    return layer


def _layer4_sector_concentration(
    ticker:     str,
    positions:  list,
) -> LayerResult:
    """
    Layer 4: Sector Concentration
    Flag if adding this pick would exceed 40% in one sector.
    """
    layer  = LayerResult(name="L4_sector_concentration")
    sector = SECTOR_MAP.get(ticker.upper(), "UNKNOWN")

    if not positions:
        layer.detail = f"No open positions — sector concentration clear ({sector})"
        return layer

    total_val     = sum(abs(float(p.get("market_value", 0))) for p in positions)
    sector_val    = sum(
        abs(float(p.get("market_value", 0)))
        for p in positions
        if SECTOR_MAP.get(p.get("symbol", "").upper(), "UNKNOWN") == sector
    )

    if total_val > 0:
        sector_pct = (sector_val / total_val)
        if sector_pct >= L4_MAX_SECTOR_PCT:
            layer.flag       = "SECTOR_CONCENTRATED"
            layer.risk_delta = 12.0
            layer.size_delta = 0.25
            layer.detail     = (
                f"{sector} at {sector_pct:.0%} of portfolio "
                f"(max {L4_MAX_SECTOR_PCT:.0%})"
            )
        else:
            layer.detail = f"{sector} at {sector_pct:.0%} — within limits"
    else:
        layer.detail = f"Sector: {sector} — first position"

    return layer


def _layer5_correlation(
    ticker:    str,
    direction: str,
    positions: list,
    bars:      list,
) -> LayerResult:
    """
    Layer 5: Correlation
    Flag if new pick is highly correlated with an existing open position
    in the same direction (doubling up on the same bet).
    """
    layer = LayerResult(name="L5_correlation")

    if not positions or not bars or len(bars) < 20:
        layer.detail = "Correlation check skipped — insufficient data"
        return layer

    # Get current pick's recent returns
    ticker_closes  = [b["c"] for b in bars[-21:]]
    ticker_returns = [
        ticker_closes[i] / ticker_closes[i-1] - 1
        for i in range(1, len(ticker_closes))
    ]

    correlated_positions = []

    for pos in positions:
        sym  = pos.get("symbol", "")
        side = pos.get("side", "long")
        if sym == ticker:
            continue

        pos_bars = _fetch_bars(sym, days=25)
        if len(pos_bars) < 21:
            continue

        pos_closes  = [b["c"] for b in pos_bars[-21:]]
        pos_returns = [
            pos_closes[i] / pos_closes[i-1] - 1
            for i in range(1, len(pos_closes))
        ]

        n = min(len(ticker_returns), len(pos_returns))
        if n < 10:
            continue

        # Pearson correlation
        tr = ticker_returns[-n:]
        pr = pos_returns[-n:]
        mean_t = sum(tr) / n
        mean_p = sum(pr) / n
        cov    = sum((tr[i] - mean_t) * (pr[i] - mean_p) for i in range(n)) / n
        std_t  = (sum((x - mean_t)**2 for x in tr) / n) ** 0.5
        std_p  = (sum((x - mean_p)**2 for x in pr) / n) ** 0.5

        if std_t == 0 or std_p == 0:
            continue

        corr = cov / (std_t * std_p)

        # Same direction + high correlation = doubling up
        pos_direction = "bullish" if side == "long" else "bearish"
        if corr > L5_HIGH_CORR and pos_direction == direction:
            correlated_positions.append((sym, corr))

    if correlated_positions:
        sym, corr = correlated_positions[0]
        layer.flag       = "HIGH_CORRELATION"
        layer.risk_delta = 10.0
        layer.size_delta = 0.15
        layer.detail     = f"Corr {corr:.2f} with {sym} in same direction — doubling up risk"
    else:
        layer.detail = "No significant correlation with existing positions"

    return layer


def _layer6_gamma_proximity(ticker: str, bars: list) -> LayerResult:
    """
    Layer 6: Gamma Wall Proximity
    Flag if price is within L6_WALL_PCT of a major gamma wall.
    Uses round number proxy when SpotGamma unavailable.
    """
    layer = LayerResult(name="L6_gamma_proximity")

    if not bars:
        layer.detail = "No price data — gamma check skipped"
        return layer

    price = bars[-1]["c"]

    # Round number gamma walls (institutional hedging concentrates here)
    walls = []
    for mult in [25, 50, 100, 250, 500]:
        wall = round(price / mult) * mult
        if wall != price:
            walls.append(wall)

    nearest_wall = min(walls, key=lambda w: abs(w - price))
    dist_pct     = abs(price - nearest_wall) / price

    if dist_pct <= L6_WALL_PCT:
        layer.flag       = "NEAR_GAMMA_WALL"
        layer.risk_delta = 8.0
        layer.size_delta = 0.10
        layer.detail     = (
            f"Price ${price:.1f} within {dist_pct:.1%} of "
            f"gamma wall ${nearest_wall:.0f}"
        )
    else:
        layer.detail = (
            f"Price ${price:.1f} | nearest wall ${nearest_wall:.0f} "
            f"({dist_pct:.1%} away) — clear"
        )

    return layer


def _layer7_dividend(ticker: str, bars: list, dte_window: int = 35) -> LayerResult:
    """
    Layer 7: Dividend Proximity
    Flag if ex-dividend date falls within the options DTE window.
    Early assignment risk on short options positions.
    """
    layer  = LayerResult(name="L7_dividend")

    # Polygon dividend data
    url = (
        f"{POLYGON_BASE}/v3/reference/dividends"
        f"?ticker={ticker}&limit=1&apiKey={POLYGON_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=6)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                ex_div_str = results[0].get("ex_dividend_date", "")
                if ex_div_str:
                    ex_div = datetime.strptime(ex_div_str, "%Y-%m-%d")
                    days_to_div = (ex_div - datetime.now()).days

                    if 0 <= days_to_div <= L7_EX_DIV_DAYS:
                        layer.flag       = "EX_DIV_IMMINENT"
                        layer.risk_delta = 10.0
                        layer.size_delta = 0.15
                        layer.detail     = (
                            f"Ex-dividend in {days_to_div}d — "
                            f"early assignment risk on short options"
                        )
                        return layer

                    elif 0 <= days_to_div <= dte_window:
                        layer.flag       = "EX_DIV_IN_DTE"
                        layer.risk_delta = 5.0
                        layer.detail     = (
                            f"Ex-dividend in {days_to_div}d — "
                            f"falls within DTE window, monitor"
                        )
                        return layer

                    layer.detail = f"Ex-dividend in {days_to_div}d — outside DTE window"
                    return layer

        layer.detail = "No dividend data — likely non-dividend stock"
    except Exception as e:
        logger.debug("Dividend check %s: %s", ticker, e)
        layer.detail = "Dividend data unavailable — skipped"

    return layer


def _layer8_macro_alignment(
    ticker:    str,
    direction: str,
) -> LayerResult:
    """
    Layer 8: Macro Alignment
    Does the pick direction match the current macro regime?
    Flags contrarian picks in risk-off environments.
    """
    layer = LayerResult(name="L8_macro_alignment")

    vix      = _get_vix()
    t10y2y   = _get_fred("T10Y2Y")
    hy_spread = _get_fred("BAMLH0A0HYM2")

    # Hard block: VIX halt level
    if vix and vix >= L8_VIX_HARD_BLOCK:
        layer.passed    = False
        layer.hard_stop = True
        layer.flag      = "VIX_HALT"
        layer.detail    = f"VIX {vix:.1f} ≥ {L8_VIX_HARD_BLOCK} — system halt"
        layer.risk_delta = 25.0
        return layer

    macro_risk = 0.0
    notes      = []

    if vix:
        if vix >= 30:
            macro_risk += 20.0
            notes.append(f"VIX={vix:.1f} HIGH_VOL")
            if direction == "bullish":
                macro_risk += 8.0
                notes.append("bullish in high vol — elevated risk")
        elif vix >= L8_VIX_CAUTION:
            macro_risk += 10.0
            notes.append(f"VIX={vix:.1f} RISK_OFF")
            if direction == "bullish":
                macro_risk += 5.0
        else:
            notes.append(f"VIX={vix:.1f} OK")

    if t10y2y is not None and t10y2y < -0.5:
        macro_risk += 8.0
        notes.append(f"yield_curve={t10y2y:.2f} INVERTED")

    if hy_spread is not None and hy_spread > 5.0:
        macro_risk += 8.0
        notes.append(f"HY_spread={hy_spread:.1f}% STRESSED")

    layer.risk_delta = min(25.0, macro_risk)
    layer.detail     = " | ".join(notes) if notes else "Macro conditions normal"

    if macro_risk >= 20:
        layer.flag       = "MACRO_HEADWIND"
        layer.size_delta = 0.20

    elif macro_risk >= 10:
        layer.flag       = "MACRO_CAUTION"
        layer.size_delta = 0.10

    return layer


def _layer9_technical_floor(
    ticker:    str,
    direction: str,
    bars:      list,
    agent_score: float,
) -> LayerResult:
    """
    Layer 9: Technical Quality Floor
    Minimum technical setup quality using price action.
    Rejects picks with agent score below floor threshold.
    """
    layer = LayerResult(name="L9_technical_floor")

    # Agent score below absolute minimum
    if agent_score < L9_MIN_SCORE:
        layer.passed    = False
        layer.hard_stop = True
        layer.flag      = "SCORE_BELOW_FLOOR"
        layer.detail    = (
            f"Agent score {agent_score:.1f} < floor {L9_MIN_SCORE} — "
            f"insufficient technical conviction"
        )
        layer.risk_delta = 15.0
        return layer

    if len(bars) < 10:
        layer.flag       = "INSUFFICIENT_BARS"
        layer.risk_delta = 5.0
        layer.detail     = "< 10 bars available — technical check limited"
        return layer

    closes = [b["c"] for b in bars]
    price  = closes[-1]

    # 10-day range position (where is price in recent range?)
    high10 = max(b["h"] for b in bars[-10:])
    low10  = min(b["l"] for b in bars[-10:])
    rng    = high10 - low10

    if rng > 0:
        range_pos = (price - low10) / rng  # 0 = at bottom, 1 = at top

        if direction == "bullish":
            if range_pos < 0.25:
                layer.flag       = "PRICE_AT_RANGE_BOTTOM"
                layer.risk_delta = 8.0
                layer.detail     = f"Price at {range_pos:.0%} of 10d range — weak bullish setup"
            elif range_pos > 0.75:
                layer.detail = f"Price at {range_pos:.0%} of 10d range — strong bullish position"
            else:
                layer.detail = f"Price at {range_pos:.0%} of 10d range — acceptable"
        else:
            if range_pos > 0.75:
                layer.flag       = "PRICE_AT_RANGE_TOP"
                layer.risk_delta = 8.0
                layer.detail     = f"Price at {range_pos:.0%} of 10d range — weak bearish setup"
            else:
                layer.detail = f"Price at {range_pos:.0%} of 10d range — acceptable bearish"

    return layer


def _layer10_circuit_breaker(positions: list, account: Optional[dict]) -> LayerResult:
    """
    Layer 10: Circuit Breaker
    Account-level hard stops — daily loss limit, position cap.
    """
    layer = LayerResult(name="L10_circuit_breaker")

    if not account:
        layer.detail = "Alpaca account data unavailable — circuit breaker skipped"
        layer.risk_delta = 5.0
        return layer

    try:
        equity         = float(account.get("equity", 0))
        last_equity    = float(account.get("last_equity", equity))
        daily_loss_pct = (equity - last_equity) / last_equity if last_equity > 0 else 0
        n_positions    = len(positions)

        if daily_loss_pct <= -L10_DAILY_LOSS_PCT:
            layer.passed    = False
            layer.hard_stop = True
            layer.flag      = "DAILY_LOSS_LIMIT"
            layer.detail    = (
                f"Daily P&L {daily_loss_pct:.1%} exceeds "
                f"-{L10_DAILY_LOSS_PCT:.0%} limit — circuit breaker open"
            )
            layer.risk_delta = 25.0
            return layer

        if n_positions >= L10_MAX_POSITIONS:
            layer.flag       = "POSITION_CAP_APPROACHING"
            layer.risk_delta = 8.0
            layer.size_delta = 0.25
            layer.detail     = (
                f"{n_positions} open positions — at capacity "
                f"(max {L10_MAX_POSITIONS})"
            )
        else:
            layer.detail = (
                f"Daily P&L: {daily_loss_pct:+.1%} | "
                f"Positions: {n_positions}/{L10_MAX_POSITIONS} — clear"
            )

    except Exception as e:
        logger.debug("Circuit breaker parse: %s", e)
        layer.detail = "Circuit breaker data parse error — skipped"
        layer.risk_delta = 3.0

    return layer


# ── Main Assessment Entry Point ────────────────────────────────────────────────

def assess(
    ticker:    str,
    direction: str,
    score:     float,
    agent:     str     = "unknown",
    strategy:  str     = "options",   # 'options' or 'swing'
) -> RiskAssessment:
    """
    Run all 10 risk layers for a submitted pick.
    Called by Axiom's /assess endpoint for every agent submission.

    Args:
        ticker:    Stock symbol
        direction: 'bullish' or 'bearish'
        score:     Agent confidence score (0–100)
        agent:     Submitting agent name
        strategy:  'options' (Alpha) or 'swing' (Prime)

    Returns:
        RiskAssessment — passed to OMNI as risk context
    """
    result = RiskAssessment(ticker=ticker, direction=direction, agent=agent, score=score)

    logger.info(
        "Assessing %s/%s score=%.1f agent=%s strategy=%s",
        ticker, direction, score, agent, strategy
    )

    # ── Fetch shared data once ──
    bars      = _fetch_bars(ticker, days=30)
    orats     = _get_orats_summary(ticker)
    positions = _get_alpaca_positions()
    account   = _get_alpaca_account()

    # ── Run all 10 layers ──
    layer_funcs = [
        ("L1",  lambda: _layer1_liquidity(ticker, bars)),
        ("L2",  lambda: _layer2_iv_environment(ticker, direction, orats)),
        ("L3",  lambda: _layer3_earnings(ticker, direction, orats)),
        ("L4",  lambda: _layer4_sector_concentration(ticker, positions)),
        ("L5",  lambda: _layer5_correlation(ticker, direction, positions, bars)),
        ("L6",  lambda: _layer6_gamma_proximity(ticker, bars)),
        ("L7",  lambda: _layer7_dividend(ticker, bars)),
        ("L8",  lambda: _layer8_macro_alignment(ticker, direction)),
        ("L9",  lambda: _layer9_technical_floor(ticker, direction, bars, score)),
        ("L10", lambda: _layer10_circuit_breaker(positions, account)),
    ]

    total_risk  = 0.0
    total_size  = 1.0

    for label, func in layer_funcs:
        try:
            layer = func()
        except Exception as e:
            logger.error("Layer %s error for %s: %s", label, ticker, e)
            layer = LayerResult(name=label, detail=f"Layer error: {e}", risk_delta=3.0)

        result.layers[layer.name] = {
            "passed":    layer.passed,
            "hard_stop": layer.hard_stop,
            "flag":      layer.flag,
            "detail":    layer.detail,
            "risk_delta": layer.risk_delta,
        }

        if layer.flag:
            result.flags.append(layer.flag)

        if layer.hard_stop:
            result.hard_stops.append(f"{layer.name}: {layer.flag} — {layer.detail}")
            result.passed = False

        total_risk += layer.risk_delta
        total_size  = max(0.1, total_size - layer.size_delta)

    # ── Final scores ──
    result.risk_score  = round(min(100.0, total_risk), 1)
    result.sizing_mult = round(total_size, 2)

    status = "PASS" if result.passed else "FAIL"
    logger.info(
        "%s/%s [%s] RiskScore=%.1f SizeMult=%.2f Flags=%s HardStops=%d",
        ticker, direction, status,
        result.risk_score, result.sizing_mult,
        result.flags, len(result.hard_stops),
    )

    return result


def assess_batch(picks: list) -> list:
    """
    Assess a batch of picks.
    picks: list of dicts with keys: ticker, direction, score, agent, strategy
    Returns list of RiskAssessment.
    """
    results = []
    for pick in picks:
        try:
            r = assess(
                ticker    = pick["ticker"],
                direction = pick.get("direction", "bullish"),
                score     = pick.get("score", 50.0),
                agent     = pick.get("agent", "unknown"),
                strategy  = pick.get("strategy", "options"),
            )
            results.append(r)
        except Exception as e:
            logger.error("Batch assess error %s: %s", pick.get("ticker"), e)
        time.sleep(0.1)
    return results


def format_assessment(r: RiskAssessment) -> str:
    """Human-readable assessment summary for Telegram/logging."""
    status = "✅ PASS" if r.passed else "❌ FAIL"
    lines  = [
        f"{status} | {r.ticker}/{r.direction} | "
        f"RiskScore={r.risk_score:.0f} | SizeMult={r.sizing_mult:.2f}x",
    ]
    if r.flags:
        lines.append(f"  Flags: {', '.join(r.flags)}")
    if r.hard_stops:
        lines.append(f"  STOPS: {' | '.join(r.hard_stops)}")
    for name, layer in r.layers.items():
        icon = "🔴" if layer["hard_stop"] else ("🟡" if layer["flag"] else "🟢")
        lines.append(f"  {icon} {name}: {layer['detail']}")
    return "\n".join(lines)


# ── Test Harness ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    # Simulate picks from Cipher/Sage/Atlas
    test_picks = [
        {"ticker": "VLO",   "direction": "bullish", "score": 88.4, "agent": "Cipher", "strategy": "options"},
        {"ticker": "AXP",   "direction": "bearish", "score": 70.2, "agent": "Sage",   "strategy": "options"},
        {"ticker": "GOOGL", "direction": "bullish", "score": 64.2, "agent": "Atlas",  "strategy": "options"},
        {"ticker": "TSLA",  "direction": "bullish", "score": 46.4, "agent": "Cipher", "strategy": "swing"},
        {"ticker": "AMAT",  "direction": "bullish", "score": 38.0, "agent": "Sage",   "strategy": "options"},
    ]

    print("\n" + "=" * 70)
    print("AXIOM 10-LAYER RISK ASSESSMENT — TEST RUN")
    print("=" * 70 + "\n")

    results = assess_batch(test_picks)

    for r in results:
        print(format_assessment(r))
        print()

    passed = sum(1 for r in results if r.passed)
    print(f"{'=' * 70}")
    print(f"Results: {passed}/{len(results)} passed | "
          f"Avg risk score: {sum(r.risk_score for r in results)/len(results):.1f}")
