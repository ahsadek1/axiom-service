"""
test_g9_auth_registry.py — G9: Auth Header Registry Tests
"""
import os
import sys
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared.auth_registry import (
    AUTH_REGISTRY,
    AuthConfigError,
    get_registry_summary,
    validate_service_auth_config,
)

# ─── T1: All required env vars set for alpha-buffer → no error ────────────────

def test_t1_alpha_buffer_valid_env_passes():
    """T1: All required env vars set for alpha-buffer → no error raised."""
    secret = "a" * 32
    with patch.dict(os.environ, {"NEXUS_SECRET": secret}):
        validate_service_auth_config("alpha-buffer")  # must not raise


# ─── T2: NEXUS_PRIME_SECRET missing for omni → AuthConfigError ───────────────

def test_t2_omni_missing_prime_secret_raises():
    """T2: NEXUS_PRIME_SECRET missing for omni → AuthConfigError."""
    secret = "a" * 32
    env = {"NEXUS_SECRET": secret, "NEXUS_PRIME_SECRET": ""}
    with patch.dict(os.environ, env, clear=False):
        with pytest.raises(AuthConfigError) as exc_info:
            validate_service_auth_config("omni")
    assert "NEXUS_PRIME_SECRET" in str(exc_info.value)


# ─── T3: NEXUS_SECRET too short (10 chars) → AuthConfigError ─────────────────

def test_t3_nexus_secret_too_short_raises():
    """T3: NEXUS_SECRET too short → AuthConfigError mentions min length."""
    short_secret = "short1234"  # 9 chars, min is 32
    with patch.dict(os.environ, {"NEXUS_SECRET": short_secret, "NEXUS_PRIME_SECRET": ""}):
        with pytest.raises(AuthConfigError) as exc_info:
            validate_service_auth_config("alpha-buffer")
    assert "too short" in str(exc_info.value)


# ─── T4: validate_service_auth_config("omni") with both secrets valid → None ─

def test_t4_omni_both_secrets_valid_returns_none():
    """T4: Both NEXUS_SECRET and NEXUS_PRIME_SECRET valid for omni → returns None."""
    secret = "b" * 32
    env = {"NEXUS_SECRET": secret, "NEXUS_PRIME_SECRET": secret}
    with patch.dict(os.environ, env, clear=False):
        result = validate_service_auth_config("omni")
    assert result is None


# ─── T5: Registry has >= 9 service entries ────────────────────────────────────

def test_t5_registry_has_at_least_9_services():
    """T5: AUTH_REGISTRY contains at least 9 service entries."""
    assert len(AUTH_REGISTRY) >= 9, (
        f"Expected >= 9 services in AUTH_REGISTRY, got {len(AUTH_REGISTRY)}: "
        f"{list(AUTH_REGISTRY.keys())}"
    )


# ─── T6: Unknown service name raises KeyError ────────────────────────────────

def test_t6_unknown_service_raises_key_error():
    """T6: Unknown service name raises KeyError."""
    with pytest.raises(KeyError) as exc_info:
        validate_service_auth_config("nonexistent-service")
    assert "nonexistent-service" in str(exc_info.value)


# ─── T7: guardian-angel has empty entries → no error ─────────────────────────

def test_t7_guardian_angel_empty_entries_no_error():
    """T7: guardian-angel has empty entry list → validate raises no error."""
    assert AUTH_REGISTRY["guardian-angel"] == [], (
        "guardian-angel should have empty entries (no auth for health endpoints)"
    )
    validate_service_auth_config("guardian-angel")  # must not raise


# ─── T8: get_registry_summary() returns dict with all services ───────────────

def test_t8_get_registry_summary_returns_all_services():
    """T8: get_registry_summary() returns a dict containing all registered services."""
    summary = get_registry_summary()
    assert isinstance(summary, dict)
    for service in AUTH_REGISTRY:
        assert service in summary, f"Service '{service}' missing from registry summary"
    # Each entry should have env_var, role, direction keys
    for service, entries in summary.items():
        for entry in entries:
            assert "env_var" in entry
            assert "role" in entry
            assert "direction" in entry
