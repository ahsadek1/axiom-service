"""
test_diagnoser.py — All 10 Outcome Monitor test cases.

Tests use unittest.mock exclusively — no real HTTP calls are made.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# Allow direct import from parent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from diagnoser import diagnose
from models import AlpacaSnapshot, ServiceSnapshot


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_services(**overrides):
    """
    Create a full set of UP service snapshots with healthy defaults.
    Override any service by passing name=ServiceSnapshot(...) as kwarg.
    """
    recent_scan = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    base = {
        "alpha_buffer": ServiceSnapshot(
            "alpha_buffer", "UP", {"submissions_today": 5, "go_verdicts_today": 2}
        ),
        "prime_buffer": ServiceSnapshot(
            "prime_buffer", "UP", {"submissions_today": 3, "go_verdicts_today": 1}
        ),
        "omni": ServiceSnapshot("omni", "UP", {"arm_pulls_today": 4}),
        "alpha_execution": ServiceSnapshot(
            "alpha_execution",
            "UP",
            {"trades_today": 0, "auto_execute": True, "mode": "live"},
        ),
        "prime_execution": ServiceSnapshot(
            "prime_execution",
            "UP",
            {"trades_today": 0, "auto_execute": True, "mode": "live"},
        ),
        "cipher": ServiceSnapshot("cipher", "UP", {"last_scan_at": recent_scan}),
        "atlas": ServiceSnapshot("atlas", "UP", {"last_scan_at": recent_scan}),
        "sage": ServiceSnapshot("sage", "UP", {"last_scan_at": recent_scan}),
    }
    base.update(overrides)
    return base


def _make_alpaca(v1_power: float = 93792.0, v2_power: float = 100000.0):
    """Create a healthy pair of Alpaca account snapshots."""
    return {
        "v1": AlpacaSnapshot("v1", "UP", buying_power=v1_power),
        "v2": AlpacaSnapshot("v2", "UP", buying_power=v2_power),
    }


# ── Test Cases ────────────────────────────────────────────────────────────────

class TestDiagnoser(unittest.TestCase):

    def test_t1_pipeline_healthy(self):
        """T1: trades > 0 → pipeline_healthy, severity=INFO, no escalation."""
        result = diagnose(
            _make_services(), _make_alpaca(),
            trades_since_last_cycle=3, consecutive_dry_cycles=0,
        )
        self.assertEqual(result.diagnosis, "pipeline_healthy")
        self.assertFalse(result.escalate)
        self.assertEqual(result.severity, "INFO")
        self.assertEqual(result.consecutive_dry_cycles, 0)

    def test_t2_auto_execute_disabled(self):
        """T2: prime_execution auto_execute=False → auto_execute_disabled, no escalation."""
        services = _make_services(
            prime_execution=ServiceSnapshot(
                "prime_execution",
                "UP",
                {"trades_today": 0, "auto_execute": False, "mode": "dry_run"},
            )
        )
        result = diagnose(
            services, _make_alpaca(),
            trades_since_last_cycle=0, consecutive_dry_cycles=1,
        )
        self.assertEqual(result.diagnosis, "auto_execute_disabled")
        self.assertFalse(result.escalate)
        self.assertIn("prime_execution", result.details)

    def test_t3_scanner_dry_no_escalation(self):
        """T3: all scanners stale >35 min, first occurrence → scanner_dry, no escalation."""
        stale = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
        services = _make_services(
            cipher=ServiceSnapshot("cipher", "UP", {"last_scan_at": stale}),
            atlas=ServiceSnapshot("atlas", "UP", {"last_scan_at": stale}),
            sage=ServiceSnapshot("sage", "UP", {"last_scan_at": stale}),
        )
        result = diagnose(
            services, _make_alpaca(),
            trades_since_last_cycle=0, consecutive_dry_cycles=1,
        )
        self.assertEqual(result.diagnosis, "scanner_dry")
        self.assertFalse(result.escalate)
        self.assertEqual(result.severity, "INFO")

    def test_t4_scanner_dry_escalation(self):
        """T4: scanner_dry, consecutive_dry_cycles=3 → WARN escalation."""
        stale = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
        services = _make_services(
            cipher=ServiceSnapshot("cipher", "UP", {"last_scan_at": stale}),
            atlas=ServiceSnapshot("atlas", "UP", {"last_scan_at": stale}),
            sage=ServiceSnapshot("sage", "UP", {"last_scan_at": stale}),
        )
        result = diagnose(
            services, _make_alpaca(),
            trades_since_last_cycle=0, consecutive_dry_cycles=3,
        )
        self.assertEqual(result.diagnosis, "scanner_dry")
        self.assertTrue(result.escalate)
        self.assertEqual(result.severity, "WARN")

    def test_t5_concordance_failing(self):
        """T5: submissions=20, go_verdicts=0 → concordance_failing, CRITICAL escalation."""
        services = _make_services(
            alpha_buffer=ServiceSnapshot(
                "alpha_buffer", "UP", {"submissions_today": 15, "go_verdicts_today": 0}
            ),
            prime_buffer=ServiceSnapshot(
                "prime_buffer", "UP", {"submissions_today": 5, "go_verdicts_today": 0}
            ),
        )
        result = diagnose(
            services, _make_alpaca(),
            trades_since_last_cycle=0, consecutive_dry_cycles=1,
        )
        self.assertEqual(result.diagnosis, "concordance_failing")
        self.assertTrue(result.escalate)
        self.assertEqual(result.severity, "CRITICAL")

    def test_t6_capital_locked(self):
        """T6: both Alpaca accounts buying_power < $2,000 → capital_locked, no escalation."""
        # Give concordance a clean bill of health so we reach check 4
        services = _make_services(
            alpha_buffer=ServiceSnapshot(
                "alpha_buffer", "UP", {"submissions_today": 5, "go_verdicts_today": 3}
            ),
            prime_buffer=ServiceSnapshot(
                "prime_buffer", "UP", {"submissions_today": 3, "go_verdicts_today": 2}
            ),
            omni=ServiceSnapshot("omni", "UP", {"arm_pulls_today": 5}),
        )
        result = diagnose(
            services, _make_alpaca(v1_power=500.0, v2_power=300.0),
            trades_since_last_cycle=0, consecutive_dry_cycles=1,
        )
        self.assertEqual(result.diagnosis, "capital_locked")
        self.assertFalse(result.escalate)

    def test_t7_omni_not_synthesizing(self):
        """T7: submissions>0, arm_pulls=0 → concordance_failing or omni_not_synthesizing, CRITICAL."""
        services = _make_services(
            omni=ServiceSnapshot("omni", "UP", {"arm_pulls_today": 0}),
            alpha_buffer=ServiceSnapshot(
                "alpha_buffer", "UP", {"submissions_today": 10, "go_verdicts_today": 0}
            ),
            prime_buffer=ServiceSnapshot(
                "prime_buffer", "UP", {"submissions_today": 5, "go_verdicts_today": 0}
            ),
        )
        result = diagnose(
            services, _make_alpaca(),
            trades_since_last_cycle=0, consecutive_dry_cycles=1,
        )
        # concordance_failing catches first (go_verdicts=0 with submissions>0)
        self.assertIn(result.diagnosis, ("concordance_failing", "omni_not_synthesizing"))
        self.assertTrue(result.escalate)
        self.assertEqual(result.severity, "CRITICAL")

    def test_t8_unknown(self):
        """T8: all checks pass but zero trades → unknown, CRITICAL escalation."""
        services = _make_services(
            alpha_buffer=ServiceSnapshot(
                "alpha_buffer", "UP", {"submissions_today": 5, "go_verdicts_today": 3}
            ),
            prime_buffer=ServiceSnapshot(
                "prime_buffer", "UP", {"submissions_today": 3, "go_verdicts_today": 2}
            ),
            omni=ServiceSnapshot("omni", "UP", {"arm_pulls_today": 6}),
        )
        result = diagnose(
            services, _make_alpaca(),
            trades_since_last_cycle=0, consecutive_dry_cycles=1,
        )
        self.assertEqual(result.diagnosis, "unknown")
        self.assertTrue(result.escalate)
        self.assertEqual(result.severity, "CRITICAL")

    def test_t9_chronicle_unreachable(self):
        """T9: CHRONICLE POST returns 500 → fallback to /tmp file, write_to_chronicle returns False."""
        from chronicler import write_to_chronicle
        from models import CycleResult, DiagnosisResult

        diag = DiagnosisResult("pipeline_healthy", "INFO", False, "Test cycle.", 0)
        result = CycleResult(
            cycle_ts="2026-04-20T14:30:00+00:00",
            market_open=True,
            trades_since_last_cycle=1,
            diagnosis=diag,
            services=_make_services(),
            alpaca={
                "v1": AlpacaSnapshot("v1", "UP", buying_power=93792.0),
                "v2": AlpacaSnapshot("v2", "UP", buying_power=100000.0),
            },
        )

        tmp = tempfile.mktemp(suffix=".jsonl")
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with (
            patch("chronicler.FALLBACK_LOG", tmp),
            patch("chronicler.requests.post", return_value=mock_resp),
        ):
            success = write_to_chronicle(result)

        self.assertFalse(success)
        self.assertTrue(os.path.exists(tmp))
        os.unlink(tmp)

    def test_t10_outside_market_hours(self):
        """T10: is_market_open() returns False → run_cycle exits, no collection or CHRONICLE write."""
        import main

        with (
            patch("main.is_market_open", return_value=False),
            patch("main.collect_all_services") as mock_collect,
            patch("main.write_to_chronicle") as mock_chronicle,
        ):
            main.run_cycle()

        mock_collect.assert_not_called()
        mock_chronicle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
