"""
test_g8_qi_fail_open.py — G8: QI Fail-Open Audit Tests

Tests MIN_BRAINS_REQUIRED startup guard, brain degradation alerts,
and synthesis behavior with degraded brain response counts.
"""
import os
import sys
import time
import tempfile
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set required env vars before any omni module imports (mirrors test_api.py pattern)
os.environ.setdefault("NEXUS_WEBHOOK_SECRET", "test-nexus-secret-g8xxxxxxxxxxxxxxxxxxxxxxxxxxxx")
os.environ.setdefault("NEXUS_PRIME_SECRET",   "test-prime-secret-g8xxxxxxxxxxxxxxxxxxxxxxxxxxxx")
os.environ.setdefault("OMNI_SECRET",          "test-omni-secret-g8")
os.environ.setdefault("ANTHROPIC_API_KEY",    "test-anthropic-g8")
os.environ.setdefault("OPENAI_API_KEY",       "test-openai-g8")
os.environ.setdefault("GEMINI_API_KEY",       "test-gemini-g8")
os.environ.setdefault("DEEPSEEK_API_KEY",     "test-deepseek-g8")
os.environ.setdefault("AXIOM_URL",            "http://localhost:8001")
os.environ.setdefault("AXIOM_SECRET",         "test-axiom-secret-g8")
os.environ.setdefault("ALPHA_EXECUTION_URL",  "http://localhost:8005")
os.environ.setdefault("PRIME_EXECUTION_URL",  "http://localhost:8006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN",   "9999:TEST_G8")
os.environ.setdefault("AHMED_CHAT_ID",        "8573754783")
os.environ.setdefault("OMNI_DB_PATH",         tempfile.mktemp(suffix="_g8.db"))

from synthesis import compute_verdict, _maybe_alert_brain_degradation, _brain_alert_sent


# ─── Helper ──────────────────────────────────────────────────────────────────

def _make_brain_results(responding: list, error_brains: list = None) -> dict:
    """Build brain_results dict with responding brains voting GO."""
    all_brains = ["claude", "o3mini", "gemini", "deepseek"]
    results = {}
    error_brains = error_brains or []
    for brain in all_brains:
        if brain in error_brains:
            results[brain] = {"error": "API failure", "vote": ""}
        elif brain in responding:
            results[brain] = {"vote": "GO", "error": ""}
        else:
            results[brain] = {"error": "timeout", "vote": ""}
    return results


# ─── T1: MIN_BRAINS_REQUIRED=3 → startup check passes ───────────────────────

def test_t1_min_brains_required_3_passes():
    """T1: MIN_BRAINS_REQUIRED=3 -> startup guard does not raise."""
    from main import _check_min_brains_required
    _check_min_brains_required(3)  # must not raise


# ─── T2: MIN_BRAINS_REQUIRED=2 → RuntimeError raised ────────────────────────

def test_t2_min_brains_required_2_raises():
    """T2: MIN_BRAINS_REQUIRED=2 -> RuntimeError raised."""
    from main import _check_min_brains_required
    with pytest.raises(RuntimeError) as exc_info:
        _check_min_brains_required(2)
    assert "safe minimum" in str(exc_info.value) or "below" in str(exc_info.value)


# ─── T3: MIN_BRAINS_REQUIRED=1 → RuntimeError raised ────────────────────────

def test_t3_min_brains_required_1_raises():
    """T3: MIN_BRAINS_REQUIRED=1 -> RuntimeError raised."""
    from main import _check_min_brains_required
    with pytest.raises(RuntimeError):
        _check_min_brains_required(1)


# ─── T4: 2 brains respond, both GO → CONDITIONAL verdict ────────────────────

def test_t4_two_brains_respond_both_go_is_conditional():
    """T4: Only 2 brains respond (< MIN_BRAINS_REQUIRED=3) -> CONDITIONAL."""
    brain_results = _make_brain_results(["claude", "o3mini"])
    verdict = compute_verdict(
        brain_results=brain_results,
        pathway="P1",
        concordance_sizing=1.0,
        axiom_result=None,
    )
    assert verdict.verdict == "CONDITIONAL"
    assert verdict.brains_responded == 2
    assert verdict.sizing_mult == 0.0


# ─── T5: 0 brains respond → CONDITIONAL ─────────────────────────────────────

def test_t5_zero_brains_respond_is_conditional():
    """T5: No brains respond (all error) -> CONDITIONAL (< MIN_BRAINS_REQUIRED)."""
    brain_results = _make_brain_results([], error_brains=["claude", "o3mini", "gemini", "deepseek"])
    verdict = compute_verdict(
        brain_results=brain_results,
        pathway="P1",
        concordance_sizing=1.0,
        axiom_result=None,
    )
    assert verdict.verdict == "CONDITIONAL"
    assert verdict.brains_responded == 0


# ─── T6: Brain returns empty string vote → not counted in brains_responded ───

def test_t6_empty_vote_not_counted_as_brain_responded():
    """T6: Brain result with empty vote string -> treated as error, not counted."""
    brain_results = {
        "claude":   {"vote": "GO",  "error": ""},
        "o3mini":   {"vote": "GO",  "error": ""},
        "gemini":   {"vote": "GO",  "error": ""},
        "deepseek": {"vote": "",    "error": ""},  # empty vote = error
    }
    verdict = compute_verdict(
        brain_results=brain_results,
        pathway="P1",
        concordance_sizing=1.0,
        axiom_result=None,
    )
    # Only 3 brains responded (deepseek's empty vote not counted)
    assert verdict.brains_responded == 3


# ─── T7: _maybe_alert_brain_degradation with brains_responded=2 → alert fires

def test_t7_degradation_alert_fires_for_two_brains():
    """T7: _maybe_alert_brain_degradation(brains_responded=2) -> CRITICAL + telegram alert."""
    # Clear rate-limit state for this ticker
    _brain_alert_sent.pop("AAPL_TEST", None)

    with patch("synthesis.send_brain_degradation_alert") as mock_alert:
        _maybe_alert_brain_degradation(
            ticker="AAPL_TEST",
            brains_responded=2,
            brain_summary={"claude": "GO", "o3mini": "GO"},
            bot_token="token",
            chat_id="chat",
        )
    mock_alert.assert_called_once()


# ─── T8: _maybe_alert_brain_degradation called twice within 1hr → once only ─

def test_t8_rate_limit_prevents_duplicate_alert_within_one_hour():
    """T8: Same ticker alert within 3600s -> second call is suppressed."""
    _brain_alert_sent.pop("TSLA_TEST", None)

    with patch("synthesis.send_brain_degradation_alert") as mock_alert:
        # First call
        _maybe_alert_brain_degradation(
            ticker="TSLA_TEST",
            brains_responded=2,
            brain_summary={"claude": "GO", "o3mini": "GO"},
            bot_token="token",
            chat_id="chat",
        )
        # Second call immediately after (well within 1hr)
        _maybe_alert_brain_degradation(
            ticker="TSLA_TEST",
            brains_responded=2,
            brain_summary={"claude": "GO", "o3mini": "GO"},
            bot_token="token",
            chat_id="chat",
        )

    # Alert should only fire once
    assert mock_alert.call_count == 1
