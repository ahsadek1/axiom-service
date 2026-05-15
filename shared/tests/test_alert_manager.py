"""
test_alert_manager.py — Unit tests for alert_manager.py
Run: python3 -m pytest shared/tests/test_alert_manager.py -v
"""

import os
import tempfile
import pytest

# Patch CHRONICLE_DB to a temp file before importing
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["CHRONICLE_DB"] = _tmp.name

import sys
sys.path.insert(0, "/Users/ahmedsadek/nexus")
from shared.alert_manager import (
    write_alert, get_open_alerts, is_known,
    ack_alert, resolve_alert, suppress_alert, get_alert, get_recent_resolved,
)


def teardown_module(_):
    os.unlink(_tmp.name)


# ---------------------------------------------------------------------------
# Test 1: write_alert creates new OPEN record
# ---------------------------------------------------------------------------
def test_write_alert_new():
    aid = write_alert("genesis", "WARNING", "omni", "omni:silence:test1",
                      "OMNI silent 31min", "No synthesis since 14:31")
    assert aid > 0
    alert = get_alert("omni:silence:test1")
    assert alert is not None
    assert alert["status"] == "OPEN"
    assert alert["severity"] == "WARNING"
    assert alert["service"] == "omni"
    assert alert["title"] == "OMNI silent 31min"


# ---------------------------------------------------------------------------
# Test 2: write_alert with duplicate key returns -1 (idempotent)
# ---------------------------------------------------------------------------
def test_write_alert_duplicate_returns_minus_one():
    write_alert("genesis", "WARNING", "omni", "omni:silence:test2", "OMNI silent", "")
    result = write_alert("genesis", "CRITICAL", "omni", "omni:silence:test2", "OMNI silent again", "")
    assert result == -1
    # Only one open alert should exist for this key
    open_alerts = get_open_alerts(service="omni")
    keys = [a["issue_key"] for a in open_alerts]
    assert keys.count("omni:silence:test2") == 1


# ---------------------------------------------------------------------------
# Test 3: resolve_alert marks RESOLVED
# ---------------------------------------------------------------------------
def test_resolve_alert():
    write_alert("genesis", "CRITICAL", "alpha-buffer", "ab:no-concordance:test3",
                "No concordance 31min", "Agents scoring below P2 threshold")
    ok = resolve_alert("ab:no-concordance:test3", "Score calibration reviewed — expected in late session")
    assert ok is True
    alert = get_alert("ab:no-concordance:test3")
    assert alert is None  # No longer OPEN/IN_PROGRESS


# ---------------------------------------------------------------------------
# Test 4: is_known returns True for OPEN issue
# ---------------------------------------------------------------------------
def test_is_known_open():
    write_alert("omni", "INFO", "omni", "omni:test4:key", "Test alert", "")
    assert is_known("omni:test4:key") is True


# ---------------------------------------------------------------------------
# Test 5: is_known returns False for RESOLVED issue
# ---------------------------------------------------------------------------
def test_is_known_resolved():
    write_alert("genesis", "WARNING", "alpha-buffer", "ab:test5:key", "Test", "")
    resolve_alert("ab:test5:key", "fixed")
    assert is_known("ab:test5:key") is False


# ---------------------------------------------------------------------------
# Test 6: Diagnostic cron skips known-open issue
# ---------------------------------------------------------------------------
def test_diagnostic_skips_known_open():
    write_alert("genesis", "WARNING", "omni", "omni:silence:test6", "Already known", "")
    # Simulate cron check — should not create duplicate
    actions_taken = 0
    if not is_known("omni:silence:test6"):
        actions_taken += 1
    assert actions_taken == 0  # Cron correctly skipped it


# ---------------------------------------------------------------------------
# Test 7: Diagnostic resolves when issue clears
# ---------------------------------------------------------------------------
def test_diagnostic_resolves_cleared_issue():
    write_alert("genesis", "WARNING", "omni", "omni:silence:test7", "OMNI silent", "")
    ack_alert("omni:silence:test7", "genesis")
    # Simulate OMNI resuming — cron should mark resolved
    resolve_alert("omni:silence:test7", "OMNI resumed — last_synthesis_min_ago=2")
    assert is_known("omni:silence:test7") is False


# ---------------------------------------------------------------------------
# Test 8: ack_alert marks IN_PROGRESS
# ---------------------------------------------------------------------------
def test_ack_alert():
    write_alert("genesis", "CRITICAL", "alpha-buffer", "ab:test8:key", "Test", "")
    ok = ack_alert("ab:test8:key", "genesis")
    assert ok is True
    alert = get_alert("ab:test8:key")
    assert alert["status"] == "IN_PROGRESS"
    assert alert["handled_by"] == "genesis"
    # is_known should still return True for IN_PROGRESS
    assert is_known("ab:test8:key") is True


# ---------------------------------------------------------------------------
# Test 9: get_open_alerts filters by service
# ---------------------------------------------------------------------------
def test_get_open_alerts_filter():
    write_alert("cipher", "WARNING", "omni", "omni:test9a", "OMNI issue", "")
    write_alert("cipher", "WARNING", "alpha-buffer", "ab:test9b", "Buffer issue", "")
    omni_alerts = get_open_alerts(service="omni")
    ab_alerts = get_open_alerts(service="alpha-buffer")
    assert any(a["issue_key"] == "omni:test9a" for a in omni_alerts)
    assert not any(a["issue_key"] == "omni:test9a" for a in ab_alerts)
    assert any(a["issue_key"] == "ab:test9b" for a in ab_alerts)


# ---------------------------------------------------------------------------
# Test 10: suppress_alert removes from is_known
# ---------------------------------------------------------------------------
def test_suppress_alert():
    write_alert("genesis", "INFO", "omni", "omni:test10:key", "EOD silence", "")
    ok = suppress_alert("omni:test10:key", "Market closed — expected silence")
    assert ok is True
    assert is_known("omni:test10:key") is False
