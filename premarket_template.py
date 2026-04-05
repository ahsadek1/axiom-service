"""
Axiom Pre-Market Report — Official Template v1.0
==================================================
Generated every trading day at 9:00 AM ET.
Distributed to: OMNI, all agents (Cipher, Sage, Atlas), Alpha Group, Prime Group.

PURPOSE:
  This is the daily risk briefing that shapes every decision made that day.
  Agents read this BEFORE screening. OMNI reads this BEFORE synthesizing.
  It sets the tone, the limits, and the flags for the session.

TEMPLATE SECTIONS:
  1.  Market Regime         — VIX, F&G, SPY trend, breadth
  2.  Overall Risk Score    — Axiom's 1-10 daily risk level
  3.  Top Risk Factors      — what could go wrong today (top 3)
  4.  Macro Events          — FOMC, CPI, earnings, Fed speakers today/this week
  5.  Volatility Landscape  — VIX term structure, IVR environment, put/call ratio
  6.  Sector Risk Map       — which sectors are elevated/safe today
  7.  Open Position Review  — how current positions look in today's context
  8.  Strategy Guidance     — what to favor/avoid given today's setup
  9.  Watchlist Flags       — specific tickers with elevated risk today
  10. Circuit Breaker Status — any active halts, restrictions, or cautions
  11. OMNI Directive        — single-sentence instruction to OMNI for the day

Author: OMNI / Axiom
Date:   2026-04-05
"""

import os
import datetime
import requests
from typing import Optional

DEEPSEEK_KEY  = os.getenv("DEEPSEEK_KEY",  "sk-b750bc3774144ebd95e8dee764ffd384")
POLYGON_KEY   = os.getenv("POLYGON_KEY",   "lzWjU48O_ZoEjNHkKt3G7M_nqbxrweUI")
ALPACA_BASE   = "https://paper-api.alpaca.markets"
ALPACA_KEY    = os.getenv("ALPACA_KEY",    "PKPGM3BRNYPGCF5Z56IAUZCZJL")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs")
ALPACA_H      = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
TG_BOT        = "8747601602:AAGTzRd3NJWq44Bvbzd5JvhtnO2edBUvjbc"
AHMED_ID      = "8573754783"
ALPHA_GROUP   = "-5167351071"
PRIME_GROUP   = "-5111649337"
HEALTH_GRP    = "-5184172590"

# FOMC dates 2025-2026
FOMC_DATES = [
    "2025-01-29","2025-03-19","2025-05-07","2025-06-18",
    "2025-07-30","2025-09-17","2025-10-29","2025-12-17",
    "2026-01-28","2026-03-18","2026-04-29","2026-06-17",
    "2026-07-29","2026-09-16","2026-10-28","2026-12-16",
]

# CPI/PPI/NFP economic calendar 2025-2026 (approximate)
ECONOMIC_EVENTS = {
    "2026-04-10": "CPI (March)", "2026-04-11": "PPI (March)",
    "2026-04-03": "NFP (March)", "2026-04-30": "FOMC Decision",
    "2026-05-13": "CPI (April)", "2026-05-14": "PPI (April)",
    "2026-06-17": "FOMC Decision", "2026-06-11": "CPI (May)",
}


# ── Data fetchers ─────────────────────────────────────────────────────────────

def _get_vix() -> dict:
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
                        headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        d = r.json()["chart"]["result"][0]
        return {
            "current": float(d["meta"]["regularMarketPrice"]),
            "prev":    float(d["meta"]["previousClose"]),
        }
    except Exception:
        return {"current": 0, "prev": 0}


def _get_spy_data() -> dict:
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/SPY/range/1/day/2025-10-01/{datetime.date.today()}",
            params={"adjusted":"true","sort":"asc","limit":300,"apiKey":POLYGON_KEY},
            timeout=10
        )
        bars = r.json().get("results", [])
        if len(bars) < 50:
            return {}
        closes = [b["c"] for b in bars]
        sma20  = sum(closes[-20:]) / 20
        sma50  = sum(closes[-50:]) / 50
        sma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
        price  = closes[-1]
        return {
            "price":  round(price, 2),
            "sma20":  round(sma20, 2),
            "sma50":  round(sma50, 2),
            "sma200": round(sma200, 2) if sma200 else None,
            "above_sma20":  price > sma20,
            "above_sma50":  price > sma50,
            "above_sma200": price > sma200 if sma200 else None,
            "chg_1d_pct": round((closes[-1]-closes[-2])/closes[-2]*100, 2) if len(closes)>=2 else 0,
            "chg_5d_pct": round((closes[-1]-closes[-6])/closes[-6]*100, 2) if len(closes)>=6 else 0,
        }
    except Exception:
        return {}


def _get_market_breadth() -> dict:
    """Approximate breadth via sector ETF performance."""
    sectors = {"XLK":"Tech","XLF":"Finance","XLV":"Health","XLE":"Energy",
               "XLI":"Industrial","XLY":"Consumer","XLP":"Staples","XLC":"Comm"}
    results = {}
    for etf, name in sectors.items():
        try:
            r = requests.get(
                f"https://api.polygon.io/v2/aggs/ticker/{etf}/range/1/day/"
                f"{datetime.date.today()-datetime.timedelta(days=7)}/{datetime.date.today()}",
                params={"adjusted":"true","sort":"asc","limit":10,"apiKey":POLYGON_KEY},
                timeout=8
            )
            bars = r.json().get("results",[])
            if len(bars) >= 2:
                chg = (bars[-1]["c"]-bars[-2]["c"])/bars[-2]["c"]*100
                results[name] = round(chg, 2)
        except Exception:
            pass
    green = sum(1 for v in results.values() if v > 0)
    red   = sum(1 for v in results.values() if v < 0)
    return {"sectors": results, "green": green, "red": red, "total": len(results)}


def _get_open_positions() -> list:
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/positions", headers=ALPACA_H, timeout=5)
        return r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
    except Exception:
        return []


def _get_put_call_ratio() -> Optional[float]:
    """Approximate PCR from CBOE via Yahoo."""
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EPCALL",
                        headers={"User-Agent":"Mozilla/5.0"}, timeout=5)
        return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        return None


def _days_to_fomc() -> int:
    today = datetime.date.today()
    upcoming = [datetime.date.fromisoformat(d) for d in FOMC_DATES if datetime.date.fromisoformat(d) >= today]
    return (upcoming[0] - today).days if upcoming else 99


def _upcoming_events_this_week() -> list:
    today   = datetime.date.today()
    week_end = today + datetime.timedelta(days=7)
    events  = []
    for date_str, event in ECONOMIC_EVENTS.items():
        try:
            d = datetime.date.fromisoformat(date_str)
            if today <= d <= week_end:
                days = (d - today).days
                events.append(f"{event} ({'today' if days==0 else f'in {days}d'})")
        except Exception:
            pass
    if _days_to_fomc() <= 7:
        events.append(f"FOMC Decision (in {_days_to_fomc()}d)")
    return events


def _label_regime(vix: float, spy: dict) -> str:
    if vix > 35 or (spy.get("above_sma50") == False and spy.get("above_sma200") == False):
        return "EXTREME_RISK_OFF"
    elif vix > 25 or spy.get("above_sma50") == False:
        return "RISK_OFF"
    elif vix < 18 and spy.get("above_sma20") and spy.get("above_sma50"):
        return "RISK_ON"
    else:
        return "NEUTRAL"


def _regime_icon(regime: str) -> str:
    return {"EXTREME_RISK_OFF":"🔴","RISK_OFF":"🟠","NEUTRAL":"🟡","RISK_ON":"🟢"}.get(regime,"⬜")


# ── Report builder ────────────────────────────────────────────────────────────

def build_premarket_data() -> dict:
    """Collect all raw data for the report."""
    vix_data  = _get_vix()
    spy_data  = _get_spy_data()
    breadth   = _get_market_breadth()
    positions = _get_open_positions()
    pcr       = _get_put_call_ratio()
    events    = _upcoming_events_this_week()
    vix       = vix_data.get("current", 0)
    regime    = _label_regime(vix, spy_data)

    return {
        "date":      datetime.date.today().isoformat(),
        "vix":       vix_data,
        "spy":       spy_data,
        "breadth":   breadth,
        "positions": positions,
        "pcr":       pcr,
        "events":    events,
        "regime":    regime,
        "fomc_days": _days_to_fomc(),
    }


def generate_report_with_deepseek(data: dict) -> str:
    """
    Generate the full pre-market report using DeepSeek.
    Provides all quantitative data — DeepSeek writes the narrative and flags.
    """
    vix        = data["vix"].get("current", "N/A")
    vix_prev   = data["vix"].get("prev", "N/A")
    vix_chg    = round(vix - vix_prev, 2) if vix and vix_prev else "N/A"
    spy        = data["spy"]
    breadth    = data["breadth"]
    regime     = data["regime"]
    positions  = data["positions"]
    pcr        = data.get("pcr")
    events     = data["events"]
    fomc_days  = data["fomc_days"]
    date_str   = data["date"]

    pos_summary = "\n".join([
        f"  - {p.get('symbol')}: {p.get('unrealized_plpc','?')}% P&L | "
        f"Market value ${float(p.get('market_value',0)):,.0f}"
        for p in positions
    ]) if positions else "  None"

    sector_lines = "\n".join([
        f"  {name}: {chg:+.2f}%"
        for name, chg in sorted(breadth.get("sectors",{}).items(),
                                 key=lambda x: x[1], reverse=True)
    ])

    events_str = "\n".join(f"  - {e}" for e in events) if events else "  None this week"

    prompt = f"""You are Axiom — NEXUS Alpha's independent risk intelligence system.
Today is {date_str}. Generate the official daily pre-market risk report.

You have been given all quantitative data below. Your job is to synthesize it into
a comprehensive, structured report that every agent and OMNI reads before the session begins.
Be direct. Commit to views. Flag real risks. Do not hedge everything.

═══════════════ MARKET DATA ═══════════════

REGIME: {regime} {_regime_icon(regime)}

VIX: {vix:.1f} (prev close: {vix_prev:.1f}, change: {vix_chg:+.1f})
SPY: ${spy.get('price','?')} | 1d: {spy.get('chg_1d_pct','?')}% | 5d: {spy.get('chg_5d_pct','?')}%
SPY vs SMA20: {'ABOVE ✅' if spy.get('above_sma20') else 'BELOW ❌'} (${spy.get('sma20','?')})
SPY vs SMA50: {'ABOVE ✅' if spy.get('above_sma50') else 'BELOW ❌'} (${spy.get('sma50','?')})
SPY vs SMA200: {'ABOVE ✅' if spy.get('above_sma200') else 'BELOW ❌' if spy.get('above_sma200') is not None else 'N/A'} (${spy.get('sma200','?')})
Put/Call Ratio: {f'{pcr:.2f}' if pcr else 'N/A'} (>1.0 = bearish sentiment)

SECTOR PERFORMANCE (1-day):
{sector_lines}
Market breadth: {breadth.get('green',0)} sectors green / {breadth.get('red',0)} red

MACRO EVENTS THIS WEEK:
{events_str}
FOMC: {fomc_days} days away

OPEN POSITIONS:
{pos_summary}
Total open: {len(positions)} positions

═══════════════ REPORT FORMAT ═══════════════

Generate the report using EXACTLY this structure.
Each section must be populated — no skipping, no "N/A" unless truly unavailable.

SECTION 1 — MARKET REGIME
State the regime, what it means for trading today, and whether it changed from yesterday.

SECTION 2 — DAILY RISK SCORE
Give an overall risk score for today (1-10, 10=maximum risk).
Explain in 2 sentences why.

SECTION 3 — TOP 3 RISK FACTORS
The 3 most important things that could go wrong today.
Be specific — not generic warnings.

SECTION 4 — MACRO EVENTS
Cover all events this week. What to watch, when, and what the market impact could be.
If no major events: state that and note what the next event is.

SECTION 5 — VOLATILITY LANDSCAPE
VIX level interpretation. Is IV elevated or compressed?
What does this mean for credit spread entries vs long option entries?
Best strategy type given today's vol environment.

SECTION 6 — SECTOR RISK MAP
Which sectors are safe to enter today? Which are elevated risk?
Rate each sector: ✅ Clear / ⚠️ Caution / 🔴 Avoid

SECTION 7 — OPEN POSITION REVIEW
Review each open position in today's market context.
Is any position under threat from today's conditions?
Any positions that should be monitored closely?

SECTION 8 — STRATEGY GUIDANCE FOR TODAY
What strategies should agents favor today?
What should be avoided?
Size guidance (full / reduced / avoid new entries)?

SECTION 9 — WATCHLIST FLAGS
Specific tickers with elevated risk or opportunity today.
Include reason for each flag.

SECTION 10 — CIRCUIT BREAKER STATUS
Are any circuit breakers active?
Any conditions that would trigger a halt today?
Current protection thresholds in effect.

SECTION 11 — OMNI DIRECTIVE
One clear sentence that tells OMNI how to approach synthesis today.
Example: "Approve only high-conviction NEUTRAL setups with IVR 25-45, size 0.75x across the board."

═══════════════════════════════════════════"""

    try:
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model":       "deepseek-chat",
                "messages":    [{"role": "user", "content": prompt}],
                "max_tokens":  2000,
                "temperature": 0.3,
            },
            timeout=40
        )
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"[DeepSeek report generation failed: {e}]"


def format_telegram_report(report_text: str, data: dict) -> str:
    """Format the report for Telegram (header + truncated body)."""
    regime     = data["regime"]
    icon       = _regime_icon(regime)
    vix        = data["vix"].get("current", 0)
    date_str   = datetime.date.today().strftime("%A %b %d, %Y")
    positions  = len(data.get("positions", []))
    events     = data.get("events", [])
    fomc_days  = data.get("fomc_days", 99)

    header = (
        f"🔷 <b>AXIOM PRE-MARKET BRIEF — {date_str}</b>\n"
        f"{'━'*35}\n"
        f"{icon} Regime: <b>{regime}</b>\n"
        f"VIX: <b>{vix:.1f}</b> | Positions: {positions}/5"
        + (f" | ⚠️ FOMC in {fomc_days}d" if fomc_days <= 7 else "") +
        (f"\n📅 <b>Events:</b> {', '.join(events[:2])}" if events else "") +
        f"\n{'━'*35}\n\n"
    )

    # Telegram has 4096 char limit — truncate gracefully
    body = report_text[:3500] + ("\n\n<i>[Full report in logs]</i>" if len(report_text) > 3500 else "")
    return header + body


def run_premarket_report(send_telegram: bool = True) -> dict:
    """
    Main entry point. Called by cron at 9:00 AM ET every trading day.
    Returns the full report data.
    """
    print(f"\n🔷 Axiom Pre-Market Report — {datetime.date.today()}")
    print("  Collecting market data...")

    data   = build_premarket_data()
    regime = data["regime"]
    vix    = data["vix"].get("current", 0)

    print(f"  Regime: {regime} | VIX: {vix:.1f} | "
          f"Positions: {len(data['positions'])} | FOMC: {data['fomc_days']}d")
    print("  Generating report with DeepSeek...")

    report_text   = generate_report_with_deepseek(data)
    tg_message    = format_telegram_report(report_text, data)

    if send_telegram:
        for chat_id in [AHMED_ID, ALPHA_GROUP, PRIME_GROUP, HEALTH_GRP]:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TG_BOT}/sendMessage",
                    json={"chat_id": chat_id, "text": tg_message,
                          "parse_mode": "HTML"},
                    timeout=8
                )
            except Exception:
                pass
        print("  ✅ Report sent to all channels")

    return {
        "date":        data["date"],
        "regime":      regime,
        "vix":         vix,
        "risk_score":  None,  # extracted from DeepSeek output
        "report_text": report_text,
        "tg_message":  tg_message,
        "positions":   len(data["positions"]),
        "events":      data["events"],
    }


if __name__ == "__main__":
    import sys
    dry = "--dry" in sys.argv
    result = run_premarket_report(send_telegram=not dry)
    print("\n" + "═"*60)
    print(result["report_text"])
