"""
test_inspector_integration.py — Integration tests for AxiomInspector

3 integration tests:
1. Full sweep (all 10 audits run, none crash)
2. Escalation routing (CRITICAL → SOVEREIGN via report())
3. Deduplication (same finding not sent twice in 2h window)

Run: pytest tests/test_inspector_integration.py -v
"""

import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, call
from typing import Dict, Any

import pytest
import pytz

from axiom.inspector import AxiomInspector, DEDUP_WINDOW_SEC

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
    """Mock FastAPI app_state dict with realistic data."""
    return {
        "pool": ["NVDA", "MSFT", "AAPL", "GOOGL", "AMZN"],
        "regime": Mock(
            vix=22.5,
            classification="NORMAL",
            is_estimated=False,
            composite_score=45,
        ),
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
# Integration Test 1: Full Sweep Completeness
# ────────────────────────────────────────────────────────────────────────────

class TestFullSweepCompleteness:
    """Verify all 10 audits complete without crashing."""

    @patch("axiom.inspector.requests.get")
    @patch("axiom.inspector.requests.post")
    @patch("axiom.inspector.get_audit_log_entries")
    @patch("axiom.inspector.report")
    def test_full_pre_market_sweep_completes(
        self,
        mock_report,
        mock_audit_log,
        mock_post,
        mock_get,
        inspector,
    ):
        """
        Run full pre-market sweep (all 10 audits).
        Verify:
        - All 10 findings returned
        - No exceptions raised
        - Completes in < 30s
        - Each finding has required keys
        """
        # Mock all HTTP requests
        mock_get.return_value = Mock(status_code=200)
        mock_post.return_value = Mock(status_code=200)
        mock_audit_log.return_value = [
            {"type": "trade", "timestamp": datetime.now(ET).isoformat()},
        ]

        start = time.time()
        result = inspector.run_pre_market_sweep()
        elapsed_s = time.time() - start

        # Assertions
        assert isinstance(result, dict), "Result must be dict"
        assert result["event"] == "pre_market_sweep"
        assert "timestamp" in result
        assert "findings" in result
        assert "escalations" in result
        assert "total_time_ms" in result

        # Verify 10 findings
        assert len(result["findings"]) == 10, f"Expected 10 findings, got {len(result['findings'])}"

        # Verify each finding has required keys
        for finding in result["findings"]:
            assert "domain" in finding, f"Finding missing 'domain': {finding}"
            assert "event" in finding, f"Finding missing 'event': {finding}"
            assert "status" in finding, f"Finding missing 'status': {finding}"
            assert "timestamp" in finding, f"Finding missing 'timestamp': {finding}"
            assert "finding" in finding, f"Finding missing 'finding': {finding}"
            assert "data" in finding, f"Finding missing 'data': {finding}"

            # Verify domain is 1–10
            assert 1 <= finding["domain"] <= 10, f"Invalid domain: {finding['domain']}"

            # Verify status is one of expected values
            assert finding["status"] in ["OK", "WARNING", "CRITICAL"], \
                f"Invalid status: {finding['status']}"

        # Verify completion time < 30s
        assert elapsed_s < 30, f"Sweep took {elapsed_s:.2f}s, expected < 30s"

        # Verify domains are in order (sorted)
        domains = [f["domain"] for f in result["findings"]]
        assert domains == sorted(domains), f"Domains not sorted: {domains}"

    @patch("axiom.inspector.requests.get")
    @patch("axiom.inspector.requests.post")
    @patch("axiom.inspector.get_audit_log_entries")
    @patch("axiom.inspector.report")
    def test_pre_market_sweep_partial_failure(
        self,
        mock_report,
        mock_audit_log,
        mock_post,
        mock_get,
        inspector,
    ):
        """
        Verify graceful degradation when some audits fail.
        If one audit times out, others still complete.
        """
        import requests

        call_count = [0]

        def side_effect_get(*args, **kwargs):
            call_count[0] += 1
            # First 5 calls succeed, 6th times out
            if call_count[0] > 5:
                raise requests.Timeout()
            return Mock(status_code=200)

        mock_get.side_effect = side_effect_get
        mock_post.return_value = Mock(status_code=200)
        mock_audit_log.return_value = []

        result = inspector.run_pre_market_sweep()

        # Should still get all 10 findings (or at least most)
        assert len(result["findings"]) >= 8, \
            f"Expected at least 8 findings despite partial failure, got {len(result['findings'])}"


# ────────────────────────────────────────────────────────────────────────────
# Integration Test 2: Escalation Routing
# ────────────────────────────────────────────────────────────────────────────

class TestEscalationRouting:
    """Verify CRITICAL findings route to SOVEREIGN correctly."""

    @patch("axiom.inspector.requests.get")
    @patch("axiom.inspector.requests.post")
    @patch("axiom.inspector.get_audit_log_entries")
    @patch("axiom.inspector.report")
    def test_critical_findings_escalated_to_sovereign(
        self,
        mock_report,
        mock_audit_log,
        mock_post,
        mock_get,
        inspector,
    ):
        """
        Verify:
        - CRITICAL findings are escalated via report()
        - report() called with correct arguments (agent, level, payload)
        - Escalation happens during sweep
        """
        # Mock: pool is empty (triggers CRITICAL in Domain 3)
        inspector.app_state["pool"] = []
        inspector.app_state["regime"] = Mock(vix=22.5)

        mock_get.return_value = Mock(status_code=200)
        mock_post.return_value = Mock(status_code=200)
        mock_audit_log.return_value = []

        result = inspector.run_pre_market_sweep()

        # Find Domain 3 finding (should be CRITICAL)
        domain3_finding = next((f for f in result["findings"] if f["domain"] == 3), None)
        assert domain3_finding is not None, "Domain 3 finding missing"
        assert domain3_finding["status"] == "CRITICAL", \
            f"Domain 3 should be CRITICAL (pool empty), got {domain3_finding['status']}"

        # Verify report() was called for the CRITICAL finding
        assert mock_report.called, "report() should be called for CRITICAL findings"

        # Verify report() was called with correct arguments
        call_args = mock_report.call_args
        assert call_args[0][0] == "axiom-inspector", \
            f"First arg should be 'axiom-inspector', got {call_args[0][0]}"
        assert call_args[0][1] == "CRITICAL", \
            f"Second arg (level) should be 'CRITICAL', got {call_args[0][1]}"

        payload = call_args[0][2]
        assert isinstance(payload, dict), "Third arg should be dict"
        assert payload.get("domain") == 3, "Payload should have domain=3"
        assert payload.get("event") == "data_integrity_audit", \
            "Payload should have correct event"

    @patch("axiom.inspector.requests.get")
    @patch("axiom.inspector.requests.post")
    @patch("axiom.inspector.get_audit_log_entries")
    @patch("axiom.inspector.report")
    def test_warning_findings_not_escalated_immediately(
        self,
        mock_report,
        mock_audit_log,
        mock_post,
        mock_get,
        inspector,
    ):
        """
        Verify:
        - WARNING findings do NOT escalate (only CRITICAL)
        - report() not called for WARNING
        """
        # Mock: webhook timeout (triggers WARNING in Domain 4)
        import requests

        def side_effect_post(*args, **kwargs):
            raise requests.Timeout()

        mock_get.return_value = Mock(status_code=200)
        mock_post.side_effect = side_effect_post
        mock_audit_log.return_value = []

        # Reset report mock
        mock_report.reset_mock()

        result = inspector.run_pre_market_sweep()

        # Find Domain 4 finding (should be WARNING)
        domain4_finding = next((f for f in result["findings"] if f["domain"] == 4), None)
        assert domain4_finding is not None, "Domain 4 finding missing"
        assert domain4_finding["status"] == "WARNING", \
            f"Domain 4 should be WARNING, got {domain4_finding['status']}"

        # report() should not be called for WARNING (only CRITICAL)
        # Count calls to report(); subtract any from other CRITICALs
        critical_calls = sum(1 for call_obj in mock_report.call_args_list
                            if len(call_obj[0]) > 1 and call_obj[0][1] == "CRITICAL")
        assert critical_calls == 0, \
            f"No CRITICAL findings, but report() called {critical_calls} times with CRITICAL level"

    @patch("axiom.inspector.requests.get")
    @patch("axiom.inspector.requests.post")
    @patch("axiom.inspector.get_audit_log_entries")
    @patch("axiom.inspector.report")
    def test_multiple_critical_findings_escalated(
        self,
        mock_report,
        mock_audit_log,
        mock_post,
        mock_get,
        inspector,
    ):
        """
        Verify:
        - Multiple CRITICAL findings are all escalated
        - Each gets its own report() call
        """
        # Mock: multiple CRITICALs
        inspector.app_state["pool"] = []  # CRITICAL in Domain 3
        inspector.app_state["regime"].vix = 39.0  # Will trigger Domain 9
        inspector.app_state["submissions_open"] = True  # CRITICAL in Domain 9

        mock_get.return_value = Mock(status_code=200)
        mock_post.return_value = Mock(status_code=200)
        mock_audit_log.return_value = []

        result = inspector.run_pre_market_sweep()

        # Count CRITICAL findings
        critical_findings = [f for f in result["findings"] if f["status"] == "CRITICAL"]
        assert len(critical_findings) >= 2, \
            f"Expected at least 2 CRITICAL findings, got {len(critical_findings)}"

        # Count CRITICAL escalations
        critical_escalations = [
            call_obj for call_obj in mock_report.call_args_list
            if len(call_obj[0]) > 1 and call_obj[0][1] == "CRITICAL"
        ]
        assert len(critical_escalations) >= 2, \
            f"Expected at least 2 CRITICAL escalations, got {len(critical_escalations)}"


# ────────────────────────────────────────────────────────────────────────────
# Integration Test 3: Deduplication Window
# ────────────────────────────────────────────────────────────────────────────

class TestDeduplicationWindow:
    """Verify same escalation not sent twice in < 2h window."""

    @patch("axiom.inspector.requests.get")
    @patch("axiom.inspector.requests.post")
    @patch("axiom.inspector.get_audit_log_entries")
    @patch("axiom.inspector.report")
    def test_deduplication_same_critical_within_window(
        self,
        mock_report,
        mock_audit_log,
        mock_post,
        mock_get,
        inspector,
    ):
        """
        Verify:
        - First sweep escalates a CRITICAL finding
        - Immediate second sweep with same CRITICAL does NOT escalate
        - Deduplication works within 2h window
        """
        # Setup: pool is empty → CRITICAL
        inspector.app_state["pool"] = []
        inspector.app_state["regime"] = Mock(vix=22.5)

        mock_get.return_value = Mock(status_code=200)
        mock_post.return_value = Mock(status_code=200)
        mock_audit_log.return_value = []

        # First sweep
        result1 = inspector.run_pre_market_sweep()
        critical_count_1 = sum(1 for f in result1["findings"] if f["status"] == "CRITICAL")
        report_call_count_1 = mock_report.call_count

        # Second sweep (immediately after, within 2h window)
        result2 = inspector.run_pre_market_sweep()
        critical_count_2 = sum(1 for f in result2["findings"] if f["status"] == "CRITICAL")
        report_call_count_2 = mock_report.call_count

        # Verify same CRITICAL findings detected both times
        assert critical_count_1 > 0, "First sweep should have CRITICAL findings"
        assert critical_count_2 > 0, "Second sweep should have CRITICAL findings"

        # Verify report() called more times in second sweep is LESS due to dedup
        # (Each CRITICAL escalation triggers report(); if deduped, report_call_count should not increase)
        additional_calls = report_call_count_2 - report_call_count_1
        assert additional_calls == 0, \
            f"Dedup should prevent 2nd escalation; expected 0 additional report() calls, got {additional_calls}"

    @patch("axiom.inspector.requests.get")
    @patch("axiom.inspector.requests.post")
    @patch("axiom.inspector.get_audit_log_entries")
    @patch("axiom.inspector.report")
    def test_escalation_after_dedup_window_expires(
        self,
        mock_report,
        mock_audit_log,
        mock_post,
        mock_get,
        inspector,
    ):
        """
        Verify:
        - After 2h window expires, same CRITICAL is escalated again
        """
        # Setup: pool is empty → CRITICAL
        inspector.app_state["pool"] = []
        inspector.app_state["regime"] = Mock(vix=22.5)

        mock_get.return_value = Mock(status_code=200)
        mock_post.return_value = Mock(status_code=200)
        mock_audit_log.return_value = []

        # First sweep
        result1 = inspector.run_pre_market_sweep()
        report_call_count_1 = mock_report.call_count

        # Manually advance dedup window (2+ hours)
        dedup_key = inspector._make_dedup_key({
            "domain": 3,
            "event": "data_integrity_audit",
        })
        inspector.escalation_history[dedup_key] = time.time() - DEDUP_WINDOW_SEC - 1

        # Second sweep (after window expiry)
        result2 = inspector.run_pre_market_sweep()
        report_call_count_2 = mock_report.call_count

        # Verify report() called again for re-escalation
        additional_calls = report_call_count_2 - report_call_count_1
        assert additional_calls > 0, \
            f"After window expiry, same CRITICAL should escalate again; " \
            f"expected > 0 additional report() calls, got {additional_calls}"

    @patch("axiom.inspector.requests.get")
    @patch("axiom.inspector.requests.post")
    @patch("axiom.inspector.get_audit_log_entries")
    @patch("axiom.inspector.report")
    def test_different_critical_always_escalated(
        self,
        mock_report,
        mock_audit_log,
        mock_post,
        mock_get,
        inspector,
    ):
        """
        Verify:
        - Different CRITICAL findings always escalate (no cross-domain dedup)
        - Domain 3 CRITICAL doesn't suppress Domain 9 CRITICAL
        """
        # Setup: Domain 3 CRITICAL (pool empty)
        inspector.app_state["pool"] = []
        inspector.app_state["regime"] = Mock(vix=22.5)
        inspector.app_state["submissions_open"] = True

        mock_get.return_value = Mock(status_code=200)
        mock_post.return_value = Mock(status_code=200)
        mock_audit_log.return_value = []

        # First sweep with Domain 3 CRITICAL
        result1 = inspector.run_pre_market_sweep()
        domain3_critical = any(f["domain"] == 3 and f["status"] == "CRITICAL"
                              for f in result1["findings"])
        assert domain3_critical, "Domain 3 should be CRITICAL"

        report_call_count_1 = mock_report.call_count

        # Change state: fix Domain 3, trigger Domain 9 CRITICAL
        inspector.app_state["pool"] = ["NVDA"]
        inspector.app_state["regime"].vix = 39.0  # VIX > 35, submissions still open

        # Second sweep with Domain 9 CRITICAL
        result2 = inspector.run_pre_market_sweep()
        domain9_critical = any(f["domain"] == 9 and f["status"] == "CRITICAL"
                              for f in result2["findings"])
        assert domain9_critical, "Domain 9 should be CRITICAL"

        report_call_count_2 = mock_report.call_count

        # Domain 9 CRITICAL should escalate even though Domain 3 was deduped
        additional_calls = report_call_count_2 - report_call_count_1
        assert additional_calls > 0, \
            f"Domain 9 CRITICAL should escalate independently; " \
            f"expected > 0 additional report() calls, got {additional_calls}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
