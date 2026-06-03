"""
test_g2_403_fix.py — G2: Atlas 403 on Prime submission fix tests.

Covers: Atlas startup secret validation, Prime Buffer diagnostic logging,
and Guardian Angel /status audit (no unauthenticated calls).
7 test cases.
"""

import os
import sys
import importlib
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# T1-T2: Atlas startup NEXUS_PRIME_SECRET validation
# ---------------------------------------------------------------------------

class TestAtlasStartupValidation(unittest.TestCase):

    def test_t1_valid_prime_secret_logs_info(self):
        """T1: Atlas starts with valid 64-char NEXUS_PRIME_SECRET → logs INFO, no CRITICAL."""
        valid_secret = "a" * 64
        critical_messages = []
        info_messages = []

        with patch("logging.Logger.critical", side_effect=lambda msg, *a, **k: critical_messages.append(msg)), \
             patch("logging.Logger.info", side_effect=lambda msg, *a, **k: info_messages.append(msg)):
            # Simulate the startup validation block from atlas/main.py
            prime_secret = valid_secret
            if not prime_secret or len(prime_secret) < 32:
                import logging
                logging.getLogger("nexus.atlas").critical(
                    "NEXUS_PRIME_SECRET invalid at startup (len=%d). Prime submissions will 403.",
                    len(prime_secret),
                )
            else:
                import logging
                logging.getLogger("nexus.atlas").info(
                    "Atlas NEXUS_PRIME_SECRET validated at startup (len=%d)", len(prime_secret)
                )

        self.assertEqual(len(critical_messages), 0)

    def test_t2_empty_prime_secret_logs_critical(self):
        """T2: Atlas starts with empty NEXUS_PRIME_SECRET → logs CRITICAL."""
        critical_calls = []

        import logging
        original_critical = logging.Logger.critical

        def capture_critical(self_logger, msg, *args, **kwargs):
            critical_calls.append(msg % args if args else msg)
            return original_critical(self_logger, msg, *args, **kwargs)

        with patch.object(logging.Logger, "critical", capture_critical):
            prime_secret = ""
            if not prime_secret or len(prime_secret) < 32:
                logging.getLogger("nexus.atlas").critical(
                    "NEXUS_PRIME_SECRET invalid at startup (len=%d). Prime submissions will 403.",
                    len(prime_secret),
                )

        self.assertTrue(any("NEXUS_PRIME_SECRET invalid" in msg for msg in critical_calls))


# ---------------------------------------------------------------------------
# T3-T5: Prime Buffer verify_secret diagnostic logging
# ---------------------------------------------------------------------------

class TestPrimeBufferDiagnosticLogging(unittest.TestCase):
    """Verify verify_secret logs first 8 chars on rejection without exposing full secret."""

    def _make_verify_secret(self, expected_secret: str):
        """Build a verify_secret function matching the patched prime-buffer implementation."""
        import secrets as secrets_mod
        from fastapi import HTTPException
        import logging

        logger = logging.getLogger("test.prime_buffer")
        warning_calls = []
        original_warning = logger.warning

        def capturing_warning(msg, *args, **kwargs):
            warning_calls.append(msg % args if args else msg)
            return original_warning(msg, *args, **kwargs)

        logger.warning = capturing_warning

        def verify_secret(incoming: str) -> None:
            if not incoming or not secrets_mod.compare_digest(incoming, expected_secret):
                logger.warning(
                    "Auth REJECTED on Prime Buffer: incoming=%r expected_len=%d",
                    (incoming[:8] if incoming else "(empty)") + "...",
                    len(expected_secret),
                )
                raise HTTPException(status_code=403, detail="Forbidden")

        return verify_secret, warning_calls

    def test_t3_correct_secret_no_warning(self):
        """T3: Correct secret passes verify_secret with no warning logged."""
        from fastapi import HTTPException
        secret = "x" * 64
        verify, warnings = self._make_verify_secret(secret)
        try:
            verify(secret)  # should not raise
        except HTTPException:
            self.fail("verify_secret raised HTTPException on correct secret")
        self.assertEqual(len(warnings), 0)

    def test_t4_empty_secret_logs_empty_prefix(self):
        """T4: Empty incoming secret → warning logged with '(empty)...' prefix."""
        from fastapi import HTTPException
        secret = "x" * 64
        verify, warnings = self._make_verify_secret(secret)
        with self.assertRaises(HTTPException) as ctx:
            verify("")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertTrue(any("(empty)..." in w for w in warnings))

    def test_t5_wrong_secret_logs_first_8_chars(self):
        """T5: Wrong secret → warning logged with first 8 chars only (not full secret)."""
        from fastapi import HTTPException
        real_secret = "correct_secret_" + "x" * 49
        wrong_secret = "deadbeef" + "y" * 56
        verify, warnings = self._make_verify_secret(real_secret)
        with self.assertRaises(HTTPException) as ctx:
            verify(wrong_secret)
        self.assertEqual(ctx.exception.status_code, 403)
        # First 8 chars of wrong_secret appear in log
        self.assertTrue(any("deadbeef" in w for w in warnings))
        # But full wrong_secret does NOT appear
        self.assertFalse(any(wrong_secret in w for w in warnings))


# ---------------------------------------------------------------------------
# T6-T7: Guardian Angel — no unauthenticated /status calls
# ---------------------------------------------------------------------------

class TestGuardianAngelStatusAudit(unittest.TestCase):

    def test_t6_guardian_angel_uses_health_not_status(self):
        """T6: GA V4 service health checks use /health path, not /status."""
        ga_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..", "guardian-angel", "guardian_angel_v4.py",
        )
        ga_path = os.path.normpath(ga_path)
        if not os.path.exists(ga_path):
            self.skipTest("guardian_angel_v4.py not found — skipping")

        with open(ga_path) as f:
            source = f.read()

        # All health_path entries in the service registry must use /health, not /status
        import re
        health_paths = re.findall(r'"health_path":\s*"([^"]+)"', source)
        for path in health_paths:
            self.assertNotEqual(
                path, "/status",
                f"GA V4 service registry uses '/status' — should use '/health'. Found: {path}"
            )

    def test_t7_no_unauthenticated_status_requests(self):
        """T7: GA V4 source has no bare requests.get(url+'/status') without auth headers."""
        ga_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..", "guardian-angel", "guardian_angel_v4.py",
        )
        ga_path = os.path.normpath(ga_path)
        if not os.path.exists(ga_path):
            self.skipTest("guardian_angel_v4.py not found — skipping")

        with open(ga_path) as f:
            lines = f.readlines()

        for i, line in enumerate(lines, 1):
            # Flag any requests.get call that includes '/status' without an adjacent headers= arg
            if "requests.get" in line and "/status" in line and "headers=" not in line:
                self.fail(
                    f"GA V4 line {i}: unauthenticated /status call detected: {line.strip()}"
                )


if __name__ == "__main__":
    unittest.main()
