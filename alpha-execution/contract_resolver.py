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

    # GAP-5: Reject zero-width spreads — rounding can collapse both legs to the same strike
    if short_strike == long_strike:
        raise ValueError(
            f"zero-width spread rejected: short={short_strike} long={long_strike} "
            f"for {ticker} {direction} at price={current_price:.2f} "
            f"(SPREAD_WIDTH_PCT={SPREAD_WIDTH_PCT}, WING_WIDTH_PCT={WING_WIDTH_PCT})"
        )

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


def resolve_available_contract(
    ticker:        str,
    direction:     str,
    current_price: float,
    alpaca_client,           # alpaca_client.AlpacaClient instance
) -> "SpreadParams":
    """
    Alpaca-aware spread resolver.

    Resolves spread parameters, then validates contracts actually exist on Alpaca.
    If the calculated expiry has no listed contracts, walks forward month-by-month
    until a valid expiry is found. Snaps strikes to nearest available.

    If the standard monthly third-Friday loop finds nothing (e.g. thinly-traded
    names that only have weekly listings), falls back to querying ALL available
    contracts for the ticker and selecting the nearest expiry >= target DTE.

    Args:
        ticker:         Underlying symbol.
        direction:      'bullish' or 'bearish'.
        current_price:  Current underlying price.
        alpaca_client:  Live AlpacaClient for contract lookup.

    Returns:
        SpreadParams with verified, live Alpaca contract symbols.

    Raises:
        ValueError: If no valid contracts found.
    """
    from datetime import date, timedelta

    option_type = "put" if direction == "bullish" else "call"
    base = resolve_spread(ticker, direction, current_price)

    # ── Step 1: Find nearest available expiry (third-Friday monthly loop) ────
    expiry = None
    candidate = date.fromisoformat(base.expiration_date)
    for _ in range(3):   # try up to 3 consecutive monthly expirations
        contracts = alpaca_client.get_option_contracts(
            underlying      = ticker,
            expiration_date = candidate.isoformat(),
            option_type     = option_type,
        )
        if contracts:
            expiry = candidate
            break
        # Advance one calendar month and recalculate third Friday
        m = candidate.month + 1 if candidate.month < 12 else 1
        y = candidate.year if candidate.month < 12 else candidate.year + 1
        candidate = _third_friday(y, m)

    if expiry is None:
        # ── Fallback: query ALL available contracts (no expiry filter) ────────
        # Handles thinly-traded names that only have weekly/near-term listings
        # and miss the standard monthly third Fridays.
        # Fix deployed 2026-04-28 by OMNI — root cause: KMI/COF/EPD only list
        # weekly expirations; 3-month loop found nothing → SPREAD_RESOLUTION_FAILED.
        target_date  = date.fromisoformat(base.expiration_date)
        min_dte_date = date.today() + timedelta(days=21)  # never < 21 DTE
        all_contracts = alpaca_client.get_option_contracts(
            underlying  = ticker,
            option_type = option_type,
            limit       = 100,
        )
        available_expiries = sorted(set(
            date.fromisoformat(c["expiration_date"])
            for c in all_contracts
            if c.get("expiration_date")
        ))
        # Pick nearest expiry >= target DTE and >= 21 DTE
        eligible = [e for e in available_expiries if e >= target_date and e >= min_dte_date]
        if not eligible:
            eligible = [e for e in available_expiries if e >= min_dte_date]
        if eligible:
            expiry = eligible[0]
            logger.warning(
                "contract_resolver: %s %s — no standard monthly expiry found; "
                "using nearest available: %s (fallback path)",
                ticker, option_type, expiry
            )
        else:
            raise ValueError(
                f"No listed {option_type} contracts found for {ticker} within 3 months of target DTE"
            )

    # Re-resolve with confirmed expiry
    spread = resolve_spread(ticker, direction, current_price, target_date=expiry)

    # ── Step 2: Snap strikes to nearest available ─────────────────────────────
    contracts = alpaca_client.get_option_contracts(
        underlying      = ticker,
        expiration_date = expiry.isoformat(),
        option_type     = option_type,
    )
    if not contracts:
        raise ValueError(f"No {option_type} contracts for {ticker} on {expiry}")

    available_strikes = sorted(
        float(c.get("strike_price", 0))
        for c in contracts
        if float(c.get("strike_price", 0)) > 0
    )

    if not available_strikes:
        raise ValueError(
            f"No valid strikes in options chain for {ticker} {option_type} on {expiry} "
            f"— chain returned {len(contracts)} contract(s) with no usable strike prices"
        )

    def snap(target: float) -> float:
        return min(available_strikes, key=lambda s: abs(s - target))

    snapped_short = snap(spread.short_strike)
    snapped_long  = snap(spread.long_strike)

    # ── Duplicate-leg guard ───────────────────────────────────────────────────
    if snapped_short == snapped_long or abs(snapped_short - snapped_long) < 1.0:
        if len(available_strikes) < 2:
            raise ValueError(
                f"Only one strike available for {ticker} {option_type} on {expiry} — "
                f"cannot construct a spread with two distinct legs"
            )
        idx = available_strikes.index(snapped_short)
        if direction == "bullish":
            if idx > 0:
                snapped_long = available_strikes[idx - 1]
            else:
                snapped_short = available_strikes[idx + 1]
                snapped_long  = available_strikes[idx]
        else:
            if idx < len(available_strikes) - 1:
                snapped_long = available_strikes[idx + 1]
            else:
                snapped_short = available_strikes[idx - 1]
                snapped_long  = available_strikes[idx]

    if snapped_short == snapped_long:
        raise ValueError(
            f"Contract resolver could not produce distinct legs for {ticker} "
            f"{direction} {option_type} on {expiry} — "
            f"short={snapped_short} long={snapped_long}"
        )

    return SpreadParams(
        underlying      = spread.underlying,
        direction       = spread.direction,
        option_type     = spread.option_type,
        short_strike    = snapped_short,
        long_strike     = snapped_long,
        expiration_date = expiry.isoformat(),
        target_dte      = (expiry - date.today()).days,
        current_price   = current_price,
        is_etf          = spread.is_etf,
    )
