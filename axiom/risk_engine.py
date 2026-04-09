"""
Axiom Risk Engine v3.0 — 10-Layer Risk Intelligence
=====================================================
ARCHITECTURE: Axiom reports. OMNI decides.

Axiom's role is intelligence, not authority. Every layer independently
assesses one dimension of risk and reports its findings with full
transparency to OMNI. OMNI is the sole holder of GO/NO-GO authority.
Only Ahmed can override OMNI.

Axiom never auto-rejects. It raises critical flags — serious conditions
that OMNI must explicitly address in its reasoning. OMNI will almost
always say NO-GO when critical flags are raised. But that is OMNI's
informed decision, not Axiom's automated veto.

LAYER ARCHITECTURE:
  Layer 1:  Concentration Risk      — Alpaca positions + sector map
  Layer 2:  Liquidity Risk          — Polygon OI/volume + bid-ask spread
  Layer 3:  Volatility Regime       — ORATS IVR + VIX independent fetch
  Layer 4:  Earnings & Event        — ORATS earnings calendar
  Layer 5:  Correlation Shock       — Polygon beta calculation
  Layer 6:  Gamma Risk Profile      — Polygon Greeks + DTE
  Layer 7:  Dividend Risk           — Polygon dividend calendar
  Layer 8:  Macro Sensitivity       — sector map + FOMC/CPI calendar
  Layer 9:  Technical Breakdown     — Polygon price/volume history
  Layer 10: Black Swan Buffer       — VIX + FOMC + triple witching + holidays

CRITICAL FLAG conditions (reported to OMNI, not auto-rejected):
  Layer 1:  Position cap at limit OR sector cap breached
  Layer 2:  OI < 100 at strike (near-zero liquidity)
  Layer 3:  IVR < 15 on credit strategy OR IVR > 25 on debit strategy
  Layer 4:  Earnings within 5 calendar days
  Layer 6:  DTE < 21 (gamma implosion zone)
  Layer 10: VIX > 40

OMNI reads all flags + all scores and makes the final call.

SIZING OUTPUT:
  Each layer produces a sizing_rec (0.0 – 1.0) — a recommendation.
  Axiom's final sizing_rec = min(all sizing_recs).
  This is a recommendation to OMNI, not an instruction.

Author: OMNI / Axiom
Date:   2026-04-05 (v3.0 — Axiom reports, OMNI decides)
"""

import os
import json
import datetime
import requests
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
ALPACA_BASE   = "https://paper-api.alpaca.markets"
ALPACA_KEY    = os.getenv("ALPACA_KEY",    "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
ALPACA_H      = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
ORATS_TOKEN   = os.getenv("ORATS_TOKEN",   "4476e955-241a-4540-b114-ebbf1a3a3b87")
DEEPSEEK_KEY  = os.getenv("DEEPSEEK_KEY",  "sk-b750bc3774144ebd95e8dee764ffd384")

TOTAL_CAPITAL   = float(os.getenv("NEXUS_TOTAL_CAPITAL", "25000"))
MAX_POSITIONS   = int(os.getenv("NEXUS_MAX_POSITIONS",   "3"))  # HARD CAP per NEXUS_ARCHITECTURE.md + HARD_RULES.md
MAX_TICKER_PCT  = 0.05    # 5% per ticker
MAX_SECTOR_PCT  = 0.15    # 15% per sector

# Layer weights (must sum to 1.0)
LAYER_WEIGHTS = {
    1:  0.10,   # Concentration
    2:  0.15,   # Liquidity
    3:  0.12,   # Volatility Regime
    4:  0.15,   # Earnings
    5:  0.10,   # Correlation
    6:  0.10,   # Gamma
    7:  0.05,   # Dividend
    8:  0.08,   # Macro
    9:  0.10,   # Technical
    10: 0.05,   # Black Swan
}

# Macro-sensitive sector map
SECTOR_MAP = {
    "XLK":    ["NVDA","AMD","AAPL","MSFT","GOOGL","META","AVGO","QCOM","MU","CRM","PLTR","ARM","SMCI"],
    "XLF":    ["JPM","GS","MS","BAC","V","MA","AXP","BLK","SCHW","C","WFC","COIN"],
    "XLV":    ["LLY","UNH","JNJ","PFE","ABBV","MRK","TMO","ABT","BMY","AMGN"],
    "XLE":    ["XOM","CVX","COP","EOG","PSX","VLO","MPC","SLB","HAL","OXY"],
    "XLI":    ["LMT","RTX","BA","CAT","HON","GE","UPS","FDX","EMR","ETN"],
    "XLY":    ["AMZN","TSLA","HD","MCD","NKE","SBUX","TJX","BKNG","ABNB","UBER"],
    "XLP":    ["WMT","COST","PG","KO","PEP","PM","MO","CL"],
    "XLC":    ["NFLX","DIS","CMCSA","T","VZ","TMUS","EA"],
    "GROWTH": ["SHOP","SNOW","PANW","ZS","CRWD","DDOG","NET","MDB","ROKU","DUOL"],
}

# Macro sensitivity by sector (rate | FX | commodity)
MACRO_SENSITIVITY = {
    "XLF":    {"rate": 9, "fx": 5, "commodity": 2, "note": "Highly rate sensitive — financials"},
    "XLK":    {"rate": 7, "fx": 6, "commodity": 3, "note": "Growth stocks rate sensitive; some FX exposure"},
    "XLE":    {"rate": 3, "fx": 5, "commodity": 9, "note": "Directly tied to oil/gas commodity prices"},
    "XLI":    {"rate": 5, "fx": 7, "commodity": 6, "note": "FX + commodity input cost exposure"},
    "XLY":    {"rate": 6, "fx": 4, "commodity": 4, "note": "Consumer cyclical, rate sensitive"},
    "XLP":    {"rate": 4, "fx": 3, "commodity": 5, "note": "Defensive; moderate macro sensitivity"},
    "XLV":    {"rate": 3, "fx": 3, "commodity": 2, "note": "Low macro sensitivity — defensive"},
    "XLC":    {"rate": 5, "fx": 4, "commodity": 2, "note": "Moderate sensitivity"},
    "GROWTH": {"rate": 8, "fx": 5, "commodity": 2, "note": "Highest rate sensitivity — long duration"},
}

# FOMC meeting dates 2025-2026 (approximate — update annually)
FOMC_DATES_2025_2026 = [
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-17",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-16",
]

# US Market holidays 2025-2026
MARKET_HOLIDAYS = [
    "2025-01-01","2025-01-20","2025-02-17","2025-04-18","2025-05-26",
    "2025-06-19","2025-07-04","2025-09-01","2025-11-27","2025-12-25",
    "2026-01-01","2026-01-19","2026-02-16","2026-04-03","2026-05-25",
    "2026-06-19","2026-07-03","2026-09-07","2026-11-26","2026-12-25",
]


# ── Data fetchers ─────────────────────────────────────────────────────────────

def _get_alpaca_positions() -> list:
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/positions", headers=ALPACA_H, timeout=5)
        return r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
    except Exception:
        return []


def _get_alpaca_account() -> dict:
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/account", headers=ALPACA_H, timeout=5)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def _get_vix() -> Optional[float]:
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
                        headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        return None


def _get_orats_summary(ticker: str) -> dict:
    try:
        r = requests.get("https://api.orats.io/datav2/summaries",
                        params={"token": ORATS_TOKEN, "ticker": ticker}, timeout=8)
        data = r.json().get("data", [])
        return data[0] if data else {}
    except Exception:
        return {}


def _get_orats_earnings(ticker: str) -> dict:
    try:
        r = requests.get("https://api.orats.io/datav2/earnings",
                        params={"token": ORATS_TOKEN, "ticker": ticker}, timeout=8)
        data = r.json().get("data", [])
        return data[0] if data else {}
    except Exception:
        return {}


def _get_orats_strike_data(ticker: str, strike: Optional[float],
                            dte_target: int) -> dict:
    """Fetch IV, OI, volume, Greeks for a specific strike/expiry."""
    try:
        r = requests.get("https://api.orats.io/datav2/strikes",
                        params={"token": ORATS_TOKEN, "ticker": ticker}, timeout=10)
        data = r.json().get("data", [])
        if not data:
            return {}
        # Filter to target DTE range ±5 days
        today = datetime.date.today()
        candidates = []
        for d in data:
            exp = d.get("expirDate", "")
            try:
                exp_date = datetime.date.fromisoformat(exp)
                delta_dte = abs((exp_date - today).days - dte_target)
                if delta_dte <= 5:
                    if strike is None or abs(float(d.get("strike", 0)) - strike) < strike * 0.05:
                        candidates.append(d)
            except Exception:
                pass
        return candidates[0] if candidates else (data[0] if data else {})
    except Exception:
        return {}


def _get_polygon_technical(ticker: str) -> dict:
    """Fetch technical data via Polygon (replaces yfinance)."""
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(__file__))
        from polygon_data import get_technical_data
        return get_technical_data(ticker)
    except Exception:
        return {}


def _get_polygon_beta(ticker: str) -> dict:
    """Fetch 60-day and 20-day realized beta via Polygon (replaces yfinance)."""
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(__file__))
        from polygon_data import calc_beta, calc_realized_beta_20d
        return {
            "beta_60d": calc_beta(ticker, period_days=60),
            "beta_20d": calc_realized_beta_20d(ticker),
        }
    except Exception:
        return {}


def _get_polygon_options(ticker: str, strike: Optional[float],
                          dte: int, option_type: str = "call") -> dict:
    """Fetch options snapshot via Polygon (replaces ORATS strike data)."""
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(__file__))
        from polygon_data import get_options_snapshot
        return get_options_snapshot(ticker, strike, dte, option_type)
    except Exception:
        return {}


def _get_polygon_dividends(ticker: str) -> dict:
    """Fetch dividend data via Polygon (replaces yfinance dividends)."""
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(__file__))
        from polygon_data import get_dividend_data
        return get_dividend_data(ticker)
    except Exception:
        return {}


def _get_sector(ticker: str) -> Optional[str]:
    for sector, tickers in SECTOR_MAP.items():
        if ticker in tickers:
            return sector
    return None


def _days_to_fomc() -> int:
    today = datetime.date.today()
    upcoming = [datetime.date.fromisoformat(d) for d in FOMC_DATES_2025_2026
                if datetime.date.fromisoformat(d) >= today]
    return (upcoming[0] - today).days if upcoming else 99


def _is_triple_witching_week() -> bool:
    """True if current week contains 3rd Friday of Mar/Jun/Sep/Dec."""
    today = datetime.date.today()
    if today.month not in [3, 6, 9, 12]:
        return False
    # Find 3rd Friday of this month
    first = today.replace(day=1)
    fridays = [first + datetime.timedelta(days=i)
               for i in range(31)
               if (first + datetime.timedelta(days=i)).weekday() == 4
               and (first + datetime.timedelta(days=i)).month == today.month]
    if len(fridays) >= 3:
        tw = fridays[2]
        return abs((tw - today).days) <= 5
    return False


def _days_to_holiday() -> int:
    today = datetime.date.today()
    upcoming = [datetime.date.fromisoformat(d) for d in MARKET_HOLIDAYS
                if datetime.date.fromisoformat(d) >= today]
    return (upcoming[0] - today).days if upcoming else 99


# ── Layer scoring functions ───────────────────────────────────────────────────

def score_layer_1_concentration(ticker: str, proposed_usd: float) -> dict:
    """Layer 1: Concentration Risk — positions + sector exposure."""
    positions = _get_alpaca_positions()
    account   = _get_alpaca_account()
    equity    = float(account.get("equity", TOTAL_CAPITAL))

    open_count    = len(positions)
    sector        = _get_sector(ticker)
    ticker_pct    = proposed_usd / equity
    hard_stop     = False
    notes         = []

    if open_count >= MAX_POSITIONS:
        return {"score": 10, "sizing_mult": 0.0, "critical_flag": True,
                "note": f"Position gate CLOSED — {open_count}/{MAX_POSITIONS} full"}

    if ticker_pct > MAX_TICKER_PCT:
        notes.append(f"Proposed ${proposed_usd:.0f} = {ticker_pct*100:.1f}% > {MAX_TICKER_PCT*100:.0f}% max")
        hard_stop = True

    if sector:
        sector_tickers = SECTOR_MAP.get(sector, [])
        sector_exposure = sum(
            float(p.get("market_value", 0)) for p in positions
            if p.get("symbol") in sector_tickers
        )
        sector_pct = (sector_exposure + proposed_usd) / equity
        if sector_pct > MAX_SECTOR_PCT:
            notes.append(f"{sector} sector at {sector_pct*100:.1f}% > {MAX_SECTOR_PCT*100:.0f}% cap")
            hard_stop = True

    if hard_stop:
        return {"score": 10, "sizing_mult": 0.0, "critical_flag": True,
                "note": "; ".join(notes) or "Concentration limit breached"}

    score = min(10, int(open_count / MAX_POSITIONS * 8) + 1)
    mult  = 1.0 - (open_count / MAX_POSITIONS) * 0.3
    return {"score": score, "sizing_mult": round(mult, 2), "critical_flag": False,
            "note": f"{open_count}/{MAX_POSITIONS} positions open | {sector or 'unknown'} sector | {ticker_pct*100:.1f}% exposure"}


def score_layer_2_liquidity(ticker: str, strike: Optional[float],
                             dte: int) -> dict:
    """Layer 2: Liquidity Risk — OI, volume, bid-ask spread via Polygon."""
    # Primary: Polygon options snapshot (real-time OI, volume, live bid-ask)
    data = _get_polygon_options(ticker, strike, dte)
    if not data:
        # Fallback: ORATS
        data = _get_orats_strike_data(ticker, strike, dte)

    # Polygon returns normalized fields; ORATS fallback uses different keys
    oi  = float(data.get("open_interest", data.get("callOi", data.get("putOi", 0))) or 0)
    vol = float(data.get("volume", data.get("callVolume", data.get("putVolume", 0))) or 0)

    if oi < 100:
        return {"score": 10, "sizing_mult": 0.0, "critical_flag": True,
                "note": f"OI {oi:.0f} < 100 minimum — no liquidity at this strike"}

    score = 2
    mult  = 1.0
    notes = []

    if oi < 1000:
        score += 3
        mult   = 0.75
        notes.append(f"OI {oi:.0f} < 1,000 target")
    if vol < 500:
        score += 2
        mult   = min(mult, 0.75)
        notes.append(f"Volume {vol:.0f} < 500 target")

    # Polygon bid-ask (live) — superior to ORATS
    bid    = float(data.get("bid", data.get("callBid", data.get("putBid", 0))) or 0)
    ask    = float(data.get("ask", data.get("callAsk", data.get("putAsk", 0))) or 0)
    spread = float(data.get("spread_pct", 0))
    if spread == 0 and bid > 0 and ask > 0:
        mid    = (bid + ask) / 2
        spread = (ask - bid) / mid if mid > 0 else 1
    if spread > 0.15:
        score += 3
        mult   = min(mult, 0.5)
        notes.append(f"Spread {spread*100:.1f}% > 15% max")

    score = min(10, score)
    return {"score": score, "sizing_mult": mult, "critical_flag": False,
            "note": f"OI:{oi:.0f} Vol:{vol:.0f}" + (f" | {'; '.join(notes)}" if notes else "")}


def score_layer_3_volatility_regime(ticker: str, strategy: str) -> dict:
    """Layer 3: Volatility Regime Alignment — IVR vs VIX vs strategy type."""
    orats  = _get_orats_summary(ticker)
    vix    = _get_vix() or 20.0
    # ORATS upgraded tier field names: rip=rank implied percentile (IVR), iv30d=current IV
    ivr    = float(orats.get("rip", orats.get("ivRank", 50)) or 50)
    iv30   = float(orats.get("iv30", 0) or 0)
    strat  = strategy.lower()
    score  = 2
    mult   = 1.0
    notes  = []

    is_credit = any(k in strat for k in ["put spread","call spread","condor","credit"])
    is_debit  = any(k in strat for k in ["long call","long put","debit","spike"])

    # Credit spreads need IV to be elevated (IVR ≥ 25 to sell premium worth selling)
    if is_credit:
        if ivr < 15:
            return {"score": 10, "sizing_mult": 0.0, "critical_flag": True,
                    "note": f"IVR {ivr:.0f} < 15 — premium too cheap for credit spread. Hard stop."}
        if ivr < 25:
            score += 2; mult = 0.85  # MEMORY.md: IVR 15-25 = soft flag, 0.85x size reduction
            notes.append(f"IVR {ivr:.0f} in caution zone (15-25) — soft flag, 0.85x size")
        if ivr > 80:
            score += 2; mult = min(mult, 0.75)
            notes.append(f"IVR {ivr:.0f} extreme — consider IV crush risk on spreads")

    # Debit/long options need LOW IV (buy cheap)
    if is_debit:
        if ivr > 25:
            return {"score": 10, "sizing_mult": 0.0, "critical_flag": True,
                    "note": f"IVR {ivr:.0f} > 25 — premium too expensive for long option. Hard stop."}
        if ivr > 20:
            score += 2; mult = 0.85
            notes.append(f"IVR {ivr:.0f} slightly elevated for debit")

    # VIX regime alignment
    if vix > 30 and is_credit:
        score += 2; mult = min(mult, 0.75)
        notes.append(f"VIX {vix:.1f} > 30 in credit strategy — vol trending")
    if vix > 40:
        score += 3; mult = min(mult, 0.5)
        notes.append(f"VIX {vix:.1f} extreme — all sizing reduced")

    score = min(10, score)
    return {"score": score, "sizing_mult": round(mult, 2), "critical_flag": False,
            "note": f"IVR:{ivr:.0f} VIX:{vix:.1f}" + (f" | {'; '.join(notes)}" if notes else "")}


def score_layer_4_earnings(ticker: str, dte: int) -> dict:
    """
    Layer 4: Earnings & Event Proximity.
    Primary: ORATS real-time summary (nextErnDate field).
    Fallback: yfinance calendar (Earnings Date).
    Both are tried — most conservative (nearest) date wins.
    """
    data    = _get_orats_earnings(ticker)
    score   = 1
    mult    = 1.0
    notes   = []

    # --- Source 1: ORATS real-time summary (upgraded tier field names) ---
    earn_str = data.get("nextErnDate", data.get("nextEarnDate",
               data.get("nextEarnings", data.get("earnDate", ""))))
    days_to_earnings = 999

    if earn_str:
        try:
            ed = datetime.date.fromisoformat(earn_str[:10])
            days = (ed - datetime.date.today()).days
            if days > 0:
                days_to_earnings = days
        except Exception:
            pass

    # --- Source 2: yfinance calendar (fallback or cross-check) ---
    if days_to_earnings == 999:
        try:
            import yfinance as yf
            cal = yf.Ticker(ticker).calendar
            earn_dates = cal.get("Earnings Date", [])
            if not isinstance(earn_dates, list):
                earn_dates = [earn_dates]
            for ed in earn_dates:
                try:
                    if hasattr(ed, 'date'):
                        ed = ed.date()
                    elif isinstance(ed, str):
                        ed = datetime.date.fromisoformat(ed[:10])
                    days = (ed - datetime.date.today()).days
                    if 0 < days < days_to_earnings:
                        days_to_earnings = days
                        earn_str = str(ed)
                except Exception:
                    pass
        except Exception:
            pass

    if 0 < days_to_earnings <= 5:
        return {"score": 10, "sizing_mult": 0.0, "critical_flag": True,
                "note": f"Earnings in {days_to_earnings} day(s) — hard stop. No new positions within 5 DTE of earnings."}

    if 0 < days_to_earnings <= 14:
        score += 5; mult = 0.5
        notes.append(f"Earnings in {days_to_earnings} days — high binary risk")
    elif 0 < days_to_earnings <= 30:
        # Check if earnings falls within DTE window
        if days_to_earnings <= dte:
            score += 3; mult = 0.75
            notes.append(f"Earnings in {days_to_earnings}d within DTE window ({dte}d)")
        else:
            score += 1
            notes.append(f"Earnings in {days_to_earnings}d — outside DTE window")

    score = min(10, score)
    note  = f"Next earnings: {earn_str[:10] if earn_str else 'unknown'} ({days_to_earnings}d away)"
    note += f" | {'; '.join(notes)}" if notes else ""
    return {"score": score, "sizing_mult": round(mult, 2), "critical_flag": False, "note": note}


def score_layer_5_correlation(ticker: str) -> dict:
    """
    Layer 5: Correlation Shock Risk — Polygon realized beta (20d + 60d).
    Uses BOTH historical and recent beta. Higher of the two scores.
    Catches regime breaks where historical beta understates true exposure.
    """
    betas = _get_polygon_beta(ticker)
    beta_60d = betas.get("beta_60d")
    beta_20d = betas.get("beta_20d")
    vix      = _get_vix() or 20.0
    score    = 2
    mult     = 1.0
    notes    = []

    if beta_60d is None and beta_20d is None:
        return {"score": 4, "sizing_mult": 0.9, "critical_flag": False,
                "note": "Beta unavailable — moderate caution"}

    # Use the HIGHER beta (most conservative — catches regime breaks)
    beta = max(b for b in [beta_60d, beta_20d] if b is not None)

    # Flag if 20-day realized beta significantly exceeds 60-day (regime break signal)
    regime_break = (beta_20d and beta_60d and beta_20d > beta_60d * 1.3)
    if regime_break:
        score += 1
        notes.append(f"Regime break: 20d beta {beta_20d:.2f} >> 60d {beta_60d:.2f}")

    if beta > 2.5:
        score += 5; mult = 0.5
        notes.append(f"Beta {beta:.2f} — extreme systematic exposure")
    elif beta > 2.0:
        score += 4; mult = 0.6
        notes.append(f"Beta {beta:.2f} — very high market sensitivity")
    elif beta > 1.5:
        score += 3; mult = 0.75
        notes.append(f"Beta {beta:.2f} > 1.5 — elevated market sensitivity")
        if vix > 25:
            score += 1; mult = min(mult, 0.65)
            notes.append(f"VIX {vix:.1f} amplifies high-beta risk")
    elif beta > 1.2:
        score += 1
        notes.append(f"Beta {beta:.2f} — moderate market sensitivity")
    elif beta < 0:
        score += 2; mult = 0.85
        notes.append(f"Beta {beta:.2f} — inverse correlation risk")

    score = min(10, score)
    return {"score": score, "sizing_mult": round(mult, 2), "critical_flag": False,
            "note": f"Beta 60d:{round(beta_60d,2) if beta_60d else 'N/A'} 20d:{round(beta_20d,2) if beta_20d else 'N/A'} VIX:{vix:.1f}"
                    + (f" | {'; '.join(notes)}" if notes else "")}


def score_layer_6_gamma(ticker: str, dte: int, strike: Optional[float]) -> dict:
    """Layer 6: Gamma Risk Profile — DTE + Polygon live Greeks."""
    if dte < 21:
        return {"score": 10, "sizing_mult": 0.0, "critical_flag": True,
                "note": f"DTE {dte} < 21 minimum — gamma implosion zone. Hard stop."}

    # Primary: Polygon (live Greeks)
    data  = _get_polygon_options(ticker, strike, dte)
    gamma = float(data.get("gamma", data.get("callGamma", data.get("putGamma", 0))) or 0)
    score = 1
    mult  = 1.0
    notes = []

    if dte < 28:
        score += 3; mult = 0.75
        notes.append(f"DTE {dte} in caution zone (21-28) — elevated gamma")
    elif dte < 35:
        score += 1
        notes.append(f"DTE {dte} — acceptable but monitor gamma weekly")

    if gamma > 0.05:
        score += 2; mult = min(mult, 0.75)
        notes.append(f"Gamma {gamma:.4f} elevated near expiry")

    score = min(10, score)
    return {"score": score, "sizing_mult": round(mult, 2), "critical_flag": False,
            "note": f"DTE:{dte} Gamma:{gamma:.4f}" + (f" | {'; '.join(notes)}" if notes else "")}


def score_layer_7_dividend(ticker: str, dte: int) -> dict:
    """Layer 7: Dividend Risk — Polygon accurate ex-div dates."""
    div_data      = _get_polygon_dividends(ticker)
    days_to_exdiv = div_data.get("days_to_exdiv", 999)
    cash_amount   = div_data.get("cash_amount",   0)
    frequency     = div_data.get("frequency",     4)
    annual_div    = cash_amount * frequency
    score         = 1
    mult          = 1.0
    notes         = []

    # Estimate yield from annual dividend vs current price (approximate)
    # Full yield requires current price — use conservative threshold on cash amount
    high_yield_flag = annual_div > 1.0  # > $1/share/year ≈ roughly >2-3% for most names

    if 0 < days_to_exdiv <= dte:
        if high_yield_flag:
            score += 5; mult = 0.5
            notes.append(f"Ex-div in {days_to_exdiv}d | ${cash_amount:.2f}/share — early assignment risk")
        else:
            score += 2; mult = 0.85
            notes.append(f"Ex-div in {days_to_exdiv}d within DTE window")
    elif days_to_exdiv <= 0:
        notes.append(f"Ex-div was {abs(days_to_exdiv)}d ago — next cycle")

    score = min(10, score)
    note  = f"Ex-div:{days_to_exdiv}d | ${cash_amount:.2f}/shr | Annual:${annual_div:.2f}"
    note += f" | {'; '.join(notes)}" if notes else ""
    return {"score": score, "sizing_mult": round(mult, 2), "critical_flag": False, "note": note}


def score_layer_8_macro(ticker: str) -> dict:
    """Layer 8: Macro Sensitivity — sector + FOMC/CPI proximity."""
    sector     = _get_sector(ticker)
    macro_map  = MACRO_SENSITIVITY.get(sector, {"rate": 5, "fx": 5, "commodity": 5, "note": ""})
    fomc_days  = _days_to_fomc()
    score      = 2
    mult       = 1.0
    notes      = []

    max_sensitivity = max(macro_map["rate"], macro_map["fx"], macro_map["commodity"])
    if max_sensitivity >= 8:
        score += 2
        notes.append(f"High macro sensitivity ({macro_map['note']})")
    elif max_sensitivity >= 6:
        score += 1

    if fomc_days <= 3:
        score += 3; mult = 0.75
        notes.append(f"FOMC in {fomc_days} day(s) — rate shock window")
    elif fomc_days <= 7:
        score += 2; mult = min(mult, 0.85)
        notes.append(f"FOMC in {fomc_days} days — size reduction")

    score = min(10, score)
    return {"score": score, "sizing_mult": round(mult, 2), "critical_flag": False,
            "note": f"Sector:{sector or 'unknown'} | Rate:{macro_map['rate']} FX:{macro_map['fx']} Comm:{macro_map['commodity']}"
                    + (f" | {'; '.join(notes)}" if notes else "")}


def score_layer_9_technical(ticker: str) -> dict:
    """Layer 9: Technical Breakdown Risk — Polygon price/volume data."""
    pg_data = _get_polygon_technical(ticker)
    rsi     = pg_data.get("rsi")
    sma20   = pg_data.get("sma20")
    sma50   = pg_data.get("sma50")
    price   = pg_data.get("price")
    vol_avg = pg_data.get("avg_volume")
    vol_now = pg_data.get("volume_today")
    at_high = pg_data.get("at_52w_high", False)
    at_low  = pg_data.get("at_52w_low",  False)
    score   = 2
    mult    = 1.0
    notes   = []

    if rsi is not None:
        if rsi > 80:
            score += 3; mult = 0.75
            notes.append(f"RSI {rsi:.0f} — extremely overbought, reversal risk")
        elif rsi > 70:
            score += 1
            notes.append(f"RSI {rsi:.0f} — overbought")
        elif rsi < 25:
            score += 3; mult = min(mult, 0.75)
            notes.append(f"RSI {rsi:.0f} — extremely oversold, dead-cat risk")
        elif rsi < 35:
            score += 1
            notes.append(f"RSI {rsi:.0f} — oversold")

    if price and sma50 and price < sma50 * 0.95:
        score += 2; mult = min(mult, 0.75)
        notes.append(f"Price ${price:.1f} >5% below SMA50 ${sma50:.1f} — downtrend")

    if at_high:
        score += 2; mult = min(mult, 0.85)
        notes.append("Near 52-week high — elevated reversal risk")
    if at_low:
        score += 2; mult = min(mult, 0.85)
        notes.append("Near 52-week low — potential support break")

    if vol_avg and vol_now:
        if vol_now < vol_avg * 0.5:
            score += 1
            notes.append(f"Volume {vol_now/1e6:.1f}M vs avg {vol_avg/1e6:.1f}M — thin market")

    score = min(10, score)
    return {"score": score, "sizing_mult": round(mult, 2), "critical_flag": False,
            "note": f"RSI:{round(rsi) if rsi else 'N/A'} | {'↑52wHigh' if at_high else '↓52wLow' if at_low else 'Mid-range'}"
                    + (f" | {'; '.join(notes)}" if notes else "")}


def score_layer_10_black_swan(dte: int) -> dict:
    """Layer 10: Black Swan Buffer — VIX, FOMC, triple witching, holidays."""
    vix        = _get_vix() or 20.0
    fomc_days  = _days_to_fomc()
    tw_week    = _is_triple_witching_week()
    hol_days   = _days_to_holiday()
    score      = 1
    mult       = 1.0
    notes      = []

    if vix > 40:
        return {"score": 10, "sizing_mult": 0.0, "critical_flag": True,
                "note": f"VIX {vix:.1f} > 40 — black swan territory. Hard stop all entries."}

    if vix > 30:
        score += 3; mult = 0.75
        notes.append(f"VIX {vix:.1f} > 30 — elevated tail risk")
    elif vix > 25:
        score += 1; mult = min(mult, 0.9)
        notes.append(f"VIX {vix:.1f} — caution zone")

    if fomc_days <= 7:
        score += 2; mult = min(mult, 0.85)
        notes.append(f"FOMC in {fomc_days}d — policy shock risk")

    if tw_week:
        score += 2; mult = min(mult, 0.85)
        notes.append("Triple witching week — unusual gamma/liquidity")

    if 0 < hol_days <= 3:
        score += 1; mult = min(mult, 0.9)
        notes.append(f"Holiday in {hol_days}d — thin liquidity window")

    score = min(10, score)
    return {"score": score, "sizing_mult": round(mult, 2), "critical_flag": False,
            "note": f"VIX:{vix:.1f} FOMC:{fomc_days}d {'TW-WEEK' if tw_week else ''} Holiday:{hol_days}d"
                    + (f" | {'; '.join(notes)}" if notes else "")}


# ── Master assessment ─────────────────────────────────────────────────────────

def run_full_assessment(
    ticker: str,
    strategy: str,
    dte: int = 35,
    strike: Optional[float] = None,
    proposed_usd: float = 1000.0,
) -> dict:
    """
    Run all 10 layers independently. Return full risk report.
    Any layer with hard_stop=True → auto-reject immediately.
    """
    layers = {}
    print(f"\n🔷 Axiom 10-Layer Assessment: {ticker} | {strategy} | DTE {dte}")

    runners = [
        (1,  "Concentration",  lambda: score_layer_1_concentration(ticker, proposed_usd)),
        (2,  "Liquidity",      lambda: score_layer_2_liquidity(ticker, strike, dte)),
        (3,  "Vol Regime",     lambda: score_layer_3_volatility_regime(ticker, strategy)),
        (4,  "Earnings",       lambda: score_layer_4_earnings(ticker, dte)),
        (5,  "Correlation",    lambda: score_layer_5_correlation(ticker)),
        (6,  "Gamma",          lambda: score_layer_6_gamma(ticker, dte, strike)),
        (7,  "Dividend",       lambda: score_layer_7_dividend(ticker, dte)),
        (8,  "Macro",          lambda: score_layer_8_macro(ticker)),
        (9,  "Technical",      lambda: score_layer_9_technical(ticker)),
        (10, "Black Swan",     lambda: score_layer_10_black_swan(dte)),
    ]

    # Run ALL layers — Axiom never aborts early. OMNI gets the full picture.
    for num, name, fn in runners:
        try:
            result = fn()
        except Exception as e:
            result = {"score": 4, "sizing_mult": 0.9, "critical_flag": False,
                      "note": f"Layer assessment failed: {e}"}

        layers[num] = {"name": name, **result}
        flag_icon = "🚨 CRITICAL FLAG" if result.get("critical_flag") or result.get("hard_stop") else f"Score:{result['score']:2d}/10"
        print(f"  Layer {num:2d} {name:15s} {flag_icon} | Size:{result['sizing_mult']:.2f}x | {result.get('note','')[:60]}")

    return _build_report(ticker, strategy, layers)


def _build_report(ticker: str, strategy: str, layers: dict) -> dict:
    """
    Aggregate all 10 layer scores into a full intelligence report for OMNI.

    AXIOM DOES NOT DECIDE. This report is advisory intelligence.
    OMNI reads every flag, every score, every note — and makes the call.
    """
    # Collect critical flags across all layers
    critical_flags = []
    for n, d in layers.items():
        if d.get("critical_flag") or d.get("hard_stop"):  # support both field names
            critical_flags.append({
                "layer":  n,
                "name":   d["name"],
                "reason": d.get("note", ""),
            })

    # Weighted aggregate risk score
    weighted_score = sum(
        layers[n]["score"] * LAYER_WEIGHTS[n] for n in layers if n in LAYER_WEIGHTS
    )
    # Floor at 4.0 when any critical flag is raised — ensures E2E gates and OMNI
    # correctly treat critical-flag picks as high-risk regardless of other layers.
    raw_score = round(min(10, weighted_score), 1)
    final_score = max(raw_score, 4.0) if any(
        layers[n].get("critical_flag") or layers[n].get("hard_stop")
        for n in layers
    ) else raw_score

    # Sizing recommendation = minimum of all layer sizing recs
    all_mults  = [layers[n]["sizing_mult"] for n in layers]
    final_mult = round(min(all_mults), 2)

    # Sizing label
    if final_mult <= 0.1:
        sizing_rec = "avoid (Axiom recommends NO-GO)"
    elif final_mult <= 0.55:
        sizing_rec = "0.5x"
    elif final_mult <= 0.80:
        sizing_rec = "0.75x"
    else:
        sizing_rec = "full"

    # Top 3 risk concerns (highest scoring layers)
    sorted_layers = sorted(layers.items(), key=lambda x: x[1]["score"], reverse=True)
    concerns      = [f"L{n} {d['name']}: {d['note'][:70]}" for n, d in sorted_layers[:3]]

    # Axiom's recommendation to OMNI (advisory only)
    if critical_flags:
        flag_summary = "; ".join(f"L{f['layer']} {f['name']}: {f['reason'][:50]}" for f in critical_flags)
        recommendation = f"STRONGLY_CAUTION — {len(critical_flags)} critical flag(s): {flag_summary}"
    elif final_score >= 8.0:
        recommendation = "CAUTION — High aggregate risk score. OMNI should scrutinize carefully."
    elif final_score >= 6.0:
        recommendation = "MODERATE_RISK — Proceed with reduced sizing if thesis is strong."
    else:
        recommendation = "ACCEPTABLE — Risk profile within normal parameters."

    # Note to OMNI — direct communication from Axiom
    if critical_flags:
        flag_reasons = "; ".join(fl["reason"][:40] for fl in critical_flags[:2])
        note_to_omni = (
            f"OMNI: {len(critical_flags)} critical condition(s) found on {ticker}. "
            f"Flags: {flag_reasons}. "
            f"Final authority is yours — these flags strongly suggest NO-GO."
        )
    else:
        note_to_omni = (
            f"OMNI: No critical flags on {ticker}. Risk score {final_score}/10. "
            f"Sizing recommendation: {sizing_rec}. All 10 layers assessed."
        )

    return {
        "ticker":             ticker,
        "strategy":           strategy,
        "risk_score":         final_score,
        "sizing_rec":         sizing_rec,
        "sizing_mult":        final_mult,
        "critical_flags":     critical_flags,
        "critical_flag_count":len(critical_flags),
        "recommendation":     recommendation,
        "note_to_omni":       note_to_omni,
        "concern_1":          concerns[0] if len(concerns) > 0 else "N/A",
        "concern_2":          concerns[1] if len(concerns) > 1 else "N/A",
        "concern_3":          concerns[2] if len(concerns) > 2 else "N/A",
        # Legacy fields — kept for backward compatibility with callers
        "sizing_suggestion":  sizing_rec,
        "auto_reject":        False,  # Axiom never auto-rejects. OMNI decides.
        "hard_stop_layer":    critical_flags[0]["layer"] if critical_flags else 0,
        # hard_stops = alias of critical_flags for E2E test compatibility
        "hard_stops":         critical_flags,
        "report":             note_to_omni,
        "layers":             layers,
        "timestamp":          datetime.datetime.utcnow().isoformat(),
    }


if __name__ == "__main__":
    print("🔷 Axiom Risk Engine v2.0 — Layer Architecture Verification\n")
    print("Layer weights:", {k: f"{v*100:.0f}%" for k, v in LAYER_WEIGHTS.items()})
    print(f"Total weight: {sum(LAYER_WEIGHTS.values())*100:.0f}%")
    print(f"\nFOMC in {_days_to_fomc()} days")
    print(f"Triple witching week: {_is_triple_witching_week()}")
    print(f"Holiday in {_days_to_holiday()} days")
    print(f"\nSector for NVDA: {_get_sector('NVDA')}")
    print(f"Sector for JPM:  {_get_sector('JPM')}")
    print(f"Macro sensitivity GROWTH: {MACRO_SENSITIVITY.get('GROWTH')}")
    print("\n✅ Layer architecture verified. Call run_full_assessment() for live assessment.")
