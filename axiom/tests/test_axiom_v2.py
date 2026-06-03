"""
tests/test_axiom_v2.py — Axiom v4 Tests (V1–V12)
Multi-factor regime model, ORACLE integration, pool enrichment.
All external calls mocked — no real network traffic.
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# ── Env setup — conftest.py runs first; setdefault here is a safe no-op ───────
_TEST_DB = tempfile.mktemp(suffix="_axiom_v2_test.db")
os.environ.setdefault("AXIOM_SECRET",        "test-secret-12345")
os.environ.setdefault("POLYGON_API_KEY",     "test-polygon")
os.environ.setdefault("ALPHA_VANTAGE_KEY",   "test-av")
os.environ.setdefault("FRED_API_KEY",        "test-fred")
os.environ.setdefault("TELEGRAM_BOT_TOKEN",  "9999:TEST")
os.environ.setdefault("AHMED_CHAT_ID",       "8573754783")
os.environ.setdefault("CIPHER_WEBHOOK_URL",  "http://localhost:9001/receive-pool")
os.environ.setdefault("SAGE_WEBHOOK_URL",    "http://localhost:9002/receive-pool")
os.environ.setdefault("ATLAS_WEBHOOK_URL",   "http://localhost:9003/receive-pool")
os.environ.setdefault("ORACLE_URL",          "http://localhost:8007")
os.environ.setdefault("ORACLE_SECRET",       "test-oracle-secret")
os.environ.setdefault("AXIOM_DB_PATH",       _TEST_DB)

from regime import Regime, classify_regime, classify_regime_v2


# ── V1: NORMAL composite regime ───────────────────────────────────────────────

def test_v1_composite_normal():
    """V1: VIX=19, HY=320bps, yield_curve=+45bps, P/C=0.78 → NORMAL (~score 18)."""
    macro = {
        "vix": 19.0,
        "hy_spread_bps": 320.0,
        "yield_spread_bps": 45.0,
        "put_call_ratio": 0.78,
        "is_estimated": False,
    }
    regime = classify_regime_v2(macro)

    assert regime.regime_source == "COMPOSITE"
    assert regime.classification == "NORMAL"
    assert 16 <= regime.composite_score <= 30
    assert regime.alpha_credit_allowed is True
    assert regime.alpha_debit_allowed is True
    assert regime.prime_allowed is True
    assert regime.alpha_size_mult == 1.0
    # v3 backward compat: to_dict() still has all old keys
    d = regime.to_dict()
    for key in ("vix", "classification", "strategy_bias", "alpha_credit_allowed",
                "alpha_debit_allowed", "prime_allowed", "alpha_size_mult", "is_estimated"):
        assert key in d


# ── V2: STRESS composite regime ───────────────────────────────────────────────

def test_v2_composite_stress():
    """V2: VIX=27 (17), HY=450bps (12), yield_curve=-30bps (17), P/C=1.05 (12) → STRESS (score=58)."""
    macro = {
        "vix": 27.0,
        "hy_spread_bps": 450.0,   # 400–499 = 12pts
        "yield_spread_bps": -30.0, # -50 to -1 = 17pts
        "put_call_ratio": 1.05,    # 1.00–1.19 = 12pts
        "is_estimated": False,
    }
    regime = classify_regime_v2(macro)

    assert regime.regime_source == "COMPOSITE"
    assert regime.classification == "STRESS"
    assert 46 <= regime.composite_score <= 60
    assert regime.alpha_credit_allowed is True
    assert regime.alpha_debit_allowed is False   # paused in STRESS
    assert regime.prime_daily_cap == 3


# ── V3: CRISIS composite regime ───────────────────────────────────────────────

def test_v3_composite_crisis():
    """V3: VIX=38, HY=720bps, yield_curve=-120bps, P/C=1.45 → CRISIS (score=100)."""
    macro = {
        "vix": 38.0,
        "hy_spread_bps": 720.0,
        "yield_spread_bps": -120.0,
        "put_call_ratio": 1.45,
        "is_estimated": False,
    }
    regime = classify_regime_v2(macro)

    assert regime.regime_source == "COMPOSITE"
    assert regime.classification == "CRISIS"
    assert regime.composite_score == 100
    assert regime.alpha_credit_allowed is False
    assert regime.alpha_debit_allowed is False
    assert regime.prime_allowed is False
    assert regime.alpha_size_mult == 0.0
    assert regime.alpha_credit_daily_cap == 0
    assert regime.prime_daily_cap == 0


# ── V4: ORACLE unavailable → VIX-only fallback ───────────────────────────────

def test_v4_oracle_unavailable_vix_fallback():
    """V4: ORACLE down → classify_regime_v2(None) → VIX-only fallback, no crash."""
    # Empty macro_data triggers fallback
    regime = classify_regime_v2({})

    # Must not crash, must return a valid regime
    assert regime is not None
    assert regime.classification in ("LOW_VOL", "NORMAL", "ELEVATED", "STRESS",
                                     "HIGH_STRESS", "CRISIS")
    # Source must indicate fallback
    assert regime.regime_source == "VIX_ONLY_FALLBACK"


def test_v4_oracle_macro_none_fallback():
    """V4: classify_regime_v2(None) → fallback to VIX-only, no crash."""
    regime = classify_regime_v2(None)
    assert regime is not None
    assert regime.regime_source == "VIX_ONLY_FALLBACK"


# ── V5: Pre-warm fires after Tier 1 ──────────────────────────────────────────

def test_v5_prefetch_preliminary_after_tier1():
    """V5: After Tier 1, ORACLE prefetch called with tier=preliminary."""
    from scheduler import _run_tier1

    mock_anchors = ["NVDA", "AAPL", "JPM", "MSFT", "TSLA"]

    with patch("tier1_filter.run_tier1_filter", return_value=mock_anchors), \
         patch("database.save_anchor_stocks"), \
         patch("oracle_client.prefetch", return_value=True) as mock_prefetch:

        from config import load_settings
        settings = load_settings()
        app_state = {"settings": settings, "anchor_stocks": [], "last_vix": 18.0}

        _run_tier1(app_state, "morning")

        mock_prefetch.assert_called_once()
        call_args = mock_prefetch.call_args
        assert call_args[0][0] == mock_anchors    # tickers (positional arg)
        assert call_args[1]["tier"] == "preliminary"  # tier (keyword arg)
        assert app_state["anchor_stocks"] == mock_anchors


# ── V6: Pre-warm fires after Tier 2 ──────────────────────────────────────────

def test_v6_prefetch_full_after_tier2():
    """V6: After Tier 2, ORACLE prefetch called with tier=full and pool tickers."""
    from scheduler import _run_tier2_if_open

    # Pool must be ≥ 10 to pass the pool-protection gate in scheduler
    mock_pool = ["NVDA", "AAPL", "JPM", "MSFT", "GOOGL",
                 "AMD", "TSLA", "META", "V", "MA"]
    mock_macro = {
        "vix": 18.5, "hy_spread_bps": 310.0,
        "yield_spread_bps": 50.0, "put_call_ratio": 0.75,
        "is_estimated": False,
    }

    from config import load_settings
    settings = load_settings()
    app_state = {
        "settings": settings,
        "anchor_stocks": mock_pool,
        "pool": [],
        "last_vix": 18.5,
        "tier2_consecutive_failures": 0,
    }

    with patch("scheduler._is_market_hours", return_value=True), \
         patch("oracle_client.get_macro_data", return_value=mock_macro), \
         patch("tier2_filter.run_tier2_filter", return_value=mock_pool), \
         patch("database.save_pool_snapshot"), \
         patch("oracle_client.prefetch", return_value=True) as mock_prefetch, \
         patch("oracle_client.get_coherence_scores", return_value={}), \
         patch("oracle_client.get_cycle_intelligence", return_value=None), \
         patch("agent_push.push_pool_to_agents", return_value={}), \
         patch("main.current_window_id", return_value="2026-04-11-1015"):

        _run_tier2_if_open(app_state)

        mock_prefetch.assert_called_once()
        call_args = mock_prefetch.call_args
        assert set(call_args[0][0]) == set(mock_pool)  # pool tickers (positional)
        assert call_args[1]["tier"] == "full"           # tier (keyword arg)


# ── V7: Coherence scores attached to pool payload ────────────────────────────

def test_v7_coherence_scores_in_pool_payload():
    """V7: Coherence scores returned from ORACLE appear in pool push payload."""
    from scheduler import _run_tier2_if_open

    mock_pool = ["NVDA", "AAPL"]
    mock_macro = {"vix": 18.0, "hy_spread_bps": 300.0,
                  "yield_spread_bps": 60.0, "put_call_ratio": 0.72}
    mock_coherence = {
        "NVDA": {"score": 82, "level": "HIGH", "flag_count": 0},
        "AAPL": {"score": 55, "level": "MEDIUM", "flag_count": 1},
    }

    from config import load_settings
    settings = load_settings()
    app_state = {
        "settings": settings,
        "anchor_stocks": ["NVDA", "AAPL", "JPM"],
        "pool": [],
        "last_vix": 18.0,
        "tier2_consecutive_failures": 0,
    }

    captured_payload = {}

    def capture_push(pool_payload, **kwargs):
        captured_payload.update(pool_payload)
        return {}

    with patch("scheduler._is_market_hours", return_value=True), \
         patch("oracle_client.get_macro_data", return_value=mock_macro), \
         patch("tier2_filter.run_tier2_filter", return_value=mock_pool), \
         patch("database.save_pool_snapshot"), \
         patch("oracle_client.prefetch", return_value=True), \
         patch("oracle_client.get_coherence_scores", return_value=mock_coherence), \
         patch("oracle_client.get_cycle_intelligence", return_value=None), \
         patch("agent_push.push_pool_to_agents", side_effect=capture_push), \
         patch("main.current_window_id", return_value="2026-04-11-1015"):

        _run_tier2_if_open(app_state)

    assert captured_payload.get("coherence_available") is True
    assert captured_payload["coherence_summary"]["NVDA"]["score"] == 82
    assert captured_payload["coherence_summary"]["AAPL"]["level"] == "MEDIUM"


# ── V8: Coherence timeout → pool push still fires ────────────────────────────

def test_v8_coherence_timeout_pool_push_still_fires():
    """V8: Coherence query returns empty → pool push fires with coherence_available=false."""
    from scheduler import _run_tier2_if_open

    mock_pool = ["NVDA", "AAPL"]
    mock_macro = {"vix": 18.0, "hy_spread_bps": 300.0,
                  "yield_spread_bps": 60.0, "put_call_ratio": 0.72}

    from config import load_settings
    settings = load_settings()
    app_state = {
        "settings": settings,
        "anchor_stocks": ["NVDA", "AAPL"],
        "pool": [],
        "last_vix": 18.0,
        "tier2_consecutive_failures": 0,
    }

    captured_payload = {}

    def capture_push(pool_payload, **kwargs):
        captured_payload.update(pool_payload)
        return {}

    with patch("scheduler._is_market_hours", return_value=True), \
         patch("oracle_client.get_macro_data", return_value=mock_macro), \
         patch("tier2_filter.run_tier2_filter", return_value=mock_pool), \
         patch("database.save_pool_snapshot"), \
         patch("oracle_client.prefetch", return_value=True), \
         patch("oracle_client.get_coherence_scores", return_value={}), \
         patch("oracle_client.get_cycle_intelligence", return_value=None), \
         patch("agent_push.push_pool_to_agents", side_effect=capture_push), \
         patch("main.current_window_id", return_value="2026-04-11-1015"):

        _run_tier2_if_open(app_state)

    # Pool push MUST fire even with no coherence data
    assert len(captured_payload) > 0
    assert captured_payload.get("coherence_available") is False
    assert captured_payload.get("pool") == mock_pool


# ── V9: Pattern intelligence in pool payload ─────────────────────────────────

def test_v9_pattern_intelligence_in_payload():
    """V9: Echo chamber risk and cycle patterns appear in pool push payload."""
    from scheduler import _run_tier2_if_open

    mock_pool = ["NVDA", "AMD", "SMCI", "AAPL"]
    mock_macro = {"vix": 20.0, "hy_spread_bps": 350.0,
                  "yield_spread_bps": 30.0, "put_call_ratio": 0.85}
    mock_intel = {
        "patterns_detected": [{
            "type": "SECTOR_FLOW_EVENT",
            "description": "3 semiconductor tickers show correlated sweep activity",
        }],
        "echo_chamber_risk_tickers": ["NVDA", "AMD", "SMCI"],
    }

    from config import load_settings
    settings = load_settings()
    app_state = {
        "settings": settings,
        "anchor_stocks": mock_pool,
        "pool": [],
        "last_vix": 20.0,
        "tier2_consecutive_failures": 0,
    }

    captured_payload = {}

    def capture_push(pool_payload, **kwargs):
        captured_payload.update(pool_payload)
        return {}

    with patch("scheduler._is_market_hours", return_value=True), \
         patch("oracle_client.get_macro_data", return_value=mock_macro), \
         patch("tier2_filter.run_tier2_filter", return_value=mock_pool), \
         patch("database.save_pool_snapshot"), \
         patch("oracle_client.prefetch", return_value=True), \
         patch("oracle_client.get_coherence_scores", return_value={}), \
         patch("oracle_client.get_cycle_intelligence", return_value=mock_intel), \
         patch("agent_push.push_pool_to_agents", side_effect=capture_push), \
         patch("main.current_window_id", return_value="2026-04-11-1015"):

        _run_tier2_if_open(app_state)

    assert captured_payload.get("pattern_intelligence_available") is True
    assert set(captured_payload["echo_chamber_risk"]) == {"NVDA", "AMD", "SMCI"}
    assert len(captured_payload["cycle_patterns"]) == 1
    assert "SECTOR_FLOW_EVENT" in captured_payload["cycle_patterns"][0]


# ── V10: ORACLE fully down → complete pool push fires ────────────────────────

def test_v10_oracle_fully_down_pool_push_fires():
    """V10: All ORACLE calls fail → pool push fires with all degraded flags correct."""
    from scheduler import _run_tier2_if_open
    from data_sources import get_vix_with_fallback

    mock_pool = ["NVDA", "AAPL"]

    from config import load_settings
    settings = load_settings()
    app_state = {
        "settings": settings,
        "anchor_stocks": mock_pool,
        "pool": [],
        "last_vix": 19.0,
        "tier2_consecutive_failures": 0,
    }

    captured_payload = {}

    def capture_push(pool_payload, **kwargs):
        captured_payload.update(pool_payload)
        return {}

    with patch("scheduler._is_market_hours", return_value=True), \
         patch("oracle_client.get_macro_data", return_value=None), \
         patch("data_sources.get_vix_with_fallback", return_value=(19.0, False)), \
         patch("tier2_filter.run_tier2_filter", return_value=mock_pool), \
         patch("database.save_pool_snapshot"), \
         patch("oracle_client.prefetch", return_value=False), \
         patch("oracle_client.get_coherence_scores", return_value={}), \
         patch("oracle_client.get_cycle_intelligence", return_value=None), \
         patch("agent_push.push_pool_to_agents", side_effect=capture_push), \
         patch("main.current_window_id", return_value="2026-04-11-1015"):

        _run_tier2_if_open(app_state)

    # Pool push MUST fire regardless
    assert len(captured_payload) > 0
    assert captured_payload.get("pool") == mock_pool
    # Degraded flags
    assert captured_payload.get("coherence_available") is False
    assert captured_payload.get("pattern_intelligence_available") is False
    assert captured_payload.get("oracle_warmed") is False
    # Regime fell back to VIX-only
    assert captured_payload["regime"]["regime_source"] == "VIX_ONLY_FALLBACK"


# ── V11: /oracle/status endpoint ─────────────────────────────────────────────

def test_v11_oracle_status_endpoint():
    """V11: /oracle/status returns correct fields with valid auth."""
    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)
    headers = {"X-Axiom-Secret": "test-secret-12345"}

    with patch("oracle_client.health_check", return_value=True):
        resp = client.get("/oracle/status", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert "oracle_reachable" in data
    assert "regime_source" in data
    assert "composite_score" in data
    assert data["oracle_reachable"] is True


def test_v11_oracle_status_requires_auth():
    """V11: /oracle/status rejects missing auth."""
    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)
    resp = client.get("/oracle/status")
    assert resp.status_code == 403


# ── V12: Regime to_dict() backward compatibility ─────────────────────────────

def test_v12_regime_to_dict_backward_compatible():
    """V12: v4 Regime.to_dict() contains all v3 keys — existing consumers won't break."""
    V3_KEYS = {
        "vix", "classification", "strategy_bias",
        "alpha_credit_allowed", "alpha_debit_allowed", "prime_allowed",
        "alpha_credit_daily_cap", "prime_daily_cap", "alpha_size_mult", "is_estimated",
    }
    V4_NEW_KEYS = {"composite_score", "hy_spread_bps", "yield_curve_bps",
                   "put_call_ratio", "regime_source"}

    macro = {
        "vix": 21.0, "hy_spread_bps": 400.0,
        "yield_spread_bps": 20.0, "put_call_ratio": 0.90,
    }
    regime = classify_regime_v2(macro)
    d = regime.to_dict()

    # All v3 keys present
    for key in V3_KEYS:
        assert key in d, f"Missing v3 key: {key}"

    # All v4 keys present
    for key in V4_NEW_KEYS:
        assert key in d, f"Missing v4 key: {key}"

    # Composite score in valid range
    assert 0 <= d["composite_score"] <= 100

    # VIX-based fallback also has all keys
    vix_regime = classify_regime(21.0)
    vix_dict = vix_regime.to_dict()
    for key in V3_KEYS:
        assert key in vix_dict, f"v3 fallback missing key: {key}"
