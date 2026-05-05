"""
test_g3_railway_urls.py — G3: Railway URL audit tests.

Verifies: no hardcoded Railway URLs in any service file, env-var-only
pattern enforced, null-guard skip behaviour, verify script logic. 6 tests.
"""

import os
import re
import sys
import unittest
from unittest.mock import patch, MagicMock

NEXUS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Hardcoded Railway URL patterns that must not appear in source
BANNED_PATTERNS = [
    r"worker-production-2060\.up\.railway\.app",
    r"nexus-prime-bot-production\.up\.railway\.app",
    r"axiom-production-334c\.up\.railway\.app",
]

# Files that previously contained hardcoded Railway URLs
AUDITED_FILES = [
    os.path.join(NEXUS_ROOT, "atlas", "buffer_client.py"),
    os.path.join(NEXUS_ROOT, "sage", "buffer_client.py"),
    os.path.join(NEXUS_ROOT, "omni", "mock_rehearsal.py"),
    os.path.join(NEXUS_ROOT, "guardian-angel", "execution_watchdog_source.py"),
    os.path.join(NEXUS_ROOT, "guardian-angel", "integrity_dashboard_source.py"),
    os.path.join(NEXUS_ROOT, "guardian-angel", "trading_integrity_check_source.py"),
    os.path.join(NEXUS_ROOT, "guardian-angel", "nexus_health_monitor_v2_source.py"),
]


class TestNoHardcodedRailwayURLs(unittest.TestCase):

    def test_t1_no_hardcoded_railway_urls_in_audited_files(self):
        """T1: None of the 7 audited files contain hardcoded Railway production URLs."""
        violations = []
        for fpath in AUDITED_FILES:
            if not os.path.exists(fpath):
                continue
            with open(fpath) as f:
                source = f.read()
            for pattern in BANNED_PATTERNS:
                if re.search(pattern, source):
                    violations.append(f"{os.path.basename(fpath)}: matches /{pattern}/")
        self.assertEqual(
            violations, [],
            f"Hardcoded Railway URLs found:\n" + "\n".join(violations),
        )

    def test_t2_no_hardcoded_railway_token(self):
        """T2: trading_integrity_check_source.py has no hardcoded RAILWAY_TOKEN string."""
        fpath = os.path.join(
            NEXUS_ROOT, "guardian-angel", "trading_integrity_check_source.py"
        )
        if not os.path.exists(fpath):
            self.skipTest("File not found")
        with open(fpath) as f:
            source = f.read()
        # Must not contain raw UUID token (hardcoded)
        self.assertNotRegex(
            source,
            r'RAILWAY_TOKEN\s*=\s*"[0-9a-f\-]{36}"',
            "RAILWAY_TOKEN must come from os.getenv(), not be hardcoded",
        )

    def test_t3_atlas_buffer_uses_env_var(self):
        """T3: atlas/buffer_client.py reads RAILWAY_WEBHOOK_URL from env, no string default."""
        fpath = os.path.join(NEXUS_ROOT, "atlas", "buffer_client.py")
        with open(fpath) as f:
            source = f.read()
        # Must have os.getenv("RAILWAY_WEBHOOK_URL") without a hardcoded fallback
        self.assertIn('os.getenv("RAILWAY_WEBHOOK_URL")', source)
        self.assertNotRegex(source, r'os\.getenv\("RAILWAY_WEBHOOK_URL",\s*"http')

    def test_t4_sage_buffer_uses_env_var(self):
        """T4: sage/buffer_client.py same pattern as atlas."""
        fpath = os.path.join(NEXUS_ROOT, "sage", "buffer_client.py")
        with open(fpath) as f:
            source = f.read()
        self.assertIn('os.getenv("RAILWAY_WEBHOOK_URL")', source)
        self.assertNotRegex(source, r'os\.getenv\("RAILWAY_WEBHOOK_URL",\s*"http')


class TestNullGuardBehaviour(unittest.TestCase):

    def test_t5_atlas_skips_railway_when_url_not_set(self):
        """T5: Atlas _submit_to_railway_async returns immediately when RAILWAY_WEBHOOK_URL is None."""
        sys.path.insert(0, os.path.join(NEXUS_ROOT, "atlas"))
        import importlib
        import buffer_client
        # Patch the module-level constant to None
        original = buffer_client.RAILWAY_WEBHOOK_URL
        buffer_client.RAILWAY_WEBHOOK_URL = None
        try:
            fired = []
            with patch("buffer_client.requests.post", side_effect=lambda *a, **k: fired.append(1)):
                buffer_client._submit_to_railway_async({"ticker": "AAPL", "agent": "Atlas",
                                                        "direction": "bullish", "score": 72.0,
                                                        "reasoning": "test"})
            import time; time.sleep(0.1)  # let background thread settle
            self.assertEqual(fired, [], "requests.post should NOT fire when RAILWAY_WEBHOOK_URL is None")
        finally:
            buffer_client.RAILWAY_WEBHOOK_URL = original
            sys.path.pop(0)

    def test_t6_verify_script_skips_when_urls_empty(self):
        """T6: verify_railway_urls.check() returns status=skipped when URL env var is empty."""
        sys.path.insert(0, os.path.join(NEXUS_ROOT, "scripts"))
        import verify_railway_urls
        result = verify_railway_urls.check("test-service", "")
        self.assertEqual(result["status"], "skipped")
        sys.path.pop(0)


if __name__ == "__main__":
    unittest.main()
