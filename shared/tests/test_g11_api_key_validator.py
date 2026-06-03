"""
test_g11_api_key_validator.py — G11: API Key Boot Validator Tests

All HTTP calls are mocked via unittest.mock.
"""
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared.api_key_validator import ApiKeyValidator, ValidationResult


def _make_resp(status_code: int) -> MagicMock:
    """Helper: create a mock requests.Response with given status code."""
    resp = MagicMock()
    resp.status_code = status_code
    return resp


# ─── T1: 200 response → ok ───────────────────────────────────────────────────

def test_t1_200_response_returns_ok():
    """T1: Probe returns HTTP 200 → status 'ok'."""
    validator = ApiKeyValidator()
    with patch("requests.get", return_value=_make_resp(200)):
        result = validator.validate_alpaca(
            "key", "secret", "https://paper-api.alpaca.markets"
        )
    assert result.status == "ok"
    assert result.api_name == "alpaca"


# ─── T2: 401 response → failed ───────────────────────────────────────────────

def test_t2_401_response_returns_failed():
    """T2: Probe returns HTTP 401 → status 'failed' (invalid key)."""
    validator = ApiKeyValidator()
    with patch("requests.get", return_value=_make_resp(401)):
        result = validator.validate_openai("bad_key")
    assert result.status == "failed"
    assert "401" in result.message


# ─── T3: 403 response → failed ───────────────────────────────────────────────

def test_t3_403_response_returns_failed():
    """T3: Probe returns HTTP 403 → status 'failed'."""
    validator = ApiKeyValidator()
    with patch("requests.get", return_value=_make_resp(403)):
        result = validator.validate_anthropic("bad_key")
    assert result.status == "failed"
    assert "403" in result.message


# ─── T4: 429 response → degraded (key is valid, rate limited) ────────────────

def test_t4_429_response_returns_degraded():
    """T4: Probe returns HTTP 429 → status 'degraded' (key is valid, rate limited)."""
    validator = ApiKeyValidator()
    with patch("requests.get", return_value=_make_resp(429)):
        result = validator.validate_gemini("some_key")
    assert result.status == "degraded"
    assert "429" in result.message or "rate" in result.message.lower()


# ─── T5: timeout on all retries → failed ─────────────────────────────────────

def test_t5_timeout_all_retries_returns_failed():
    """T5: All probe attempts time out → status 'failed' after MAX_PROBE_RETRIES."""
    import requests as _req
    validator = ApiKeyValidator()

    with patch("requests.get", side_effect=_req.exceptions.Timeout):
        with patch("time.sleep"):  # skip sleep delays
            result = validator.validate_deepseek("some_key")

    assert result.status == "failed"
    assert "attempts" in result.message.lower() or "failed" in result.message.lower()


# ─── T6: connection error retries then fails ─────────────────────────────────

def test_t6_connection_error_all_retries_returns_failed():
    """T6: All probe attempts raise ConnectionError → status 'failed'."""
    validator = ApiKeyValidator()

    with patch("requests.get", side_effect=ConnectionError("unreachable")):
        with patch("time.sleep"):
            result = validator.validate_polygon("some_key")

    assert result.status == "failed"


# ─── T7: raise_on_critical_failures with failed critical API → RuntimeError ──

def test_t7_critical_failure_raises_runtime_error():
    """T7: raise_on_critical_failures raises RuntimeError if a critical API failed."""
    validator = ApiKeyValidator()
    results = [
        ValidationResult(api_name="alpaca", status="failed", message="401 invalid", latency_ms=10),
        ValidationResult(api_name="openai", status="ok", message="200 in 50ms", latency_ms=50),
    ]
    with pytest.raises(RuntimeError) as exc_info:
        validator.raise_on_critical_failures(results, critical_apis=["alpaca"])
    assert "alpaca" in str(exc_info.value)


# ─── T8: raise_on_critical_failures with degraded critical API → no raise ────

def test_t8_degraded_critical_api_does_not_raise():
    """T8: raise_on_critical_failures does NOT raise if critical API is 'degraded' (rate limited = key valid)."""
    validator = ApiKeyValidator()
    results = [
        ValidationResult(api_name="alpaca", status="degraded", message="429 rate limit", latency_ms=5),
    ]
    # Should not raise — degraded means key is valid, just rate limited
    validator.raise_on_critical_failures(results, critical_apis=["alpaca"])


# ─── T9: ValidationResult has correct fields ─────────────────────────────────

def test_t9_validation_result_fields():
    """T9: ValidationResult dataclass has correct api_name, status, message, latency_ms."""
    result = ValidationResult(
        api_name="test_api",
        status="ok",
        message="HTTP 200 in 42ms",
        latency_ms=42,
    )
    assert result.api_name == "test_api"
    assert result.status == "ok"
    assert result.message == "HTTP 200 in 42ms"
    assert result.latency_ms == 42
