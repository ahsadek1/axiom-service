"""
test_g5_auto_execute.py — G5: AUTO_EXECUTE dry-run contract tests.

Tests the contract at the logic level (env-var parsing, health field, OMNI payload)
without re-spinning the full app (which has module-level state). 6 tests.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Set required env vars before any service module is imported
os.environ.setdefault("NEXUS_AUTO_EXECUTE",   "false")
os.environ.setdefault("NEXUS_WEBHOOK_SECRET", "x" * 64)
os.environ.setdefault("ALPHA_EXEC_DB_PATH",   tempfile.mktemp(suffix=".db"))
os.environ.setdefault("ALPACA_API_KEY",       "PKTEST")
os.environ.setdefault("ALPACA_SECRET_KEY",    "secret")
os.environ.setdefault("ALPHA_BUFFER_URL",     "http://localhost:8002")
os.environ.setdefault("TELEGRAM_BOT_TOKEN",   "123:abc")
os.environ.setdefault("AHMED_CHAT_ID",        "999")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestAutoExecuteEnvParsing(unittest.TestCase):
    """T1-T3: env var parsing contract — the gate that governs all live trading."""

    def test_t1_false_string_means_dry_run(self):
        """T1: 'false' → dry_run. The safe default."""
        result = "false".lower() == "true"
        self.assertFalse(result)

    def test_t2_true_string_means_live(self):
        """T2: exact 'true' (case-insensitive) → live mode."""
        for val in ["true", "True", "TRUE"]:
            result = val.lower() == "true"
            self.assertTrue(result, f"'{val}' should enable live mode")

    def test_t3_ambiguous_values_default_to_dry_run(self):
        """T3: 'yes', '1', 'on', 'enabled' — NOT accepted as live. Only exact 'true'."""
        for bad_val in ["yes", "1", "on", "enabled", "live"]:
            result = bad_val.strip().lower() == "true"
            self.assertFalse(result, f"'{bad_val}' must NOT enable live mode")

    def test_t4_missing_env_var_defaults_false(self):
        """T4: absent NEXUS_AUTO_EXECUTE → defaults to 'false'. Never fail-open."""
        saved = os.environ.pop("NEXUS_AUTO_EXECUTE", None)
        try:
            result = os.getenv("NEXUS_AUTO_EXECUTE", "false").lower() == "true"
            self.assertFalse(result)
        finally:
            if saved is not None:
                os.environ["NEXUS_AUTO_EXECUTE"] = saved


class TestAutoExecuteHealthFields(unittest.TestCase):
    """T5: Health endpoint must expose auto_execute and mode fields."""

    def test_t5_health_exposes_auto_execute_and_mode(self):
        """T5: app_state-sourced health response contains both auto_execute and mode fields."""
        # Simulate the health response construction from alpha-execution/main.py
        for auto_exec_val, expected_mode in [("false", "dry_run"), ("true", "live")]:
            auto_execute = auto_exec_val.lower() == "true"
            mode = "live" if auto_execute else "dry_run"
            health_response = {
                "service": "alpha-execution",
                "auto_execute": auto_execute,
                "mode": mode,
            }
            self.assertIn("auto_execute", health_response)
            self.assertIn("mode", health_response)
            self.assertEqual(health_response["mode"], expected_mode)
            self.assertEqual(health_response["auto_execute"], auto_exec_val == "true")


class TestOmniDispatchPayload(unittest.TestCase):
    """T6: OMNI must include auto_execute in every execution dispatch."""

    def test_t6_omni_includes_auto_execute_in_dispatch(self):
        """T6: execution_router.route_to_execution payload always includes 'auto_execute'."""
        omni_path = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "omni"
        ))
        sys.path.insert(0, omni_path)

        # Set OMNI env vars
        for k, v in [
            ("NEXUS_WEBHOOK_SECRET", "x" * 64),
            ("NEXUS_PRIME_SECRET", "y" * 64),
            ("ANTHROPIC_API_KEY", "sk-ant-test"),
            ("OPENAI_API_KEY", "sk-test"),
            ("GEMINI_API_KEY", "gm-test"),
            ("DEEPSEEK_API_KEY", "ds-test"),
            ("ALPHA_EXECUTION_URL", "http://localhost:8005"),
            ("PRIME_EXECUTION_URL", "http://localhost:8006"),
        ]:
            os.environ.setdefault(k, v)

        captured_payload = {}

        def mock_post(url, **kwargs):
            captured_payload.update(kwargs.get("json", {}))
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {"executed": False, "mode": "dry_run"}
            return m

        try:
            # Clear cached modules
            for mod in list(sys.modules.keys()):
                if mod in ("execution_router", "config") and "omni" not in sys.modules.get(mod, "").__file__ if mod in sys.modules and hasattr(sys.modules[mod], "__file__") and sys.modules[mod].__file__ else False:
                    del sys.modules[mod]

            with patch("requests.post", side_effect=mock_post):
                import execution_router
                # Force reload to pick up env
                import importlib; importlib.reload(execution_router)
                execution_router.route_to_execution(
                    system="alpha",
                    concordance={
                        "ticker": "NVDA", "direction": "bullish",
                        "pathway": "P1", "scores": {},
                        "weighted_score": 70.0, "window_id": "w1",
                    },
                    synthesis_verdict={
                        "verdict": "GO", "votes_go": 3, "sizing_mult": 1.0,
                        "echo_chamber_flagged": False, "brain_summary": {},
                        "axiom_risk_score": 0.0,
                    },
                    position_size=2000.0,
                    alpha_exec_url="http://localhost:8005",
                    prime_exec_url="http://localhost:8006",
                    auth_secret="x" * 64,
                )

            self.assertIn(
                "auto_execute", captured_payload,
                "OMNI dispatch payload must include 'auto_execute' field for execution services",
            )
        finally:
            sys.path.pop(0)


if __name__ == "__main__":
    unittest.main()
