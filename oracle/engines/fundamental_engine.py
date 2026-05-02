"""
ORACLE — Engine 6: Fundamental Engine
Sources: Alpha Vantage (earnings) + SEC EDGAR (insider transactions, institutional)
Cache TTL: 24 hours
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import cache
import config
from clients import alpha_vantage_client, edgar_client, market_chameleon_client, polygon_client
from models import FundamentalData, InsiderTransaction

logger = logging.getLogger(__name__)

ENGINE = "fundamental"


def fetch(ticker: str, card_type: str = "full") -> tuple[Optional[FundamentalData], str]:
    """
    Fetch fundamental intelligence for a ticker.

    For preliminary cards: earnings date and days_to_earnings only.
    For full cards: complete fundamental + insider + institutional data.

    Args:
        ticker:    Stock ticker symbol.
        card_type: "preliminary" or "full".

    Returns:
        Tuple of (FundamentalData or None, freshness string).
    """
    start = time.monotonic()

    cached = cache.get(ticker, ENGINE, card_type)
    if cached is not None:
        cache.log_api_call(ENGINE, "edgar", ticker,
                           int((time.monotonic() - start) * 1000),
                           True, cache_hit=True)
        return FundamentalData(**cached), config.FRESHNESS_LIVE

    # Always fetch earnings data (needed for preliminary cards too)
    # ORATS hist/earnings is the primary source (unlimited quota, $399/mo plan).
    # Alpha Vantage is fallback only — free tier exhausts at 5 calls/min during warmup.
    quarterly = _get_earnings_history(ticker)

    # Find next earnings date from history (most recent quarter gives approximate timing)
    earnings_date, days_to_earnings = _estimate_next_earnings(quarterly)

    # Polygon financials: revenue growth + margin trend (D5 scorer inputs)
    fin = polygon_client.get_financials(ticker) or {}
    rev_growth   = fin.get("revenue_growth_yoy")
    margin_trend = fin.get("margin_trend", "FLAT")

    # Analyst revision bias: derived from EPS beat rate over last 8 quarters
    beats = sum(1 for q in quarterly[:8] if q.get("beat"))
    total = len(quarterly[:8])
    if total >= 4:
        beat_rate = beats / total
        if beat_rate >= 0.75:   analyst_bias = "POSITIVE"
        elif beat_rate >= 0.50: analyst_bias = "NEUTRAL"
        else:                    analyst_bias = "NEGATIVE"
    else:
        analyst_bias = "NEUTRAL"

    fundamental = FundamentalData(
        earnings_date=earnings_date,
        days_to_earnings=days_to_earnings,
        earnings_clear_25d=days_to_earnings is None or days_to_earnings > 25,
        earnings_clear_45d=days_to_earnings is None or days_to_earnings > 45,
        last_8_surprises=quarterly[:8],
        revenue_growth_yoy=rev_growth,
        margin_trend=margin_trend,
        analyst_revision_bias=analyst_bias,
    )

    if card_type == "full":
        # Fetch insider data from EDGAR
        insider_raw = edgar_client.get_insider_transactions(ticker, days=90)
        transactions = [
            InsiderTransaction(
                date=t.get("file_date", ""),
                insider_name="; ".join(t.get("filers", [])),
                role="",
                transaction_type="Form 4",
                shares=None,
                price=None,
                total_value=None,
            )
            for t in insider_raw
        ]

        cluster = len(transactions) >= 3
        # OMNI Pass 3 Finding 3: pass raw dicts (not Pydantic objects) so
        # _compute_insider_bias can read the "direction" field populated by
        # edgar_client. InsiderTransaction Pydantic model doesn't have a
        # .get() method — raw dicts do.
        net_bias = _compute_insider_bias(insider_raw)

        # Earnings move history from Market Chameleon
        mc_earnings = market_chameleon_client.get_earnings_move_history(ticker) or {}

        fundamental.insider_transactions_90d = transactions
        fundamental.insider_net_bias = net_bias
        fundamental.insider_cluster_flag = cluster
        fundamental.avg_actual_move_pct = mc_earnings.get("avg_actual_move_pct")
        fundamental.implied_move_pct = mc_earnings.get("current_implied_move_pct")

    cache.set(ticker, ENGINE, fundamental.model_dump(), config.FUNDAMENTAL_TTL, card_type)
    cache.log_api_call(ENGINE, "edgar", ticker,
                       int((time.monotonic() - start) * 1000), True)
    return fundamental, config.FRESHNESS_LIVE


def _get_earnings_history(ticker: str) -> list:
    """
    Fetch earnings history. ORATS primary (unlimited), AV fallback (5 calls/min).
    Returns list of dicts with 'date', 'beat', 'surprise_pct' keys.
    """
    # Try ORATS hist/earnings first
    try:
        import requests as _req
        ORATS_TOKEN = "4476e955-241a-4540-b114-ebbf1a3a3b87"
        resp = _req.get(
            "https://api.orats.io/datav2/hist/earnings",
            params={"token": ORATS_TOKEN, "ticker": ticker, "limit": 12},
            timeout=8,
        )
        rows = resp.json().get("data", [])
        if rows:
            # Sort descending by earnDate
            rows.sort(key=lambda r: r.get("earnDate", ""), reverse=True)
            quarterly = []
            for r in rows[:8]:
                quarterly.append({
                    "date": r.get("earnDate", ""),
                    "estimated_eps": None,
                    "actual_eps": None,
                    "surprise_pct": None,
                    "beat": False,
                })
            return quarterly
    except Exception as e:
        logger.debug("ORATS earnings history failed for %s: %s", ticker, e)

    # Fallback: Alpha Vantage
    try:
        av_data = alpha_vantage_client.get_earnings(ticker)
        return av_data.get("quarterly_history", [])
    except Exception as e:
        logger.debug("AV earnings fallback failed for %s: %s", ticker, e)
    return []


def _estimate_next_earnings(quarterly: list) -> tuple[Optional[str], Optional[int]]:
    """
    Estimate next earnings date from historical quarterly pattern.
    If quarterly data unavailable, returns (None, None).
    """
    if not quarterly:
        return None, None

    # Most recent reported date
    try:
        last_date_str = quarterly[0].get("date", "")
        if not last_date_str:
            return None, None
        last_date = datetime.strptime(last_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        # Approximate next earnings ~91 days after last
        from datetime import timedelta
        next_date = last_date + timedelta(days=91)
        today = datetime.now(tz=timezone.utc)
        days_to = (next_date - today).days
        if days_to < 0:
            # Already passed — add another quarter
            next_date = next_date + timedelta(days=91)
            days_to = (next_date - today).days
        return next_date.strftime("%Y-%m-%d"), max(0, days_to)
    except (ValueError, TypeError):
        return None, None


def _compute_insider_bias(transactions: list) -> str:
    """
    Determine net insider bias from Form 4 transactions.

    OMNI Pass 3 Finding 3: prior code counted total filings without examining
    direction — both insider buying AND selling returned "STRONG_BUYING".
    Insider selling is a bearish signal; counting it as bullish corrupts synthesis.

    Transactions with direction "buy" or "sell" are counted separately.
    Transactions without a known direction (current EDGAR search API limitation)
    are treated as NEUTRAL rather than optimistically assumed to be buys.

    Transaction direction codes (SEC Form 4):
      P = Open-market purchase (bullish)
      S = Open-market sale (bearish)
      F = Forfeiture (neutral)
      M = Option exercise (neutral — often accompanied by an S sale)
      A = Grant/award (neutral — not a voluntary open-market action)
    """
    if not transactions:
        return "NEUTRAL"

    buys  = sum(1 for t in transactions if t.get("direction") == "buy")
    sells = sum(1 for t in transactions if t.get("direction") == "sell")

    # If direction is unknown for all transactions (EDGAR search doesn't return
    # individual transaction codes), return NEUTRAL rather than crying wolf.
    if buys == 0 and sells == 0:
        return "NEUTRAL"

    if sells > buys * 2:
        return "STRONG_SELLING" if sells >= 3 else "SELLING"
    if buys > sells * 2:
        return "STRONG_BUYING" if buys >= 3 else "BUYING"
    return "NEUTRAL"
