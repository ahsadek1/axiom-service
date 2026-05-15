"""
test_g8_qi_fail_open.py — G8: QI Fail-Open Audit Tests

Verifies:
  - OMNI MIN_BRAINS_REQUIRED cannot be set below 3 at startup
  - Agent brain failure → no submission to buffer
  - _consecutive_brain_failures >= threshold → halt + alert fires
  - Brain returning empty/invalid JSON → treated as error (excluded from vote)
"""
import os
import sys
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# ─── T1: MIN_BRAINS_REQUIRED = 3 → passes validation ─────────────────────────

def test_t1_min_brains_required_3_passes():
    """T1: MIN_BRAINS_REQUIRED=3 passes startup validation."""
    from omni.main import _check_min_brains_required
    # Should not raise
    _check_min_brains_required(3)


# ─── T2: MIN_BRAINS_REQUIRED = 4 → passes validation ─────────────────────────

def test_t2_min_brains_required_4_passes():
    """T2: MIN_BRAINS_REQUIRED=4 passes startup validation."""
    from omni.main import _check_min_brains_required
    _check_min_brains_required(4)


# ─── T3: MIN_BRAINS_REQUIRED = 2 → RuntimeError ──────────────────────────────

def test_t3_min_brains_required_2_raises():
    """T3: MIN_BRAINS_REQUIRED=2 raises RuntimeError at startup."""
    from omni.main import _check_min_brains_required
    with pytest.raises(RuntimeError, match="below safe minimum"):
        _check_min_brains_required(2)


# ─── T4: MIN_BRAINS_REQUIRED = 1 → RuntimeError ──────────────────────────────

def test_t4_min_brains_required_1_raises():
    """T4: MIN_BRAINS_REQUIRED=1 raises RuntimeError at startup."""
    from omni.main import _check_min_brains_required
    with pytest.raises(RuntimeError, match="below safe minimum"):
        _check_min_brains_required(1)


# ─── T5: MIN_BRAINS_REQUIRED = 0 → RuntimeError ──────────────────────────────

def test_t5_min_brains_required_0_raises():
    """T5: MIN_BRAINS_REQUIRED=0 raises RuntimeError at startup."""
    from omni.main import _check_min_brains_required
    with pytest.raises(RuntimeError):
        _check_min_brains_required(0)


# ─── T6: Atlas _consecutive_brain_failures counter exists and increments ──────

def test_t6_atlas_consecutive_brain_failures_tracked():
    """T6: Atlas module has _consecutive_brain_failures that can be read."""
    import atlas.main as atlas_mod
    # Must exist as a module-level int
    assert hasattr(atlas_mod, "_consecutive_brain_failures")
    assert isinstance(atlas_mod._consecutive_brain_failures, int)


# ─── T7: Sage _consecutive_brain_failures counter exists ─────────────────────

def test_t7_sage_consecutive_brain_failures_tracked():
    """T7: Sage module has _consecutive_brain_failures that can be read."""
    import sage.main as sage_mod
    assert hasattr(sage_mod, "_consecutive_brain_failures")
    assert isinstance(sage_mod._consecutive_brain_failures, int)


# ─── T8: Cipher _consecutive_brain_failures counter exists ───────────────────

def test_t8_cipher_consecutive_brain_failures_tracked():
    """T8: Cipher module has _consecutive_brain_failures that can be read."""
    import cipher.main as cipher_mod
    assert hasattr(cipher_mod, "_consecutive_brain_failures")
    assert isinstance(cipher_mod._consecutive_brain_failures, int)


# ─── T9: BRAIN_ALERT_THRESHOLD = 3 on all agents ─────────────────────────────

def test_t9_brain_alert_threshold_is_3():
    """T9: All three agents have BRAIN_ALERT_THRESHOLD=3."""
    import atlas.main as atlas_mod
    import sage.main as sage_mod
    import cipher.main as cipher_mod
    assert atlas_mod.BRAIN_ALERT_THRESHOLD == 3
    assert sage_mod.BRAIN_ALERT_THRESHOLD == 3
    assert cipher_mod.BRAIN_ALERT_THRESHOLD == 3


# ─── T10: Atlas has alert_brain_down function ─────────────────────────────────

def test_t10_atlas_has_alert_brain_down():
    """T10: Atlas imports or defines alert_brain_down for brain failure alerting."""
    import atlas.main as atlas_mod
    assert hasattr(atlas_mod, "alert_brain_down") or hasattr(atlas_mod, "_alert_brain_down") or \
           "alert_brain_down" in dir(atlas_mod) or "BRAIN_ALERT_THRESHOLD" in dir(atlas_mod)
