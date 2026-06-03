"""
earnings_calendar.py — Earnings Calendar Engine
================================================
Downloads and maintains a rolling 14-day earnings calendar.
Classifies each event by expected move size and liquidity.
Identifies pre-earnings and post-earnings opportunities.

Data source: Alpha Vantage EARNINGS_CALENDAR (free, reliable)
Supplemented by: Polygon price/volume, ORATS IV rank

S&P 500 + NASDAQ 100 universe only.
"""
from __future__ import annotations
import csv
import io
import logging
import os
import sqlite3
import time
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("prime_v2.earnings_calendar")
_ET = ZoneInfo("America/New_York")

AV_KEY       = os.getenv("ALPHA_VANTAGE_KEY", "5LPKGHYMW9ZK24KL")
POLYGON_KEY  = os.getenv("POLYGON_API_KEY", "ZMEnZ_2GURw_UqbufU5npd49ZDeptMhl")
ORATS_TOKEN  = os.getenv("ORATS_TOKEN", "4476e955-241a-4540-b114-ebbf1a3a3b87")
CALENDAR_DB  = os.getenv("PRIME_V2_DB",
               "/Users/ahmedsadek/nexus/data/prime_v2.db")

# S&P 500 + NASDAQ 100 universe
SP500_NASDAQ100 = {
    "AAPL","MSFT","NVDA","AMZN","GOOGL","GOOG","META","TSLA","AVGO","BRK",
    "JPM","LLY","V","UNH","XOM","MA","COST","HD","PG","JNJ","ABBV","MRK",
    "CVX","CRM","BAC","NFLX","AMD","PEP","KO","TMO","ORCL","MCD","ABT",
    "CSCO","GE","IBM","NOW","QCOM","TXN","ISRG","INTU","AMGN","RTX","GS",
    "SPGI","BKNG","CAT","DHR","LOW","HON","UNP","AXP","ELV","PLD","VRTX",
    "NEE","AMAT","PANW","REGN","ADI","LRCX","SLB","MU","KLAC","BSX","GILD",
    "MDT","ICE","CME","TJX","DE","MMC","ZTS","ETN","SHW","MDLZ","DUK","SO",
    "WM","CDNS","SNPS","MRVL","NXPI","FTNT","CRWD","DDOG","SNOW","WDAY",
    "TEAM","ZS","NET","OKTA","MDB","HUBS","VEEV","COUP","PAYC","ANSS",
    "INTC","ADSK","PAYX","FAST","ODFL","CTAS","ROK","PH","EMR","DOV",
    "WDC","STX","HPQ","HPE","NTAP","KEYS","TRMB","FFIV","AKAM","CDW",
    "DLTR","DG","TGT","WMT","COST","TJX","ROST","BBY","ULTA","AN",
    "F","GM","TSLA","HOG","LKQ","APTV","BWA","LEA","DAN",
    "JPM","BAC","WFC","GS","MS","C","USB","TFC","PNC","COF","AXP",
    "BLK","SCHW","CB","MMC","AON","MET","PRU","AFL","ALL","PGR",
    "AMGN","GILD","BIIB","REGN","VRTX","ALXN","BMRN","EXEL","SAGE",
    "JNJ","ABT","TMO","DHR","MDT","BSX","SYK","ZBH","BAX","BDX",
    "UNH","CVS","CI","HUM","CNC","MOH","HCA","THC","UHS","DVA",
    "XOM","CVX","COP","EOG","PXD","MPC","VLO","PSX","HES","DVN",
    "NEE","DUK","SO","D","AEP","EXC","SRE","PEG","ES","XEL",
    "AMT","PLD","CCI","EQIX","PSA","SPG","WY","AVB","EQR","MAA",
}


def init_db() -> None:
    conn = sqlite3.connect(CALENDAR_DB, timeout=10)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS earnings_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            report_date     TEXT NOT NULL,
            fiscal_end      TEXT,
            eps_estimate    REAL,
            timing          TEXT,
            avg_move_pct    REAL,
            iv_rank         REAL,
            adv_30          REAL,
            tier            TEXT,
            pre_entry_date  TEXT,
            post_entry_date TEXT,
            status          TEXT DEFAULT 'UPCOMING',
            actual_eps      REAL,
            actual_move_pct REAL,
            created_at      TEXT,
            updated_at      TEXT,
            UNIQUE(ticker, report_date)
        );

        CREATE TABLE IF NOT EXISTS historical_moves (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            report_date TEXT NOT NULL,
            move_pct    REAL,
            direction   TEXT,
            beat        INTEGER,
            ts          REAL,
            UNIQUE(ticker, report_date)
        );

        CREATE INDEX IF NOT EXISTS idx_events_date
            ON earnings_events(report_date, status);
        CREATE INDEX IF NOT EXISTS idx_events_tier
            ON earnings_events(tier, report_date);
    """)
    conn.commit()
    conn.close()
    log.info("Prime V2 DB initialized: %s", CALENDAR_DB)


# ---------------------------------------------------------------------------
# Earnings calendar fetch
# ---------------------------------------------------------------------------

def fetch_earnings_calendar(horizon_days: int = 14) -> list[dict]:
    """
    Fetch upcoming earnings for S&P500 + NASDAQ100 universe.
    Returns list of events within horizon_days.
    """
    try:
        r = requests.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "EARNINGS_CALENDAR",
                "horizon":  "3month",
                "apikey":   AV_KEY,
            },
            timeout=30,
        )
        if r.status_code != 200:
            log.error("Alpha Vantage earnings calendar failed: %d", r.status_code)
            return []

        reader  = csv.DictReader(io.StringIO(r.text))
        today   = date.today()
        horizon = today + timedelta(days=horizon_days)
        events  = []

        for row in reader:
            ticker      = row.get("symbol","").upper()
            report_date = row.get("reportDate","")
            timing      = row.get("timeOfTheDay","")
            eps_est     = row.get("estimate","")

            if ticker not in SP500_NASDAQ100:
                continue

            try:
                rdate = date.fromisoformat(report_date)
                if rdate < today or rdate > horizon:
                    continue
            except ValueError:
                continue

            events.append({
                "ticker":       ticker,
                "report_date":  report_date,
                "fiscal_end":   row.get("fiscalDateEnding",""),
                "eps_estimate": float(eps_est) if eps_est else None,
                "timing":       timing or "unknown",
            })

        log.info("Earnings calendar: %d events in next %d days", len(events), horizon_days)
        return events

    except Exception as exc:
        log.error("Earnings calendar fetch failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Historical move analysis
# ---------------------------------------------------------------------------

def get_historical_avg_move(ticker: str) -> Optional[float]:
    """
    Get average absolute earnings move for ticker over last 8 quarters.
    Uses Polygon daily bars around known earnings dates.
    """
    try:
        # Check cache first
        conn = sqlite3.connect(CALENDAR_DB, timeout=5)
        rows = conn.execute("""
            SELECT AVG(ABS(move_pct)) FROM historical_moves
            WHERE ticker=? AND ts > ?
        """, (ticker, time.time() - 86400 * 30)).fetchone()
        conn.close()

        if rows and rows[0] and rows[0] > 0:
            return round(rows[0], 2)

        # Fetch from Polygon — look at last 2 years of daily bars
        end   = date.today()
        start = end - timedelta(days=730)

        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day"
            f"/{start}/{end}",
            params={
                "adjusted": "true",
                "sort":     "asc",
                "limit":    500,
                "apiKey":   POLYGON_KEY,
            },
            timeout=10,
        )

        if r.status_code != 200:
            return None

        bars = r.json().get("results", [])
        if len(bars) < 2:
            return None

        # Find large gap days (>4% = likely earnings)
        moves = []
        for i in range(1, len(bars)):
            prev_close = bars[i-1]["c"]
            curr_open  = bars[i]["o"]
            gap_pct    = (curr_open - prev_close) / prev_close * 100
            if abs(gap_pct) >= 4.0:
                moves.append(abs(gap_pct))

        if not moves:
            return None

        avg = sum(moves) / len(moves)
        return round(avg, 2)

    except Exception as exc:
        log.warning("Historical move fetch failed for %s: %s", ticker, exc)
        return None


def get_iv_rank(ticker: str) -> Optional[float]:
    """Get current IV rank from ORATS."""
    try:
        today = date.today()
        r = requests.get(
            "https://api.orats.io/datav2/hist/dailies",
            params={
                "token":     ORATS_TOKEN,
                "ticker":    ticker,
                "fields":    "ticker,tradeDate,ivRank",
                "startDate": (today - timedelta(days=5)).isoformat(),
                "endDate":   today.isoformat(),
            },
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                iv_rank = data[-1].get("ivRank")
                if iv_rank is not None:
                    return float(iv_rank)
    except Exception:
        pass
    return None


def get_avg_daily_volume(ticker: str) -> Optional[float]:
    """Get 30-day average daily volume from Polygon."""
    try:
        end   = date.today()
        start = end - timedelta(days=45)
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day"
            f"/{start}/{end}",
            params={"adjusted":"true","sort":"desc","limit":30,"apiKey":POLYGON_KEY},
            timeout=8,
        )
        if r.status_code == 200:
            bars = r.json().get("results",[])
            if bars:
                return sum(b["v"] for b in bars) / len(bars)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------

def classify_tier(
    avg_move_pct: Optional[float],
    iv_rank:      Optional[float],
    adv_30:       Optional[float],
) -> str:
    """
    Classify earnings event into ROI tier.

    TIER_A: Expected 10-15%+ move
    TIER_B: Expected 5-10% move
    TIER_C: Expected 2-5% move
    SKIP:   Not worth trading
    """
    if avg_move_pct is None:
        return "TIER_C"   # Unknown — be conservative

    # Liquidity gate: minimum $10M average daily volume
    if adv_30 and adv_30 < 1_000_000:
        return "SKIP"

    # High IV rank = market expects big move = good for both arms
    iv_boost = 1.0
    if iv_rank and iv_rank >= 60:
        iv_boost = 1.2
    elif iv_rank and iv_rank >= 40:
        iv_boost = 1.1

    adjusted_move = avg_move_pct * iv_boost

    if adjusted_move >= 9.0:
        return "TIER_A"
    elif adjusted_move >= 5.0:
        return "TIER_B"
    elif adjusted_move >= 2.5:
        return "TIER_C"
    else:
        return "SKIP"


def get_entry_dates(
    report_date: str,
    timing:      str,
) -> tuple[Optional[str], Optional[str]]:
    """
    Calculate pre-earnings and post-earnings entry dates.

    Pre-earnings: 4 trading days before report (avoid 3-day window to be safe)
    Post-earnings: day after report (morning after beat)
    """
    try:
        rdate = date.fromisoformat(report_date)

        # Pre-earnings entry: 4 calendar days before
        pre_entry = rdate - timedelta(days=4)
        # Skip weekends
        while pre_entry.weekday() >= 5:
            pre_entry -= timedelta(days=1)

        # Post-earnings entry: next trading day after report
        if timing == "pre-market":
            # Reports before market: trade same day after 10 AM
            post_entry = rdate
        else:
            # Reports after close: trade next morning
            post_entry = rdate + timedelta(days=1)
            while post_entry.weekday() >= 5:
                post_entry += timedelta(days=1)

        return pre_entry.isoformat(), post_entry.isoformat()

    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Main calendar builder
# ---------------------------------------------------------------------------

def build_calendar() -> list[dict]:
    """
    Build complete earnings calendar with tier classifications.
    Saves to DB. Returns list of actionable events.
    Called daily at 7:00 AM ET.
    """
    init_db()
    events  = fetch_earnings_calendar(horizon_days=14)
    results = []
    now     = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(CALENDAR_DB, timeout=10)

    for event in events:
        ticker      = event["ticker"]
        report_date = event["report_date"]
        timing      = event["timing"]

        # Enrich with move history, IV rank, volume
        avg_move = get_historical_avg_move(ticker)
        iv_rank  = get_iv_rank(ticker)
        adv      = get_avg_daily_volume(ticker)
        tier     = classify_tier(avg_move, iv_rank, adv)
        pre_d, post_d = get_entry_dates(report_date, timing)

        try:
            conn.execute("""
                INSERT OR REPLACE INTO earnings_events
                (ticker, report_date, fiscal_end, eps_estimate, timing,
                 avg_move_pct, iv_rank, adv_30, tier,
                 pre_entry_date, post_entry_date, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                ticker, report_date,
                event.get("fiscal_end"),
                event.get("eps_estimate"),
                timing,
                avg_move, iv_rank, adv,
                tier, pre_d, post_d,
                "UPCOMING", now, now,
            ))
        except Exception as exc:
            log.warning("DB insert failed for %s: %s", ticker, exc)
            continue

        if tier != "SKIP":
            results.append({
                "ticker":        ticker,
                "report_date":   report_date,
                "timing":        timing,
                "avg_move_pct":  avg_move,
                "iv_rank":       iv_rank,
                "tier":          tier,
                "pre_entry":     pre_d,
                "post_entry":    post_d,
            })

        log.info("%s: report=%s tier=%s avg_move=%.1f%% iv_rank=%s",
                ticker, report_date, tier,
                avg_move or 0, iv_rank or "N/A")

    conn.commit()
    conn.close()

    tier_a = sum(1 for r in results if r["tier"]=="TIER_A")
    tier_b = sum(1 for r in results if r["tier"]=="TIER_B")
    tier_c = sum(1 for r in results if r["tier"]=="TIER_C")
    log.info("Calendar built: %d events — A:%d B:%d C:%d",
             len(results), tier_a, tier_b, tier_c)
    return results


def get_todays_opportunities() -> dict[str, list[dict]]:
    """
    Get today's actionable opportunities from calendar.
    Returns dict with pre_earnings and post_earnings lists.
    """
    init_db()
    today = date.today().isoformat()
    conn  = sqlite3.connect(CALENDAR_DB, timeout=5)
    conn.row_factory = sqlite3.Row

    # Pre-earnings: today is the pre-entry date
    pre = conn.execute("""
        SELECT * FROM earnings_events
        WHERE pre_entry_date=? AND status='UPCOMING' AND tier != 'SKIP'
        ORDER BY tier ASC, avg_move_pct DESC
    """, (today,)).fetchall()

    # Post-earnings: today is the post-entry date
    post = conn.execute("""
        SELECT * FROM earnings_events
        WHERE post_entry_date=? AND status='UPCOMING' AND tier != 'SKIP'
        ORDER BY tier ASC, avg_move_pct DESC
    """, (today,)).fetchall()

    conn.close()
    return {
        "pre_earnings":  [dict(r) for r in pre],
        "post_earnings": [dict(r) for r in post],
    }


def get_upcoming_events(days: int = 7) -> list[dict]:
    """Get all upcoming events in next N days — for planning."""
    init_db()
    today  = date.today().isoformat()
    future = (date.today() + timedelta(days=days)).isoformat()
    conn   = sqlite3.connect(CALENDAR_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    rows   = conn.execute("""
        SELECT * FROM earnings_events
        WHERE report_date >= ? AND report_date <= ?
        AND status='UPCOMING' AND tier != 'SKIP'
        ORDER BY tier ASC, report_date ASC
    """, (today, future)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
