"""
test_g11_api_key_boot_validation.py — G11: API Key Boot Validation Tests

Verifies ApiKeyValidator behavior:
  - 2xx response → status='ok' with latency
  - 401/403 → status='failed' immediately, no retry
  - 429 → status='degraded' (key valid, rate limited)
  - Timeout → retries, then status='failed'
  - ConnectionError → retries, then status='failed'
  - raise_on_critical_failures raises RuntimeError for critical API failures
  - raise_on_critical_failures does NOT raise for non-critical failures
"""
import os
import sys
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared.api_key_validator import ApiKeyValidator, ValidationResult


def _resp(status_code: int, json_data: dict = None) -> MagicMock:
    m = MagicMock()
    m.status_code = status_code
    m.json.return_value = json_data or {}
    return m


validator = ApiKeyValidator()


# ─── T1: 200 response → status='ok' ──────────────────────────────────────────

def test_t1_success_returns_ok():
    """T1: 200 response → ValidationResult.status='ok'."""
    with patch("requests.get", return_value=_resp(200, {"status": "ACTIVE"})):
        result = validator.validate_alpaca("key", "secret", "https://paper-api.alpaca.markets")
    assert result.status == "ok"
    assert result.api_name == "alpaca"


# ─── T2: 401 response → status='failed' immediately ─────────────────────────

def test_t2_401_returns_failed():
    """T2: 401 Unauthorized → status='failed'."""
    with patch("requests.get", return_value=_resp(401)):
        result = validator.validate_alpaca("bad_key", "bad_secret", "https://paper-api.alpaca.markets")
    assert result.status == "failed"
    assert "401" in result.message


# ─── T3: 403 response → status='failed' ──────────────────────────────────────

def test_t3_403_returns_failed():
    """T3: 403 Forbidden → status='failed'."""
    with patch("requests.get", return_value=_resp(403)):
        result = validator.validate_anthropic("bad_key")
    assert result.status == "failed"
    assert "403" in result.message


# ─── T4: 429 response → status='degraded' ────────────────────────────────────

def test_t4_429_returns_degraded():
    """T4: 429 rate-limited → status='degraded' (key is valid)."""
    with patch("requests.get", return_value=_resp(429)):
        result = validator.validate_openai("valid_key_rate_limited")
    assert result.status == "degraded"
    assert "429" in result.message or "Rate limited" in result.message


# ─── T5: Timeout on all retries → status='failed' ────────────────────────────

def test_t5_timeout_all_retries_returns_failed():
    """T5: Timeout on every probe attempt → status='failed' after retries."""
    import requests as _req
    with patch("requests.get", side_effect=_req.exceptions.Timeout):
        with patch("time.sleep"):
            result = validator.validate_gemini("key")
    assert result.status == "failed"
    assert "Timeout" in result.message or "attempts" in result.message


# ─── T6: ConnectionError on all retries → status='failed' ────────────────────

def test_t6_connection_error_all_retries_returns_failed():
    """T6: ConnectionError on every attempt → status='failed' after retries."""
    import requests as _req
    with patch("requests.get", side_effect=_req.exceptions.ConnectionError("unreachable")):
        with patch("time.sleep"):
            result = validator.validate_polygon("key")
    assert result.status == "failed"


# ─── T7: Success on 3rd retry → status='ok' ──────────────────────────────────

def test_t7_success_on_third_retry():
    """T7: First 2 attempts fail (timeout), 3rd succeeds → status='ok'."""
    import requests as _req
    call_count = [0]
    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] <= 2:
            raise _req.exceptions.Timeout
        return _resp(200)
    with patch("requests.get", side_effect=side_effect):
        with patch("time.sleep"):
            result = validator.validate_deepseek("key")
    assert result.status == "ok"
    assert call_count[0] == 3


# ─── T8: raise_on_critical_failures raises for critical failure ───────────────

def test_t8_raise_on_critical_failures_raises():
    """T8: Critical API with status='failed' → RuntimeError raised."""
    results = [
        ValidationResult("alpaca", "failed", "HTTP 401", 50),
        ValidationResult("anthropic", "ok", "HTTP 200", 120),
    ]
    with pytest.raises(RuntimeError, match="alpaca"):
        validator.raise_on_critical_failures(results, critical_apis=["alpaca", "anthropic"])


# ─── T9: raise_on_critical_failures does not raise for non-critical failure ───

def test_t9_raise_on_critical_ignores_non_critical():
    """T9: Non-critical API failure → no RuntimeError."""
    results = [
        ValidationResult("alpaca", "ok", "HTTP 200", 80),
        ValidationResult("orats", "failed", "timeout", 0),
    ]
    # Should not raise — orats is not in critical_apis
    validator.raise_on_critical_failures(results, critical_apis=["alpaca"])


# ─── T10: ValidationResult fields are correctly populated ────────────────────

def test_t10_validation_result_fields():
    """T10: ValidationResult dataclass has correct field types."""
    r = ValidationResult(api_name="test", status="ok", message="HTTP 200 in 45ms", latency_ms=45)
    assert r.api_name == "test"
    assert r.status == "ok"
    assert r.latency_ms == 45
    assert isinstance(r.message, str)
