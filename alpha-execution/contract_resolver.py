"""
contract_resolver.py — Alpha Options Contract Resolver

Resolves a trade signal into specific options contract parameters.
Handles strike rounding, expiry selection, and spread construction.

Defaults:
  - Target DTE: 40 days (nearest Friday on or after)
  - Bullish: bull put credit spread (sell ATM-5% put, buy ATM-10% put)
  - Bearish: bear call credit spread (sell ATM+5% call, buy ATM+10% call)
  - ETF strikes rounded to $5; stock strikes to $1
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from config import ETF_TICKERS, SPREAD_WIDTH_PCT, WING_WIDTH_PCT, TARGET_DTE

logger = logging.getLogger("alpha_exec.contract")


@dataclass
class SpreadParams:
    """Resolved options spread parameters ready for order placement."""

    underlying:       str
    direction:        str          # 'bullish' or 'bearish'
    option_type:      str          # 'put' (bullish) or 'call' (bearish)
    short_strike:     float        # the strike we sell
    long_strike:      float        # the strike we buy (protection)
    expiration_date:  str          # 'YYYY-MM-DD'
    target_dte:       int
    current_price:    float
    is_etf:           bool

    def leg_description(self) -> str:
        """Human-readable spread description for logging/Telegram."""
        spread_type = "bull put" if self.direction == "bullish" else "bear call"
        return (
            f"{self.underlying} {spread_type} spread "
            f"{self.short_strike}/{self.long_strike} {self.option_type.upper()} "
            f"exp {self.expiration_date} (~{self.target_dte} DTE)"
        )


def resolve_spread(
    ticker:        str,
    direction:     str,
    current_price: float,
    target_date:   Optional[date] = None,
) -> SpreadParams:
    """
    Resolve trade signal into spread parameters.

    Args:
        ticker:        Stock or ETF symbol (uppercase).
        direction:     'bullish' or 'bearish'.
        current_price: Current underlying price.
        target_date:   Override expiration date (for testing). Default = today + TARGET_DTE.

    Returns:
        SpreadParams with all required parameters for order placement.

    Raises:
        ValueError: If direction is invalid or price is zero.
    """
    if current_price <= 0:
        raise ValueError(f"Invalid price for {ticker}: {current_price}")
    if direction not in ("bullish", "bearish"):
        raise ValueError(f"Invalid direction: {direction}")

    is_etf       = ticker.upper() in ETF_TICKERS
    expiry_date  = target_date or _target_expiry(date.today())
    actual_dte   = (expiry_date - date.today()).days

    if direction == "bullish":
        # Bull put credit spread: sell put at ATM-5%, buy put at ATM-10%
        short_strike = _round_strike(current_price * (1 - SPREAD_WIDTH_PCT), is_etf)
        long_strike  = _round_strike(current_price * (1 - SPREAD_WIDTH_PCT - WING_WIDTH_PCT), is_etf)
        option_type  = "put"
    else:
        # Bear call credit spread: sell call at ATM+5%, buy call at ATM+10%
        short_strike = _round_strike(current_price * (1 + SPREAD_WIDTH_PCT), is_etf)
        long_strike  = _round_strike(current_price * (1 + SPREAD_WIDTH_PCT + WING_WIDTH_PCT), is_etf)
        option_type  = "call"

    return SpreadParams(
        underlying      = ticker.upper(),
        direction       = direction,
        option_type     = option_type,
        short_strike    = short_strike,
        long_strike     = long_strike,
        expiration_date = expiry_date.isoformat(),
        target_dte      = actual_dte,
        current_price   = current_price,
        is_etf          = is_etf,
    )


def _target_expiry(today: date) -> date:
    """
    Find the standard monthly options expiration (third Friday) at or after today + TARGET_DTE.

    Adversarial fix #2: previous logic bumped to "nearest Friday", which produces
    non-existent expiry dates. Standard US equity options expire on the THIRD FRIDAY
    of the month. We pick the third Friday of the month that is TARGET_DTE+ days out;
    if that third Friday has already passed for that month, we advance to the next month.

    Args:
        today: Starting date (usually today).

    Returns:
        Expiration date (a valid third-Friday monthly expiry) approximately TARGET_DTE days out.
    """
    target = today + timedelta(days=TARGET_DTE)

    # Find the third Friday of the target month
    expiry = _third_friday(target.year, target.month)

    # If that third Friday is still before today+TARGET_DTE, advance one month
    if expiry < target:
        # Advance to next month
        if target.month == 12:
            expiry = _third_friday(target.year + 1, 1)
        else:
            expiry = _third_friday(target.year, target.month + 1)

    return expiry


def _third_friday(year: int, month: int) -> date:
    """
    Return the third Friday of the given month/year.

    Args:
        year:  Four-digit year.
        month: Month (1-12).

    Returns:
        Date of the third Friday in that month.
    """
    # Find first Friday of the month
    first_day = date(year, month, 1)
    # weekday(): Monday=0, Friday=4
    days_to_friday = (4 - first_day.weekday()) % 7
    first_friday = first_day + timedelta(days=days_to_friday)
    # Third Friday = first Friday + 14 days
    return first_friday + timedelta(days=14)


def _round_strike(price: float, is_etf: bool) -> float:
    """
    Round a strike price to the nearest valid increment.

    ETFs: round to nearest $5. Stocks: round to nearest $1.

    Args:
        price:  Raw target strike price.
        is_etf: True if the underlying is an ETF.

    Returns:
        Rounded strike price.
    """
    increment = 5.0 if is_etf else 1.0
    return round(round(price / increment) * increment, 2)


def calculate_dte(expiration_date_str: str) -> int:
    """
    Calculate days to expiration from an expiration date string.

    Args:
        expiration_date_str: Expiration date in 'YYYY-MM-DD' format.

    Returns:
        Days remaining until expiration (0 if expired).
    """
    expiry = date.fromisoformat(expiration_date_str)
    dte    = (expiry - date.today()).days
    return max(0, dte)
