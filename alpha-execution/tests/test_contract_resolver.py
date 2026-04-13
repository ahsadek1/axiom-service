"""
test_contract_resolver.py — Options contract resolver unit tests.

Tests strike calculation, DTE resolution, ETF rounding, and stock rounding.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date, timedelta
import pytest
from contract_resolver import resolve_spread, _round_strike, _target_expiry, calculate_dte


class TestStrikeRounding:
    def test_etf_rounds_to_5(self):
        assert _round_strike(447.3, is_etf=True)  == 445.0
        assert _round_strike(452.9, is_etf=True)  == 455.0
        assert _round_strike(500.0, is_etf=True)  == 500.0

    def test_stock_rounds_to_1(self):
        assert _round_strike(897.4, is_etf=False) == 897.0
        assert _round_strike(897.6, is_etf=False) == 898.0
        assert _round_strike(900.0, is_etf=False) == 900.0

    def test_etf_midpoint_rounds_up(self):
        assert _round_strike(447.5, is_etf=True) == 450.0


class TestTargetExpiry:
    def test_expiry_is_friday(self):
        today  = date(2026, 4, 10)   # Friday
        expiry = _target_expiry(today)
        assert expiry.weekday() == 4   # 4 = Friday

    def test_expiry_at_least_40_days_out(self):
        today  = date(2026, 4, 10)
        expiry = _target_expiry(today)
        assert (expiry - today).days >= 40

    def test_expiry_advances_to_friday_if_target_is_midweek(self):
        today  = date(2026, 4, 13)  # Monday — target ~May 23 (Saturday) → May 29 (Friday)
        expiry = _target_expiry(today)
        assert expiry.weekday() == 4


class TestResolveSpread:
    def test_bullish_spy_put_spread(self):
        spread = resolve_spread("SPY", "bullish", 500.0)
        assert spread.option_type    == "put"
        assert spread.direction      == "bullish"
        assert spread.is_etf         is True
        # Short strike = ATM - 5% = 500 * 0.95 = 475 → rounded to $5 = 475
        assert spread.short_strike   == 475.0
        # Long strike = ATM - 10% = 500 * 0.90 = 450 → rounded to $5 = 450
        assert spread.long_strike    == 450.0
        assert spread.short_strike > spread.long_strike   # credit spread: sell higher, buy lower

    def test_bearish_qqq_call_spread(self):
        spread = resolve_spread("QQQ", "bearish", 400.0)
        assert spread.option_type    == "call"
        assert spread.direction      == "bearish"
        assert spread.is_etf         is True
        # Short = ATM + 5% = 420 → rounded to $5 = 420
        assert spread.short_strike   == 420.0
        # Long  = ATM + 10% = 440 → rounded to $5 = 440
        assert spread.long_strike    == 440.0
        assert spread.long_strike > spread.short_strike   # bear call: buy further OTM

    def test_stock_rounds_to_dollar(self):
        spread = resolve_spread("NVDA", "bullish", 900.0)
        assert spread.is_etf         is False
        # Short = 900 * 0.95 = 855 → rounded to $1 = 855
        assert spread.short_strike   == 855.0
        assert spread.long_strike    == 810.0

    def test_non_etf_stock_identified(self):
        spread = resolve_spread("AAPL", "bullish", 200.0)
        assert spread.is_etf is False

    def test_etf_identified(self):
        spread = resolve_spread("SPY", "bullish", 500.0)
        assert spread.is_etf is True

    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError):
            resolve_spread("SPY", "long", 500.0)

    def test_zero_price_raises(self):
        with pytest.raises(ValueError):
            resolve_spread("SPY", "bullish", 0.0)

    def test_target_date_override(self):
        fixed_date = date(2026, 6, 19)  # specific Friday
        spread = resolve_spread("SPY", "bullish", 500.0, target_date=fixed_date)
        assert spread.expiration_date == "2026-06-19"

    def test_leg_description_contains_ticker(self):
        spread = resolve_spread("NVDA", "bullish", 900.0)
        assert "NVDA" in spread.leg_description()
        assert "put" in spread.leg_description()


class TestCalculateDte:
    def test_future_date_positive(self):
        future = (date.today() + timedelta(days=40)).isoformat()
        dte    = calculate_dte(future)
        assert dte == 40

    def test_today_is_zero(self):
        today = date.today().isoformat()
        assert calculate_dte(today) == 0

    def test_past_date_is_zero(self):
        past = (date.today() - timedelta(days=5)).isoformat()
        assert calculate_dte(past) == 0
