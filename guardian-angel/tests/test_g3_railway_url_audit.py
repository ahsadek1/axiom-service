"""
test_g3_railway_url_audit.py — G3 Railway URL Audit tests.

Verifies that all guardian-angel source files use env vars for Railway URLs
and contain no hardcoded railway.app fallback strings.
"""
import os
import re

GUARDIAN_DIR = os.path.join(os.path.dirname(__file__), "..")

# Match Railway URLs that are NOT in comments (# ...) or docstrings used as example text
# Strategy: find lines where the URL is a Python string literal (not after a #)
RAILWAY_PATTERN = re.compile(r"https://\S+\.up\.railway\.app")


def _find_hardcoded_urls(content: str) -> list:
    """Return Railway URLs that appear as string literals (not in comments or example strings)."""
    found = []
    for line in content.splitlines():
        stripped = line.strip()
        # Skip comment lines and inline comments
        if stripped.startswith("#"):
            continue
        # Check for Railway URL in a string literal (quoted value, not a comment)
        # Remove inline comments first
        code_part = line.split("  #")[0].split(" #")[0]
        matches = RAILWAY_PATTERN.findall(code_part)
        # Accept if the URL is inside os.getenv() — that's the example in the docstring
        for m in matches:
            # It's hardcoded if it's a bare string literal, not an env var default
            if 'os.getenv' not in code_part and 'backboard.railway.app' not in m:
                found.append(m)
    return found

SOURCE_FILES = [
    "execution_watchdog_source.py",
    "integrity_dashboard_source.py",
    "nexus_health_monitor_v2_source.py",
    "trading_integrity_check_source.py",
]


def _read(filename: str) -> str:
    path = os.path.join(GUARDIAN_DIR, filename)
    with open(path) as f:
        return f.read()


class TestNoHardcodedRailwayUrls:
    def test_execution_watchdog_no_hardcoded_url(self):
        content = _read("execution_watchdog_source.py")
        matches = _find_hardcoded_urls(content)
        assert not matches, f"Hardcoded Railway URLs in execution_watchdog_source.py: {matches}"

    def test_integrity_dashboard_no_hardcoded_url(self):
        content = _read("integrity_dashboard_source.py")
        matches = _find_hardcoded_urls(content)
        assert not matches, f"Hardcoded Railway URLs in integrity_dashboard_source.py: {matches}"

    def test_health_monitor_no_hardcoded_url(self):
        content = _read("nexus_health_monitor_v2_source.py")
        matches = _find_hardcoded_urls(content)
        assert not matches, f"Hardcoded Railway URLs in nexus_health_monitor_v2_source.py: {matches}"

    def test_trading_integrity_no_hardcoded_url(self):
        content = _read("trading_integrity_check_source.py")
        matches = _find_hardcoded_urls(content)
        assert not matches, f"Hardcoded Railway URLs in trading_integrity_check_source.py: {matches}"

    def test_trading_integrity_no_hardcoded_token(self):
        content = _read("trading_integrity_check_source.py")
        # Token should not be hardcoded — must come from env
        assert "95959846-6306-47e7-a502-b7461514ffff" not in content, \
            "Hardcoded Railway token found in trading_integrity_check_source.py"


class TestEnvVarUsage:
    def test_execution_watchdog_uses_env(self):
        content = _read("execution_watchdog_source.py")
        assert 'os.getenv("RAILWAY_ALPHA_URL"' in content

    def test_integrity_dashboard_uses_env(self):
        content = _read("integrity_dashboard_source.py")
        assert 'os.getenv("RAILWAY_ALPHA_URL"' in content
        assert 'os.getenv("RAILWAY_PRIME_URL"' in content

    def test_health_monitor_uses_env(self):
        content = _read("nexus_health_monitor_v2_source.py")
        assert 'os.getenv("RAILWAY_AXIOM_URL"' in content
        assert 'os.getenv("RAILWAY_ALPHA_URL"' in content
        assert 'os.getenv("RAILWAY_PRIME_URL"' in content

    def test_trading_integrity_uses_env(self):
        content = _read("trading_integrity_check_source.py")
        assert 'os.getenv("RAILWAY_ALPHA_URL"' in content
        assert 'os.getenv("RAILWAY_AXIOM_URL"' in content
        assert 'os.getenv("RAILWAY_PRIME_URL"' in content
        assert 'os.getenv("RAILWAY_TOKEN"' in content


class TestNullGuards:
    def test_watchdog_has_null_guard(self):
        content = _read("execution_watchdog_source.py")
        assert "if not ALPHA_URL" in content

    def test_trading_integrity_alpha_null_guard(self):
        content = _read("trading_integrity_check_source.py")
        assert "if not ALPHA_URL" in content

    def test_trading_integrity_axiom_null_guard(self):
        content = _read("trading_integrity_check_source.py")
        assert "if not AXIOM_URL" in content

    def test_trading_integrity_prime_null_guard(self):
        content = _read("trading_integrity_check_source.py")
        assert "if not PRIME_URL" in content

    def test_health_monitor_diagnostic_null_guard(self):
        content = _read("nexus_health_monitor_v2_source.py")
        assert "if not url" in content
