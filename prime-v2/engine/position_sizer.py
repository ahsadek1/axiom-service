"""
position_sizer.py — Dynamic Position Sizing Engine
====================================================
Capital allocation based on conviction score and tier.

Base capital: $100,000
Base position: $5,000 (5% of capital)
Maximum position: $10,000 (10% of capital)
Maximum concurrent: 15 positions
Capital reserve: 20% always held

Sizing matrix:
  Tier A + conviction 90-100 → 2.0x = $10,000
  Tier A + conviction 80-89  → 1.5x = $7,500
  Tier B + conviction 70-79  → 1.0x = $5,000
  Tier B + conviction 55-69  → 0.7x = $3,500
  Tier C + any conviction    → 0.5x = $2,500

Agent confirmation adjustments:
  3/3 agents agree → +20% to size
  2/3 agents agree → base size
  1/3 agents agree → -30% to size (Tier A only)
"""
from __future__ import annotations
import logging
import os
import sqlite3
from typing import Optional

log = logging.getLogger("prime_v2.sizer")

TOTAL_CAPITAL      = float(os.getenv("PRIME_V2_CAPITAL",    "100000"))
BASE_POSITION      = float(os.getenv("PRIME_V2_BASE_POS",   "5000"))
MAX_POSITION       = float(os.getenv("PRIME_V2_MAX_POS",    "10000"))
MAX_CONCURRENT     = int(os.getenv("PRIME_V2_MAX_CONC",     "15"))
CAPITAL_RESERVE    = float(os.getenv("PRIME_V2_RESERVE",    "0.20"))
PRIME_V2_DB        = os.getenv("PRIME_V2_DB",
                    "/Users/ahmedsadek/nexus/data/prime_v2.db")


def get_available_capital() -> float:
    """
    Available capital = total - reserved - deployed in open positions.
    """
    try:
        conn = sqlite3.connect(PRIME_V2_DB, timeout=5)
        row  = conn.execute("""
            SELECT COALESCE(SUM(position_size_usd), 0)
            FROM prime_v2_positions
            WHERE status='OPEN'
        """).fetchone()
        conn.close()
        deployed = float(row[0]) if row else 0.0
    except Exception:
        deployed = 0.0

    reserve   = TOTAL_CAPITAL * CAPITAL_RESERVE
    available = TOTAL_CAPITAL - reserve - deployed
    return max(0.0, available)


def get_open_position_count() -> int:
    """Count currently open positions."""
    try:
        conn = sqlite3.connect(PRIME_V2_DB, timeout=5)
        row  = conn.execute(
            "SELECT COUNT(*) FROM prime_v2_positions WHERE status='OPEN'"
        ).fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def calculate_position_size(
    conviction:         float,
    tier:               str,
    agent_count:        int = 2,
    available_capital:  Optional[float] = None,
) -> dict:
    """
    Calculate position size for a trade.

    Args:
        conviction:        0-100 conviction score from ranker
        tier:              TIER_A / TIER_B / TIER_C
        agent_count:       number of agents that confirmed (1, 2, or 3)
        available_capital: override available capital (for testing)

    Returns:
        dict with position_size_usd, shares_estimate, size_mult, rationale
    """
    if available_capital is None:
        available_capital = get_available_capital()

    open_count = get_open_position_count()

    # Position limit check
    if open_count >= MAX_CONCURRENT:
        return {
            "approved":         False,
            "position_size_usd": 0,
            "reason":           f"Position limit reached: {open_count}/{MAX_CONCURRENT}",
        }

    # Base size multiplier from conviction
    if conviction >= 90:   base_mult = 2.0
    elif conviction >= 80: base_mult = 1.5
    elif conviction >= 70: base_mult = 1.0
    elif conviction >= 55: base_mult = 0.7
    else:                  base_mult = 0.5

    # Tier adjustment
    tier_mult = {"TIER_A": 1.0, "TIER_B": 0.85, "TIER_C": 0.65}.get(tier, 0.65)

    # Agent confirmation adjustment
    if agent_count >= 3:   agent_mult = 1.20
    elif agent_count == 2: agent_mult = 1.00
    elif agent_count == 1: agent_mult = 0.70
    else:                  agent_mult = 0.50

    # Only allow single-agent confirmation for Tier A
    if agent_count == 1 and tier != "TIER_A":
        return {
            "approved":         False,
            "position_size_usd": 0,
            "reason":           "Single agent confirmation only allowed for Tier A",
        }

    # Calculate final size
    raw_size      = BASE_POSITION * base_mult * tier_mult * agent_mult
    position_size = min(raw_size, MAX_POSITION)
    position_size = min(position_size, available_capital * 0.80)
    position_size = round(position_size / 100) * 100   # round to $100

    if position_size < 500:
        return {
            "approved":         False,
            "position_size_usd": 0,
            "reason":           f"Position size ${position_size:.0f} below minimum $500",
        }

    rationale = (
        f"Conv={conviction:.0f} {tier} agents={agent_count}/3 "
        f"base_mult={base_mult:.1f}x tier_mult={tier_mult:.2f} "
        f"agent_mult={agent_mult:.2f} → ${position_size:,.0f}"
    )

    log.info("Position sized: %s", rationale)

    return {
        "approved":          True,
        "position_size_usd": position_size,
        "size_mult":         base_mult * tier_mult * agent_mult,
        "available_capital": available_capital,
        "open_positions":    open_count,
        "rationale":         rationale,
    }


def print_sizing_matrix() -> None:
    """Print full sizing matrix for review."""
    print(f"\nPrime V2 Position Sizing Matrix")
    print(f"Total Capital: ${TOTAL_CAPITAL:,.0f} | Base: ${BASE_POSITION:,.0f} | Max: ${MAX_POSITION:,.0f}")
    print(f"Reserve: {CAPITAL_RESERVE:.0%} | Max Concurrent: {MAX_CONCURRENT}")
    print()
    print(f"{'Conviction':>12} {'Tier':>8} {'Agents':>8} {'Size':>10} {'Multiplier':>12}")
    print("-" * 55)
    for conv in [95, 85, 75, 60, 45]:
        for tier in ["TIER_A", "TIER_B", "TIER_C"]:
            for agents in [3, 2]:
                result = calculate_position_size(conv, tier, agents, 80000)
                if result["approved"]:
                    print(
                        f"{conv:>12} {tier:>8} {agents:>8}/3 "
                        f"${result['position_size_usd']:>8,.0f} "
                        f"{result['size_mult']:>11.2f}x"
                    )
