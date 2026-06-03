"""
Tests for shared/thesis_client.py

Covers: posture adjustment mapping, sizing cap mapping, sector detection,
macro gate check, cache behaviour, and HTTP failure fallback.
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch
import pytest

from thesis_client import (
    get_thesis_context,
    posture_to_score_adjustment,
    posture_to_sizing_cap,
    is_sector_favored,
    macro_gate_allows_trading,
    _SAFE_DEFAULT,
)

# ── helpers ──────────────────────────────────────────────────────────────────

def _ctx(posture="NEUTRAL", macro_gate="PASS", favored=None, avoided=None, fallback=False):
    return {
        "trading_posture": posture,
        "macro_gate": macro_gate,
        "sizing_multiplier": 1.0,
        "favored_sectors": favored or [],
        "avoid_sectors": avoided or [],
        "is_fallback": fallback,
    }

# ── posture_to_score_adjustment ───────────────────────────────────────────────

def test_aggressive_gives_positive_adj():
    assert posture_to_score_adjustment(_ctx("AGGRESSIVE")) == 5.0

def test_neutral_gives_zero():
    assert posture_to_score_adjustment(_ctx("NEUTRAL")) == 0.0

def test_selective_gives_negative():
    assert posture_to_score_adjustment(_ctx("SELECTIVE")) == -5.0

def test_defensive_gives_large_negative():
    assert posture_to_score_adjustment(_ctx("DEFENSIVE")) == -15.0

def test_unknown_posture_gives_zero():
    assert posture_to_score_adjustment(_ctx("UNKNOWN_XYZ")) == 0.0

# ── posture_to_sizing_cap ────────────────────────────────────────────────────

def test_aggressive_no_cap():
    assert posture_to_sizing_cap(_ctx("AGGRESSIVE")) == 1.0

def test_selective_cap_75():
    assert posture_to_sizing_cap(_ctx("SELECTIVE")) == 0.75

def test_defensive_cap_50():
    assert posture_to_sizing_cap(_ctx("DEFENSIVE")) == 0.50

# ── macro_gate_allows_trading ────────────────────────────────────────────────

def test_macro_gate_pass_allows():
    assert macro_gate_allows_trading(_ctx(macro_gate="PASS")) is True

def test_macro_gate_fail_blocks():
    assert macro_gate_allows_trading(_ctx(macro_gate="FAIL", fallback=False)) is False

def test_fallback_context_always_allows():
    assert macro_gate_allows_trading(_ctx(macro_gate="FAIL", fallback=True)) is True

# ── is_sector_favored ────────────────────────────────────────────────────────

def test_favored_sector_returns_true():
    ctx = _ctx(favored=["Technology", "Energy"])
    assert is_sector_favored("Technology", ctx) is True

def test_avoided_sector_returns_false():
    ctx = _ctx(avoided=["Real Estate"])
    assert is_sector_favored("Real Estate", ctx) is False

def test_neutral_sector_returns_none():
    ctx = _ctx(favored=["Technology"])
    assert is_sector_favored("Healthcare", ctx) is None

def test_none_ticker_sector_returns_none():
    assert is_sector_favored(None, _ctx()) is None

# ── get_thesis_context fallback on HTTP failure ───────────────────────────────

def test_http_failure_returns_safe_default():
    """Connection error → safe NEUTRAL default, no exception."""
    import thesis_client
    thesis_client._cached_context = None
    thesis_client._cached_at = 0.0

    with patch("thesis_client.requests.get", side_effect=Exception("connection refused")):
        result = get_thesis_context("http://fake:9999")

    assert result["trading_posture"] == "NEUTRAL"
    assert result["is_fallback"] is True

def test_http_200_caches_result():
    """Valid 200 response is cached for subsequent calls."""
    import thesis_client
    thesis_client._cached_context = None
    thesis_client._cached_at = 0.0

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _ctx("AGGRESSIVE", fallback=False)

    with patch("thesis_client.requests.get", return_value=mock_resp) as mock_get:
        r1 = get_thesis_context("http://fake:8060")
        r2 = get_thesis_context("http://fake:8060")  # should use cache

    assert r1["trading_posture"] == "AGGRESSIVE"
    assert mock_get.call_count == 1  # only one real HTTP call
