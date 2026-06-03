"""
opportunity_ranker.py — Unified Opportunity Queue
==================================================
Combines earnings calendar + momentum scanner into one
prioritized opportunity queue.

Scoring logic:
  Base score from tier (A=100, B=70, C=40)
  Bonus for double signal (earnings + momentum = +30)
  Bonus for high expected move (+up to 20)
  Bonus for volume surge (+up to 15)
  Bonus for near 52-week high (+10)
  Penalty for low liquidity (-20)

Output: ranked list of opportunities with:
  - Strategy arm (PRE_EARNINGS / POST_EARNINGS / MOMENTUM)
  - Tier (A/B/C)
  - Conviction score (0-100)
  - Recommended position size multiplier
  - Entry timing
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, date, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .earnings_calendar import get_todays_opportunities, get_upcoming_events
from .momentum_scanner import scan_momentum, get_bearish_momentum

log = logging.getLogger("prime_v2.ranker")
_ET = ZoneInfo("America/New_York")

# Base scores by tier
TIER_SCORES = {"TIER_A": 100, "TIER_B": 70, "TIER_C": 40}

# Position size multipliers by conviction score
def get_size_multiplier(conviction: float) -> float:
    if conviction >= 90:  return 2.0   # $10K on $5K base
    if conviction >= 80:  return 1.5   # $7.5K
    if conviction >= 70:  return 1.0   # $5K base
    if conviction >= 55:  return 0.7   # $3.5K
    return 0.5                          # $2.5K minimum


def score_opportunity(opp: dict) -> float:
    """Calculate conviction score 0-100 for an opportunity."""
    base = TIER_SCORES.get(opp.get("tier","TIER_C"), 40)
    bonus = 0.0

    # Double signal bonus — appears in both earnings and momentum
    if opp.get("double_signal"):
        bonus += 30

    # Expected move bonus
    avg_move = opp.get("avg_move_pct") or opp.get("change_5d") or 0
    if avg_move >= 12:   bonus += 20
    elif avg_move >= 8:  bonus += 12
    elif avg_move >= 5:  bonus += 6

    # Volume surge bonus
    vol_ratio = opp.get("vol_ratio", 1.0)
    if vol_ratio >= 3.0:   bonus += 15
    elif vol_ratio >= 2.0: bonus += 10
    elif vol_ratio >= 1.5: bonus += 5

    # Near 52-week high bonus (momentum confirmation)
    pct_from_high = opp.get("pct_from_52wh", -10)
    if pct_from_high >= -1.0:  bonus += 10
    elif pct_from_high >= -3.0: bonus += 5

    # IV rank bonus (earnings events)
    iv_rank = opp.get("iv_rank")
    if iv_rank and iv_rank >= 60:  bonus += 8
    elif iv_rank and iv_rank >= 40: bonus += 4

    # Timing bonus — pre-market earnings = cleaner signal
    if opp.get("timing") == "pre-market":
        bonus += 5

    return min(100.0, base + bonus * (base / 100))


def get_todays_queue() -> list[dict]:
    """
    Build today's complete opportunity queue.
    Merges earnings events + momentum setups.
    Scores and ranks all candidates.
    Returns sorted list — best first.
    """
    today        = date.today()
    opportunities = []

    # ── Earnings opportunities ────────────────────────────────────────────────
    earnings_today = get_todays_opportunities()
    earnings_tickers = set()

    for event in earnings_today.get("pre_earnings", []):
        ticker = event["ticker"]
        earnings_tickers.add(ticker)
        opp = {
            **event,
            "strategy":    "PRE_EARNINGS",
            "entry_today": True,
            "hold_days":   3,
            "exit_rule":   "exit_1_day_before_earnings",
            "direction":   "bullish",  # pre-earnings default bullish
        }
        opp["conviction"] = score_opportunity(opp)
        opp["size_mult"]  = get_size_multiplier(opp["conviction"])
        opportunities.append(opp)

    for event in earnings_today.get("post_earnings", []):
        ticker = event["ticker"]
        earnings_tickers.add(ticker)
        opp = {
            **event,
            "strategy":    "POST_EARNINGS",
            "entry_today": True,
            "hold_days":   3,
            "exit_rule":   "exit_at_10pct_or_3days",
            "direction":   "TBD",  # direction determined post-gap analysis
        }
        opp["conviction"] = score_opportunity(opp)
        opp["size_mult"]  = get_size_multiplier(opp["conviction"])
        opportunities.append(opp)

    # ── Momentum opportunities ─────────────────────────────────────────────────
    momentum_results = scan_momentum()

    for setup in momentum_results:
        ticker = setup["ticker"]

        # Check for double signal — appears in both earnings and momentum
        double_signal = ticker in earnings_tickers

        opp = {
            **setup,
            "strategy":     "MOMENTUM_BREAKOUT",
            "entry_today":  True,
            "hold_days":    4,
            "exit_rule":    "trailing_stop_8pct",
            "double_signal": double_signal,
            "avg_move_pct": setup.get("change_5d", 0),
        }

        # Upgrade tier on double signal
        if double_signal and opp["tier"] == "TIER_B":
            opp["tier"] = "TIER_A"
            log.info("DOUBLE SIGNAL: %s upgraded to TIER_A", ticker)

        opp["conviction"] = score_opportunity(opp)
        opp["size_mult"]  = get_size_multiplier(opp["conviction"])
        opportunities.append(opp)

    # ── Bearish opportunities (when regime is risk-off) ───────────────────────
    bearish = get_bearish_momentum()
    for setup in bearish[:5]:  # max 5 bearish plays
        opp = {
            **setup,
            "strategy":    "MOMENTUM_BREAKDOWN",
            "entry_today": True,
            "hold_days":   3,
            "exit_rule":   "trailing_stop_8pct",
        }
        opp["conviction"] = score_opportunity(opp)
        opp["size_mult"]  = get_size_multiplier(opp["conviction"])
        opportunities.append(opp)

    # ── Deduplicate — keep highest conviction per ticker ──────────────────────
    seen: dict[str, dict] = {}
    for opp in opportunities:
        ticker = opp["ticker"]
        if ticker not in seen or opp["conviction"] > seen[ticker]["conviction"]:
            seen[ticker] = opp

    # ── Sort by conviction score ──────────────────────────────────────────────
    ranked = sorted(seen.values(), key=lambda x: -x["conviction"])

    # ── Log summary ───────────────────────────────────────────────────────────
    tier_a = sum(1 for o in ranked if o["tier"]=="TIER_A")
    tier_b = sum(1 for o in ranked if o["tier"]=="TIER_B")
    tier_c = sum(1 for o in ranked if o["tier"]=="TIER_C")
    doubles = sum(1 for o in ranked if o.get("double_signal"))

    log.info(
        "Opportunity queue: %d total — A:%d B:%d C:%d doubles:%d",
        len(ranked), tier_a, tier_b, tier_c, doubles
    )

    return ranked


def get_weekly_pipeline() -> list[dict]:
    """
    7-day forward view — what's coming this week.
    Used for planning and position sizing ahead of time.
    """
    upcoming_earnings = get_upcoming_events(days=7)
    momentum_now      = scan_momentum()
    momentum_tickers  = {m["ticker"] for m in momentum_now}

    pipeline = []
    for event in upcoming_earnings:
        double = event["ticker"] in momentum_tickers
        opp = {
            **event,
            "double_signal": double,
        }
        opp["conviction"] = score_opportunity(opp)
        opp["size_mult"]  = get_size_multiplier(opp["conviction"])
        pipeline.append(opp)

    pipeline.sort(key=lambda x: (-x["conviction"], x["report_date"]))
    return pipeline
