"""Tests for pre_market_snapshot.py"""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from pre_market_snapshot import (
    ServiceSnapshot, TradingDaySnapshot, _hash, _sign_snapshot,
    classify_drift_action, _get_plist_mtimes, SAFE_AUTO_CORRECT,
)


def _make_snapshot(**kwargs) -> TradingDaySnapshot:
    defaults = dict(
        snapshot_date="2026-04-14",
        captured_at="2026-04-14T06:00:00-04:00",
        services={},
        agent_weights={"Cipher": 0.45, "Atlas": 0.30, "Sage": 0.25},
        plist_mtimes={},
        global_invariants={"paper_mode": "true"},
        signature="",
        valid=False,
    )
    defaults.update(kwargs)
    return TradingDaySnapshot(**defaults)


def test_hash_deterministic():
    assert _hash("hello") == _hash("hello")
    assert _hash("hello") != _hash("world")


def test_sign_snapshot_deterministic():
    snap = _make_snapshot()
    sig1 = _sign_snapshot(snap)
    sig2 = _sign_snapshot(snap)
    assert sig1 == sig2
    assert len(sig1) == 64  # SHA256 hex


def test_sign_snapshot_changes_on_mutation():
    snap1 = _make_snapshot()
    snap2 = _make_snapshot(snapshot_date="2026-04-15")
    assert _sign_snapshot(snap1) != _sign_snapshot(snap2)


def test_service_snapshot_dataclass():
    ss = ServiceSnapshot(
        service_name="cipher",
        version="1.0.0",
        status="healthy",
        health_data={"port": 9001},
        captured_at="2026-04-14T06:00:00",
    )
    assert ss.service_name == "cipher"
    assert ss.status == "healthy"


def test_classify_drift_service_down():
    action = classify_drift_action("SERVICE_DOWN:cipher")
    assert action == "service_restart"
    assert action in SAFE_AUTO_CORRECT


def test_classify_drift_plist_modified():
    action = classify_drift_action("PLIST_MODIFIED:ai.nexus.cipher.plist")
    assert action.startswith("REQUIRES_AHMED")


def test_classify_drift_agent_weight():
    action = classify_drift_action("AGENT_WEIGHT_CHANGED:Cipher")
    assert action.startswith("REQUIRES_AHMED")


def test_classify_drift_unknown():
    action = classify_drift_action("UNKNOWN_DRIFT:something")
    assert action.startswith("REQUIRES_AHMED")


def test_get_plist_mtimes_returns_dict():
    result = _get_plist_mtimes()
    assert isinstance(result, dict)
    # All values should be floats (mtimes)
    for k, v in result.items():
        assert isinstance(v, float)
