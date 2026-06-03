"""
test_regime.py — Unit tests for VIX regime classification.

Tests all 6 regime classifications and their allowed flags.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from regime import classify_regime, regime_allows_any_trading, Regime


class TestRegimeClassification:
    """Test all 6 VIX regime classifications."""

    def test_low_vol_regime(self):
        regime = classify_regime(10.5)
        assert regime.classification == "LOW_VOL"
        assert regime.alpha_credit_allowed is True
        assert regime.alpha_debit_allowed is True
        assert regime.prime_allowed is True
        assert regime.alpha_size_mult == 0.75

    def test_normal_regime(self):
        regime = classify_regime(18.2)
        assert regime.classification == "NORMAL"
        assert regime.alpha_credit_allowed is True
        assert regime.alpha_debit_allowed is True
        assert regime.prime_allowed is True
        assert regime.alpha_size_mult == 1.0
        assert regime.alpha_credit_daily_cap is None
        assert regime.prime_daily_cap is None

    def test_elevated_regime(self):
        regime = classify_regime(22.5)
        assert regime.classification == "ELEVATED"
        assert regime.alpha_credit_allowed is True
        assert regime.alpha_debit_allowed is True
        assert regime.prime_allowed is True

    def test_stress_regime(self):
        regime = classify_regime(27.0)
        assert regime.classification == "STRESS"
        assert regime.alpha_credit_allowed is True
        assert regime.alpha_debit_allowed is False      # PAUSED
        assert regime.prime_allowed is True
        assert regime.prime_daily_cap == 3

    def test_high_stress_regime(self):
        regime = classify_regime(32.0)
        assert regime.classification == "HIGH_STRESS"
        assert regime.alpha_credit_allowed is True
        assert regime.alpha_debit_allowed is False      # HALTED
        assert regime.prime_allowed is False            # PAUSED
        assert regime.alpha_credit_daily_cap == 2

    def test_crisis_regime(self):
        regime = classify_regime(37.5)
        assert regime.classification == "CRISIS"
        assert regime.alpha_credit_allowed is False     # HALTED
        assert regime.alpha_debit_allowed is False      # HALTED
        assert regime.prime_allowed is False            # HALTED
        assert regime.alpha_size_mult == 0.0

    def test_vix_boundary_exactly_12(self):
        """VIX exactly 12 should be NORMAL not LOW_VOL."""
        regime = classify_regime(12.0)
        assert regime.classification == "NORMAL"

    def test_vix_boundary_exactly_35(self):
        """VIX exactly 35 should be CRISIS."""
        regime = classify_regime(35.0)
        assert regime.classification == "CRISIS"

    def test_estimated_vix_flag(self):
        """is_estimated flag should be set correctly."""
        regime_real = classify_regime(18.0, is_estimated=False)
        regime_est  = classify_regime(18.0, is_estimated=True)
        assert regime_real.is_estimated is False
        assert regime_est.is_estimated is True

    def test_regime_to_dict(self):
        """to_dict should include all required fields."""
        regime = classify_regime(18.0)
        d      = regime.to_dict()
        required_keys = [
            "vix", "classification", "strategy_bias",
            "alpha_credit_allowed", "alpha_debit_allowed",
            "prime_allowed", "alpha_size_mult", "is_estimated",
        ]
        for key in required_keys:
            assert key in d, f"Missing key: {key}"

    def test_immutability(self):
        """Regime should be immutable (frozen dataclass)."""
        regime = classify_regime(18.0)
        with pytest.raises(Exception):
            regime.classification = "HACKED"


class TestRegimeAllowsTrading:
    """Test regime_allows_any_trading helper."""

    def test_normal_allows_trading(self):
        assert regime_allows_any_trading(classify_regime(18.0)) is True

    def test_crisis_blocks_trading(self):
        assert regime_allows_any_trading(classify_regime(37.0)) is False

    def test_high_stress_allows_alpha_credit(self):
        """HIGH_STRESS allows Alpha credit — so trading is allowed."""
        assert regime_allows_any_trading(classify_regime(32.0)) is True
