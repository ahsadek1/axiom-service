"""Tests for graceful_degradation.py"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from graceful_degradation import (
    GracefulDegradationManager, DegradationLevel,
    NON_NEGOTIABLES, LEVEL_SIZE_MULTIPLIER, LEVEL_NEW_ENTRIES_ALLOWED,
)


def test_initial_level():
    mgr = GracefulDegradationManager()
    assert mgr.current_level == DegradationLevel.FULL


def test_qi_one_brain_timeout_is_minor():
    mgr = GracefulDegradationManager()
    mgr.on_event("QI_ONE_BRAIN_TIMEOUT")
    assert mgr.current_level == DegradationLevel.MINOR
    assert mgr.get_size_multiplier() == 1.0
    assert mgr.is_new_entry_allowed() is True


def test_qi_two_brains_timeout_is_moderate():
    mgr = GracefulDegradationManager()
    mgr.on_event("QI_TWO_BRAINS_TIMEOUT")
    assert mgr.current_level == DegradationLevel.MODERATE
    # Spec: size_multiplier_delta=-0.75 → 1.0 - 0.75 = 0.25
    assert mgr.get_size_multiplier() == 0.25
    assert mgr.is_new_entry_allowed() is True


def test_qi_all_brains_timeout_blocks_new_entries():
    mgr = GracefulDegradationManager()
    mgr.on_event("QI_ALL_BRAINS_TIMEOUT")
    assert mgr.current_level == DegradationLevel.SEVERE
    assert mgr.is_new_entry_allowed() is False


def test_vix_brake_full_is_emergency():
    mgr = GracefulDegradationManager()
    mgr.on_event("VIX_BRAKE_FULL")
    assert mgr.current_level == DegradationLevel.EMERGENCY
    assert mgr.get_size_multiplier() == 0.0
    assert mgr.is_new_entry_allowed() is False


def test_level_takes_maximum_of_conditions():
    mgr = GracefulDegradationManager()
    mgr.on_event("QI_ONE_BRAIN_TIMEOUT")       # MINOR
    mgr.on_event("CAPITAL_ROUTER_UNREACHABLE")  # MODERATE
    assert mgr.current_level == DegradationLevel.MODERATE


def test_clear_condition_reduces_level():
    mgr = GracefulDegradationManager()
    mgr.on_event("QI_TWO_BRAINS_TIMEOUT")       # MODERATE
    mgr.on_event("QI_ONE_BRAIN_TIMEOUT")         # MINOR (already MODERATE)
    mgr.clear_degradation("QI_TWO_BRAINS_TIMEOUT")
    assert mgr.current_level == DegradationLevel.MINOR


def test_non_negotiables_are_never_degraded():
    for item in NON_NEGOTIABLES:
        mgr = GracefulDegradationManager()
        mgr.on_event("VIX_BRAKE_FULL")   # Emergency level
        # Non-negotiables should still be recognized
        assert mgr.is_non_negotiable(item) is True


def test_non_negotiable_false_for_normal_action():
    mgr = GracefulDegradationManager()
    assert mgr.is_non_negotiable("open_new_position") is False


def test_eod_report_structure():
    mgr = GracefulDegradationManager()
    mgr.on_event("QI_ONE_BRAIN_TIMEOUT")
    report = mgr.get_eod_degradation_report()
    assert "final_level" in report
    assert "total_degradation_events" in report
    assert "events" in report


def test_status_summary():
    mgr = GracefulDegradationManager()
    status = mgr.get_status_summary()
    assert status["level"] == 0
    assert status["level_name"] == "FULL"
    assert status["size_multiplier"] == 1.0
    assert status["new_entries_allowed"] is True
