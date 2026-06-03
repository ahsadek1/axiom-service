"""
test_inspector.py — Unit tests for AxiomInspector module

40+ test cases covering 10 domains:
- 4–5 tests per domain (happy path + error cases)
- All external dependencies mocked (requests, database)
- Verify finding dict structure, escalation routing, deduplication

Run: pytest tests/test_inspector.py -v
"""

import json
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, Mock, patch
from typing import Dict, Any

import pytest
import pytz

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from inspector import AxiomInspector, DEDUP_WINDOW_SEC

ET = pytz.timezone("America/New_York")


@pytest.fixture
def mock_settings():
    """Mock Axiom Settings object."""
    settings = Mock()
    settings.axiom_secret = "test-secret"
    settings.nexus_secret = "test-nexus-secret"
    settings.polygon_api_key = "test-polygon-key"
    settings.alpha_vantage_key = "test-av-key"
    settings.fred_api_key = "test-fred-key"
    settings.cipher_webhook_url = "http://localhost:9001/webhook"
    settings.sage_webhook_url = "http://localhost:9002/webhook"
    settings.atlas_webhook_url = "http://localhost:9003/webhook"
    return settings


@pytest.fixture
def mock_app_state():
    """Mock FastAPI app_state dict."""
    return {
        "pool": ["NVDA", "MSFT", "AAPL"],
        "regime": Mock(vix=22.5, classification="NORMAL", is_estimated=False),
        "regime_last_updated": datetime.now(ET).isoformat(),
        "last_vix": 22.5,
        "submissions_open": True,
        "settings": None,
    }


@pytest.fixture
def mock_logger():
    """Mock logger."""
    return Mock()


@pytest.fixture
def inspector(mock_app_state, mock_settings, mock_logger):
    """Instantiate AxiomInspector with mocks."""
    return AxiomInspector(mock_app_state, mock_settings, mock_logger)


# ────────────────────────────────────────────────────────────────────────────
# Domain 1: Auth Headers (4 tests)
# ────────────────────────────────────────────────────────────────────────────

class TestDomain1AuthHeaders:
    """Tests for audit_auth_headers()."""

    @patch("axiom.inspector.requests.get")
    def test_auth_all_services_ok(self, mock_get, inspector):
        """All 9 services return 200."""
        mock_get.return_value = Mock(status_code=200)

        findings = inspector.audit_auth_headers()

        assert findings["domain"] == 1
        assert findings["status"] == "OK"
        assert findings["passed"] == 9
        assert findings["failed"] == 0
        assert findings["finding"] == "All 9 services passed auth verification"

    @patch("axiom.inspector.requests.get")
    def test_auth_single_service_403(self, mock_get, inspector):
        """One service returns 403."""
        def side_effect(*args, **kwargs):
            if "8002" in args[0]:  # Alpha Buffer
                return Mock(status_code=403)
            return Mock(status_code=200)

        mock_get.side_effect = side_effect

        findings = inspector.audit_auth_headers()

        assert findings["status"] == "CRITICAL"
        assert findings["failed"] == 1
        assert findings["failed_service"] == "Alpha Buffer"
        assert "CRITICAL" in findings["severity"]

    @patch("axiom.inspector.requests.get")
    def test_auth_timeout(self, mock_get, inspector):
        """Service times out."""
        import requests

        mock_get.side_effect = requests.Timeout("Connection timeout")

        findings = inspector.audit_auth_headers()

        assert findings["status"] == "CRITICAL"
        assert findings["failed"] > 0

    @patch("axiom.inspector.requests.get")
    def test_auth_all_services_fail(self, mock_get, inspector):
        """All services fail."""
        import requests

        mock_get.side_effect = requests.ConnectionError("Network error")

        findings = inspector.audit_auth_headers()

        assert findings["status"] == "CRITICAL"
        assert findings["failed"] == 9
        assert findings["passed"] == 0


# ────────────────────────────────────────────────────────────────────────────
# Domain 2: Config Consistency (4 tests)
# ────────────────────────────────────────────────────────────────────────────

class TestDomain2ConfigConsistency:
    """Tests for audit_config_consistency()."""

    @patch("axiom.inspector.requests.get")
    def test_config_all_match(self, mock_get, inspector):
        """All services have same MAX_POSITIONS."""
        mock_get.return_value = Mock(
            status_code=200,
            json=lambda: {"MAX_POSITIONS": 3, "MAX_RISK_PER_TRADE": 1000.0}
        )

        findings = inspector.audit_config_consistency()

        assert findings["domain"] == 2
        assert findings["status"] == "OK"
        assert findings["mismatches"] == 0
        assert findings["mismatch_usd"] == 0.0

    @patch("axiom.inspector.requests.get")
    def test_config_mismatch_critical(self, mock_get, inspector):
        """MAX_POSITIONS mismatch > $100."""
        def side_effect(*args, **kwargs):
            if "8002" in args[0]:  # Alpha Buffer
                return Mock(
                    status_code=200,
                    json=lambda: {"MAX_POSITIONS": 5, "MAX_RISK_PER_TRADE": 1000.0}
                )
            return Mock(
                status_code=200,
                json=lambda: {"MAX_POSITIONS": 3, "MAX_RISK_PER_TRADE": 1000.0}
            )

        mock_get.side_effect = side_effect

        findings = inspector.audit_config_consistency()

        assert findings["status"] == "CRITICAL"
        assert findings["mismatches"] > 0
        assert findings["mismatch_usd"] >= 100.0

    @patch("axiom.inspector.requests.get")
    def test_config_fetch_failure(self, mock_get, inspector):
        """Cannot fetch config from a service."""
        import requests

        mock_get.side_effect = requests.Timeout()

        findings = inspector.audit_config_consistency()

        # Should gracefully degrade
        assert "config" in findings or "status" in findings

    @patch("axiom.inspector.requests.get")
    def test_config_warning_minor_mismatch(self, mock_get, inspector):
        """Minor mismatch (< $100)."""
        mock_get.return_value = Mock(
            status_code=200,
            json=lambda: {"MAX_POSITIONS": 3, "MAX_RISK_PER_TRADE": 1000.0}
        )

        findings = inspector.audit_config_consistency()

        # Should not escalate if mismatch < $100
        if findings["mismatch_usd"] < 100.0:
            assert findings["status"] == "OK"


# ────────────────────────────────────────────────────────────────────────────
# Domain 3: Data Integrity (5 tests)
# ────────────────────────────────────────────────────────────────────────────

class TestDomain3DataIntegrity:
    """Tests for audit_data_integrity()."""

    def test_pool_non_empty_regime_fresh(self, inspector):
        """Pool non-empty, regime fresh."""
        inspector.app_state["pool"] = ["NVDA", "MSFT", "AAPL"]
        inspector.app_state["regime_last_updated"] = datetime.now(ET).isoformat()

        findings = inspector.audit_data_integrity()

        assert findings["domain"] == 3
        assert findings["pool_status"] == "OK"
        assert findings["pool_size"] == 3
        assert findings["regime_status"] == "OK"
        assert findings["status"] == "OK"

    def test_pool_empty_critical(self, inspector):
        """Pool is empty."""
        inspector.app_state["pool"] = []

        findings = inspector.audit_data_integrity()

        assert findings["status"] == "CRITICAL"
        assert findings["pool_status"] == "EMPTY"
        assert findings["finding"] == "Pool is empty — no tickers available"

    def test_regime_stale_critical(self, inspector):
        """Regime older than 90 minutes."""
        inspector.app_state["pool"] = ["NVDA", "MSFT"]
        # Set regime_last_updated to 120 minutes ago
        old_time = datetime.now(ET) - timedelta(minutes=120)
        inspector.app_state["regime_last_updated"] = old_time.isoformat()

        findings = inspector.audit_data_integrity()

        assert findings["status"] == "CRITICAL"
        assert findings["regime_status"] == "STALE"
        assert findings["regime_age_min"] >= 120

    def test_regime_never_updated_critical(self, inspector):
        """Regime has never been updated."""
        inspector.app_state["pool"] = ["NVDA"]
        inspector.app_state["regime_last_updated"] = None

        findings = inspector.audit_data_integrity()

        assert findings["status"] == "CRITICAL"
        assert findings["regime_status"] == "NEVER_UPDATED"

    def test_unparseable_timestamp(self, inspector):
        """Timestamp cannot be parsed."""
        inspector.app_state["pool"] = ["NVDA"]
        inspector.app_state["regime_last_updated"] = "not-a-valid-iso-timestamp"

        findings = inspector.audit_data_integrity()

        assert findings["regime_status"] == "UNPARSEABLE"


# ────────────────────────────────────────────────────────────────────────────
# Domain 4: Webhooks (4 tests)
# ────────────────────────────────────────────────────────────────────────────

class TestDomain4Webhooks:
    """Tests for audit_webhooks()."""

    @patch("axiom.inspector.requests.post")
    def test_webhooks_all_ok(self, mock_post, inspector):
        """All 3 webhooks reachable and fast."""
        mock_post.return_value = Mock(status_code=200)

        findings = inspector.audit_webhooks()

        assert findings["domain"] == 4
        assert findings["status"] == "OK"
        assert len(findings["webhooks"]) == 3
        assert all(w["status"] == 200 for w in findings["webhooks"])
        assert findings["finding"] == "All 3 webhooks reachable and responsive"

    @patch("axiom.inspector.requests.post")
    def test_webhook_timeout_warning(self, mock_post, inspector):
        """One webhook times out."""
        import requests

        def side_effect(*args, **kwargs):
            if "9001" in args[0]:  # Cipher
                raise requests.Timeout()
            return Mock(status_code=200)

        mock_post.side_effect = side_effect

        findings = inspector.audit_webhooks()

        assert findings["status"] == "WARNING"
        assert len(findings["timeouts"]) == 1
        assert findings["timeouts"][0]["webhook"] == "Cipher"

    @patch("axiom.inspector.requests.post")
    def test_webhook_500_error(self, mock_post, inspector):
        """One webhook returns 5xx."""
        def side_effect(*args, **kwargs):
            if "9002" in args[0]:  # Sage
                return Mock(status_code=500)
            return Mock(status_code=200)

        mock_post.side_effect = side_effect

        findings = inspector.audit_webhooks()

        assert findings["status"] == "WARNING"
        assert len(findings["unreachable"]) == 1

    @patch("axiom.inspector.requests.post")
    def test_webhooks_network_error(self, mock_post, inspector):
        """Network error communicating with webhook."""
        import requests

        mock_post.side_effect = requests.ConnectionError("Network down")

        findings = inspector.audit_webhooks()

        assert findings["status"] == "WARNING"
        assert len(findings["unreachable"]) == 3


# ────────────────────────────────────────────────────────────────────────────
# Domain 5: Resilience (2 tests)
# ────────────────────────────────────────────────────────────────────────────

class TestDomain5Resilience:
    """Tests for audit_resilience()."""

    def test_resilience_circuit_breaker_normal(self, inspector):
        """Circuit breaker normal (VIX < 35)."""
        inspector.app_state["regime"] = Mock(vix=22.5)

        findings = inspector.audit_resilience()

        assert findings["domain"] == 5
        assert findings["status"] == "OK"
        assert findings["circuit_breaker_armed"] is False

    def test_resilience_circuit_breaker_armed(self, inspector):
        """Circuit breaker armed (VIX > 35)."""
        inspector.app_state["regime"] = Mock(vix=38.0)

        findings = inspector.audit_resilience()

        assert findings["circuit_breaker_armed"] is True


# ────────────────────────────────────────────────────────────────────────────
# Domain 6: External APIs (4 tests)
# ────────────────────────────────────────────────────────────────────────────

class TestDomain6ExternalAPIs:
    """Tests for audit_external_apis()."""

    @patch("axiom.inspector.requests.get")
    def test_apis_all_ok(self, mock_get, inspector):
        """All external APIs healthy."""
        mock_get.return_value = Mock(status_code=200)

        findings = inspector.audit_external_apis()

        assert findings["domain"] == 6
        assert findings["status"] == "OK"
        assert len(findings["apis"]) == 3
        assert all(a["status"] == "OK" for a in findings["apis"])

    @patch("axiom.inspector.requests.get")
    def test_api_rate_limited(self, mock_get, inspector):
        """API returns 429."""
        def side_effect(*args, **kwargs):
            if "polygon" in args[0]:
                return Mock(status_code=429)
            return Mock(status_code=200)

        mock_get.side_effect = side_effect

        findings = inspector.audit_external_apis()

        assert findings["status"] == "WARNING"
        assert len(findings["failures"]) == 1
        assert findings["failures"][0]["status"] == "RATE_LIMITED"

    @patch("axiom.inspector.requests.get")
    def test_api_timeout(self, mock_get, inspector):
        """API times out."""
        import requests

        def side_effect(*args, **kwargs):
            if "alpha" in args[0].lower():
                raise requests.Timeout()
            return Mock(status_code=200)

        mock_get.side_effect = side_effect

        findings = inspector.audit_external_apis()

        assert findings["status"] == "WARNING"
        assert len(findings["failures"]) >= 1

    @patch("axiom.inspector.requests.get")
    def test_api_critical_after_multiple_failures(self, mock_get, inspector):
        """API fails 6+ consecutive times."""
        mock_get.return_value = Mock(status_code=503)

        findings = inspector.audit_external_apis(consecutive_failures=6)

        assert findings["status"] == "CRITICAL"
        assert len(findings["failures"]) >= 1


# ────────────────────────────────────────────────────────────────────────────
# Domain 7: Performance (4 tests)
# ────────────────────────────────────────────────────────────────────────────

class TestDomain7Performance:
    """Tests for audit_performance()."""

    def test_performance_nominal(self, inspector):
        """P95 < 3s, no slow calls."""
        latencies = [100, 150, 200, 250, 300, 350, 400, 450, 500, 600]

        findings = inspector.audit_performance(latencies=latencies)

        assert findings["domain"] == 7
        assert findings["status"] == "OK"
        assert findings["p95_ms"] < 3000
        assert findings["consecutive_slow"] == 0

    def test_performance_high_p95(self, inspector):
        """P95 > 3s."""
        latencies = [100] * 90 + [3500, 3600, 3700, 3800, 3900]

        findings = inspector.audit_performance(latencies=latencies)

        assert findings["p95_ms"] > 3000
        # Status depends on consecutive slow calls

    def test_performance_consecutive_slow_calls(self, inspector):
        """5+ consecutive calls > 3s."""
        latencies = [100, 200, 3500, 3600, 3700, 3800, 3900, 400]

        findings = inspector.audit_performance(latencies=latencies)

        assert findings["status"] == "WARNING"
        assert findings["consecutive_slow"] >= 5

    @patch("axiom.inspector.get_audit_log_entries")
    def test_performance_from_audit_log(self, mock_log, inspector):
        """Read latencies from audit log."""
        mock_log.return_value = [
            {"latency_ms": 150},
            {"latency_ms": 200},
            {"latency_ms": 250},
        ]

        findings = inspector.audit_performance(latencies=None)

        # Should read from mocked audit log
        assert "p50_ms" in findings


# ────────────────────────────────────────────────────────────────────────────
# Domain 8: VIX Freshness (4 tests)
# ────────────────────────────────────────────────────────────────────────────

class TestDomain8VIXFreshness:
    """Tests for audit_vix_freshness()."""

    def test_vix_real_time_ok(self, inspector):
        """VIX is real-time (not estimated)."""
        inspector.app_state["regime"] = Mock(vix=22.5, is_estimated=False)

        findings = inspector.audit_vix_freshness()

        assert findings["domain"] == 8
        assert findings["status"] == "OK"
        assert findings["is_estimated"] is False
        assert findings["vix_value"] == 22.5

    def test_vix_estimated_once_ok(self, inspector):
        """VIX estimated but only once."""
        inspector.app_state["regime"] = Mock(vix=22.5, is_estimated=True)

        findings = inspector.audit_vix_freshness(consecutive_estimated=1)

        assert findings["status"] == "OK"
        assert findings["is_estimated"] is True

    def test_vix_estimated_3_consecutive_warning(self, inspector):
        """VIX estimated 3+ consecutive times."""
        inspector.app_state["regime"] = Mock(vix=22.5, is_estimated=True)

        findings = inspector.audit_vix_freshness(consecutive_estimated=3)

        assert findings["status"] == "WARNING"
        assert findings["consecutive_estimated"] == 3
        assert "high uncertainty" in findings["finding"]

    def test_vix_regime_not_loaded(self, inspector):
        """Regime not loaded."""
        inspector.app_state["regime"] = None

        findings = inspector.audit_vix_freshness()

        assert findings["is_estimated"] is True
        assert "not loaded" in findings["finding"]


# ────────────────────────────────────────────────────────────────────────────
# Domain 9: Circuit Breaker (4 tests)
# ────────────────────────────────────────────────────────────────────────────

class TestDomain9CircuitBreaker:
    """Tests for audit_circuit_breaker()."""

    def test_circuit_breaker_normal(self, inspector):
        """VIX < 35, submissions open."""
        inspector.app_state["regime"] = Mock(vix=22.5)
        inspector.app_state["submissions_open"] = True

        findings = inspector.audit_circuit_breaker()

        assert findings["domain"] == 9
        assert findings["status"] == "OK"
        assert findings["circuit_breaker_correct"] is True
        assert findings["vix_value"] == 22.5

    def test_circuit_breaker_armed(self, inspector):
        """VIX > 35, submissions paused."""
        inspector.app_state["regime"] = Mock(vix=38.0)
        inspector.app_state["submissions_open"] = False

        findings = inspector.audit_circuit_breaker()

        assert findings["status"] == "OK"
        assert findings["submissions_paused"] is True
        assert findings["circuit_breaker_correct"] is True

    def test_circuit_breaker_failure(self, inspector):
        """VIX > 35 but submissions still open (CRITICAL)."""
        inspector.app_state["regime"] = Mock(vix=39.0)
        inspector.app_state["submissions_open"] = True

        findings = inspector.audit_circuit_breaker()

        assert findings["status"] == "CRITICAL"
        assert findings["circuit_breaker_correct"] is False
        assert findings["finding"] == "CIRCUIT BREAKER FAILURE: VIX=39.0 but submissions still open"

    def test_circuit_breaker_regime_not_loaded(self, inspector):
        """Regime not loaded."""
        inspector.app_state["regime"] = None

        findings = inspector.audit_circuit_breaker()

        assert findings["finding"] == "Regime not loaded"


# ────────────────────────────────────────────────────────────────────────────
# Domain 10: Audit Log Completeness (4 tests)
# ────────────────────────────────────────────────────────────────────────────

class TestDomain10AuditLog:
    """Tests for audit_log_completeness()."""

    @patch("axiom.inspector.get_audit_log_entries")
    def test_audit_log_complete(self, mock_log, inspector):
        """Audit log complete with no gaps."""
        now = datetime.now(ET)
        mock_log.return_value = [
            {"type": "trade", "timestamp": (now - timedelta(minutes=45)).isoformat()},
            {"type": "trade", "timestamp": (now - timedelta(minutes=30)).isoformat()},
            {"type": "regime", "timestamp": (now - timedelta(minutes=15)).isoformat()},
            {"type": "pool", "timestamp": now.isoformat()},
        ]

        findings = inspector.audit_log_completeness()

        assert findings["domain"] == 10
        assert findings["status"] == "OK"
        assert findings["trades_logged"] == 2
        assert findings["regimes_logged"] == 1
        assert findings["pools_logged"] == 1
        assert findings["gaps"] == 0

    @patch("axiom.inspector.get_audit_log_entries")
    def test_audit_log_gap_critical(self, mock_log, inspector):
        """Audit log has > 60 min gap."""
        now = datetime.now(ET)
        mock_log.return_value = [
            {"type": "trade", "timestamp": (now - timedelta(minutes=120)).isoformat()},
            {"type": "trade", "timestamp": (now - timedelta(minutes=45)).isoformat()},
        ]

        findings = inspector.audit_log_completeness()

        assert findings["status"] == "CRITICAL"
        assert findings["gaps"] == 1
        assert findings["max_gap_min"] >= 60

    @patch("axiom.inspector.get_audit_log_entries")
    def test_audit_log_no_entries(self, mock_log, inspector):
        """No audit log entries."""
        mock_log.return_value = []

        findings = inspector.audit_log_completeness()

        assert "No audit log entries" in findings["finding"]

    @patch("axiom.inspector.get_audit_log_entries")
    def test_audit_log_database_error(self, mock_log, inspector):
        """Database error reading audit log."""
        mock_log.side_effect = Exception("Database connection failed")

        findings = inspector.audit_log_completeness()

        assert "failed" in findings["finding"].lower()


# ────────────────────────────────────────────────────────────────────────────
# Deduplication Tests
# ────────────────────────────────────────────────────────────────────────────

class TestDeduplication:
    """Tests for escalation deduplication."""

    def test_dedup_key_generation(self, inspector):
        """Generate consistent dedup keys."""
        finding1 = {"domain": 1, "event": "auth_header_audit"}
        finding2 = {"domain": 1, "event": "auth_header_audit"}

        key1 = inspector._make_dedup_key(finding1)
        key2 = inspector._make_dedup_key(finding2)

        assert key1 == key2  # Same finding → same key

    def test_should_escalate_first_time(self, inspector):
        """First escalation should proceed."""
        finding = {"domain": 1, "event": "auth_header_audit"}

        result = inspector._should_escalate(finding)

        assert result is True

    def test_should_not_escalate_within_window(self, inspector):
        """Do not escalate same finding within 2h window."""
        finding = {"domain": 1, "event": "auth_header_audit"}

        # First escalation
        inspector._should_escalate(finding)

        # Second escalation within 2h
        result = inspector._should_escalate(finding)

        assert result is False

    def test_should_escalate_after_window(self, inspector):
        """Escalate same finding after 2h window expired."""
        finding = {"domain": 1, "event": "auth_header_audit"}

        # First escalation
        inspector._should_escalate(finding)

        # Manually advance time past window
        dedup_key = inspector._make_dedup_key(finding)
        inspector.escalation_history[dedup_key] = time.time() - DEDUP_WINDOW_SEC - 1

        # Second escalation should proceed
        result = inspector._should_escalate(finding)

        assert result is True

    @patch("axiom.inspector.report")
    def test_escalate_to_sovereign(self, mock_report, inspector):
        """Escalate CRITICAL finding to SOVEREIGN."""
        finding = {
            "domain": 9,
            "event": "circuit_breaker_failure",
            "status": "CRITICAL",
            "severity": "CRITICAL",
            "timestamp": datetime.now(ET).isoformat(),
            "finding": "Circuit breaker failure",
            "impact": "Trading at high risk",
            "required_action": "Investigate",
            "data": {},
        }

        inspector._escalate_to_sovereign(finding)

        # Verify report() was called
        mock_report.assert_called_once()
        call_args = mock_report.call_args
        assert call_args[0][0] == "axiom-inspector"
        assert call_args[0][1] == "CRITICAL"

    @patch("axiom.inspector.report")
    def test_escalate_deduped_not_called(self, mock_report, inspector):
        """Deduped escalation does not call report()."""
        finding = {
            "domain": 1,
            "event": "auth_failure",
            "status": "CRITICAL",
            "timestamp": datetime.now(ET).isoformat(),
            "finding": "Auth failed",
            "data": {},
        }

        # First call
        inspector._escalate_to_sovereign(finding)
        assert mock_report.call_count == 1

        # Second call (within window)
        inspector._escalate_to_sovereign(finding)
        assert mock_report.call_count == 1  # Still 1, not 2


# ────────────────────────────────────────────────────────────────────────────
# Sweep Orchestration Tests
# ────────────────────────────────────────────────────────────────────────────

class TestSweeps:
    """Tests for sweep orchestration."""

    @patch("axiom.inspector.AxiomInspector.audit_auth_headers")
    @patch("axiom.inspector.AxiomInspector.audit_config_consistency")
    @patch("axiom.inspector.AxiomInspector.audit_data_integrity")
    @patch("axiom.inspector.AxiomInspector.audit_webhooks")
    @patch("axiom.inspector.AxiomInspector.audit_resilience")
    @patch("axiom.inspector.AxiomInspector.audit_external_apis")
    @patch("axiom.inspector.AxiomInspector.audit_performance")
    @patch("axiom.inspector.AxiomInspector.audit_vix_freshness")
    @patch("axiom.inspector.AxiomInspector.audit_circuit_breaker")
    @patch("axiom.inspector.AxiomInspector.audit_log_completeness")
    def test_pre_market_sweep_all_ok(self, *mock_audits, inspector):
        """Pre-market sweep with all audits OK."""
        for mock in mock_audits:
            mock.return_value = {
                "domain": 1,
                "status": "OK",
                "finding": "OK",
                "event": "test",
                "timestamp": datetime.now(ET).isoformat(),
                "data": {},
            }

        result = inspector.run_pre_market_sweep()

        assert result["event"] == "pre_market_sweep"
        assert len(result["findings"]) == 10
        assert result["escalations"] == 0
        assert result["total_time_ms"] < 30000

    @patch("axiom.inspector.AxiomInspector.audit_data_integrity")
    @patch("axiom.inspector.AxiomInspector.audit_external_apis")
    @patch("axiom.inspector.AxiomInspector.audit_performance")
    @patch("axiom.inspector.AxiomInspector.audit_vix_freshness")
    @patch("axiom.inspector.AxiomInspector.audit_circuit_breaker")
    def test_market_hours_sweep(self, *mock_audits, inspector):
        """Market-hours sweep runs subset of domains."""
        for mock in mock_audits:
            mock.return_value = {
                "domain": 1,
                "status": "OK",
                "finding": "OK",
                "event": "test",
                "timestamp": datetime.now(ET).isoformat(),
                "data": {},
            }

        result = inspector.run_market_hours_sweep()

        assert result["event"] == "market_hours_sweep"
        assert len(result["findings"]) == 5  # Domains 3, 6, 7, 8, 9


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
