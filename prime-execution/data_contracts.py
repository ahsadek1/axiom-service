"""
data_contracts.py — Typed data boundaries for Prime Execution (B1).
Spec: Nexus V1 Resilience — Block 1 (Cipher, 2026-05-03)

Slim equity-focused contracts for Prime's long-only swing system.
Mirrors the Alpha pattern (VolatilityData / TechnicalData) but scoped
to what Prime actually consumes: equity price, RSI, earnings gate.

Philosophy: same as Alpha — make the bad-data-reaches-scorer failure
class impossible by construction.
"""

from __future__ import annotations

import math
import sys
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.resilience.contracts import DataContractError, require_float, require_str


# ── Typed Data Objects ────────────────────────────────────────────────────────

@dataclass
class EquityData:
    """
    Price and momentum data for a Prime equity pick.

    All non-Optional fields are validated in __post_init__.
    Raises DataContractError on NaN, Inf, or out-of-range values.
    """
    ticker:          str
    last_price:      float          # Current price — must be > 0
    rsi_14:          float          # 14-period RSI — must be in [0, 100]
    price_vs_20d_ma: Optional[float] = None   # % deviation (informational)
    price_vs_50d_ma: Optional[float] = None
    volume_ratio:    Optional[float] = None   # today / 30d avg
    source:          str             = "polygon"
    fetched_at:      datetime        = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate all non-Optional numeric fields via require_float (NaN/Inf safe)."""
        # last_price: must be positive
        self.last_price = require_float(
            self.last_price, source="equity_data", field="last_price",
            min_val=0.01  # $0.01 minimum — catches zero and negative prices
        )
        # rsi_14: bounded [0, 100]
        self.rsi_14 = require_float(
            self.rsi_14, source="equity_data", field="rsi_14",
            min_val=0.0, max_val=100.0
        )
        # Validate optional floats if present
        if self.price_vs_20d_ma is not None:
            self.price_vs_20d_ma = require_float(
                self.price_vs_20d_ma, source="equity_data", field="price_vs_20d_ma"
            )
        if self.price_vs_50d_ma is not None:
            self.price_vs_50d_ma = require_float(
                self.price_vs_50d_ma, source="equity_data", field="price_vs_50d_ma"
            )
        if self.volume_ratio is not None:
            self.volume_ratio = require_float(
                self.volume_ratio, source="equity_data", field="volume_ratio",
                min_val=0.0
            )


@dataclass
class EarningsData:
    """
    Earnings calendar data for a Prime pick.

    days_to_earnings=None means unknown (ETF or no coverage) — callers
    must treat None conservatively (apply a staleness penalty or block).
    """
    ticker:              str
    days_to_earnings:    Optional[int]    # None = unknown
    earnings_date:       Optional[str]    # ISO date string, or None
    has_earnings_soon:   bool             = False   # True if earnings within 14 days

    def __post_init__(self) -> None:
        """Validate days_to_earnings if provided."""
        if self.days_to_earnings is not None:
            if not isinstance(self.days_to_earnings, int):
                raise DataContractError(
                    "earnings_data", "days_to_earnings",
                    "must be int or None", raw=self.days_to_earnings
                )
            if self.days_to_earnings < 0:
                raise DataContractError(
                    "earnings_data", "days_to_earnings",
                    "cannot be negative", raw=self.days_to_earnings
                )


def validate_equity_execute_request(ticker: str, price: float, rsi: float) -> EquityData:
    """
    Validate an incoming /execute request's numeric fields for Prime.

    Raises DataContractError on any invalid field — caller returns 422.
    This is the single validation boundary before Prime scores a pick.

    Args:
        ticker: Equity symbol.
        price:  Last price from the pick submission.
        rsi:    14-period RSI from the pick submission.

    Returns:
        Validated EquityData object.
    """
    return EquityData(
        ticker=ticker,
        last_price=price,
        rsi_14=rsi,
    )
