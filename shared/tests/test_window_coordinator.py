"""
test_window_coordinator.py — Tests for SW3/G7 window mismatch coordinator.

8 test cases per spec.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from window_coordinator import cycles_stale, evaluate_window, apply_sizing_adjustment


class TestCyclesStale:
    def test_same_window_is_zero(self):
        assert cycles_stale("2026-04-17-0915", "2026-04-17-0915") == 0

    def test_one_cycle_stale(self):
        assert cycles_stale("2026-04-17-0900", "2026-04-17-0915") == 1

    def test_two_cycles_stale(self):
        assert cycles_stale("2026-04-17-0845", "2026-04-17-0915") == 2

    def test_three_cycles_stale(self):
        assert cycles_stale("2026-04-17-0830", "2026-04-17-0915") == 3

    def test_empty_window_id_returns_999(self):
        assert cycles_stale("", "2026-04-17-0915") == 999

    def test_parse_failure_is_fresh(self):
        assert cycles_stale("invalid", "also-invalid") == 0


class TestEvaluateWindow:
    def test_fresh_same_window(self):
        """T1: Matching window_id → FRESH, sizing 1.0, EXECUTE."""
        r = evaluate_window("2026-04-17-0915", "2026-04-17-0915", "NVDA", "Cipher")
        assert r["status"] == "FRESH"
        assert r["sizing_adjustment"] == 1.0
        assert r["action"] == "EXECUTE"
        assert r["alert_sent"] is False

    def test_stale_1_one_cycle(self):
        """T2: 1 cycle old → STALE_1, sizing 0.75, EXECUTE_REDUCED."""
        r = evaluate_window("2026-04-17-0900", "2026-04-17-0915", "NVDA", "Cipher")
        assert r["status"] == "STALE_1"
        assert r["sizing_adjustment"] == 0.75
        assert r["action"] == "EXECUTE_REDUCED"
        assert r["alert_sent"] is False

    def test_stale_2_two_cycles(self):
        """T3: 2 cycles old → STALE_2, EXECUTE_REDUCED at 50% sizing, no alert.
        Cipher fix (2026-04-22): STALE_2 no longer deadlocks picks — executes
        at reduced sizing. HOLD threshold moved to STALE_3 (45 min).
        """
        r = evaluate_window("2026-04-17-0845", "2026-04-17-0915", "NVDA", "Cipher")
        assert r["status"] == "STALE_2"
        assert r["action"] == "EXECUTE_REDUCED"
        assert r["sizing_adjustment"] == 0.50
        assert r["alert_sent"] is False

    def test_stale_3_three_cycles(self):
        """T4: 3 cycles old → STALE_3, HOLD_FOR_REVIEW, alert sent.
        Cipher fix (2026-04-22): HOLD threshold moved from 30 min to 45 min.
        EXPIRED threshold moved from 45 min to 60 min (4+ cycles).
        """
        r = evaluate_window("2026-04-17-0830", "2026-04-17-0915", "NVDA", "Cipher")
        assert r["status"] == "STALE_3"
        assert r["action"] == "HOLD_FOR_REVIEW"
        assert r["sizing_adjustment"] == 0.50
        assert r["cycles_stale"] == 3
        assert r["alert_sent"] is True

    def test_expired_four_cycles(self):
        """T4b: 4 cycles old (60 min) → EXPIRED, REJECTED, sizing 0.
        Cipher fix (2026-04-22): EXPIRED threshold moved from 3 to 4+ cycles.
        """
        r = evaluate_window("2026-04-17-0815", "2026-04-17-0915", "NVDA", "Cipher")
        assert r["status"] == "EXPIRED"
        assert r["action"] == "REJECTED"
        assert r["sizing_adjustment"] == 0.0
        assert r["cycles_stale"] == 4

    def test_no_window_id_treated_as_expired(self):
        """T5: Missing pick window_id → EXPIRED."""
        r = evaluate_window("", "2026-04-17-0915", "AAPL", "Atlas")
        assert r["status"] == "EXPIRED"
        assert r["action"] == "REJECTED"

    def test_axiom_unreachable_treated_as_fresh(self):
        """T6: When current_window_id equals pick (fail-open), FRESH."""
        r = evaluate_window("2026-04-17-0915", "2026-04-17-0915", "AAPL", "Atlas")
        assert r["status"] == "FRESH"


class TestApplySizingAdjustment:
    def test_fresh_no_adjustment(self):
        """T7: FRESH → no change to sizing_mult."""
        result = {"action": "EXECUTE", "sizing_adjustment": 1.0}
        assert apply_sizing_adjustment(1.0, result) == 1.0

    def test_stale_1_reduces_sizing(self):
        """T7b: STALE_1 → 75% of original sizing."""
        result = {"action": "EXECUTE_REDUCED", "sizing_adjustment": 0.75}
        assert apply_sizing_adjustment(1.0, result) == 0.75

    def test_rejected_returns_zero(self):
        """T8: REJECTED → 0.0 regardless of input."""
        result = {"action": "REJECTED", "sizing_adjustment": 0.0}
        assert apply_sizing_adjustment(1.0, result) == 0.0

    def test_axiom_1_5_sizing_with_stale_1(self):
        """Axiom sends sizing_mult=1.5, STALE_1 → 1.5 * 0.75 = 1.125."""
        result = {"action": "EXECUTE_REDUCED", "sizing_adjustment": 0.75}
        assert apply_sizing_adjustment(1.5, result) == 1.125
