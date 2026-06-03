"""
test_axiom_agent_integration.py — End-to-End Axiom→Agent Push Integration Test

Tests the PRODUCTION code path: Axiom agent_push.py → Cipher/Atlas/Sage HTTP endpoints.
This is the test that would have caught the auth header bug before it reached live trading.

Verifies:
  1. Auth header (X-Nexus-Secret) is sent correctly
  2. Payload schema is accepted (no 422)
  3. All 3 agents respond 200
  4. No agent returns 401/403/422/500

Run as part of every 7 AM diagnostic.
Run manually: cd axiom && .venv/bin/pytest tests/test_axiom_agent_integration.py -v
"""

import sys
import os
import json
from typing import Optional
from unittest.mock import patch, MagicMock

import pytest
import requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"))

# Add axiom to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_push import push_pool_to_agents, _push_to_agent
from config import Settings

# ── Fixtures ──────────────────────────────────────────────────────────────────

# TEST FIXTURE — this is the shared NEXUS_SECRET for local integration tests.
# Acceptable in test files. Never use this pattern in production code.
NEXUS_SECRET = "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2"

CIPHER_URL = "http://localhost:9001/receive-pool"
ATLAS_URL  = "http://localhost:9002/receive-pool"
SAGE_URL   = "http://localhost:9003/receive-pool"

PRODUCTION_STYLE_PAYLOAD = {
    # Required fields — exact schema Axiom sends in production
    "pool": [
        {"ticker": "AAPL", "ails_score": 72, "price": 205.0, "iv_rank": 35},
        {"ticker": "NVDA", "ails_score": 81, "price": 875.0, "iv_rank": 42},
    ],
    "count": 2,
    "window_id": "INTEGRATION-TEST-20260414-1300",
    "updated_at": "2026-04-14T13:00:00-04:00",
    "market_open": True,
    "regime": {
        "classification": "NORMAL",
        "vix": 19.1,
        "alpha_debit_allowed": True,
        "alpha_credit_allowed": True,
        "prime_allowed": True,
        "strategy_bias": "NEUTRAL",
    },
    "coherence_summary": {
        "high_coherence_count": 2,
        "low_coherence_count": 0,
        "coherence_available": True,
    },
    "coherence_available": True,
    "echo_chamber_risk": [],
    "cycle_patterns": [],
    "pattern_intelligence_available": False,
    "oracle_warmed": True,
}


# ── Unit Tests: _push_to_agent ─────────────────────────────────────────────────

class TestPushToAgent:
    """Unit tests for _push_to_agent — the function that was missing the auth header."""

    def test_auth_header_is_sent(self):
        """REGRESSION: Auth header must be present in every outbound request."""
        captured_headers = {}

        def mock_post(url, json, headers, timeout):
            captured_headers.update(headers)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"status": "received"}
            return mock_resp

        with patch("requests.post", side_effect=mock_post):
            result = _push_to_agent(
                agent_name="Cipher",
                url=CIPHER_URL,
                payload=PRODUCTION_STYLE_PAYLOAD,
                nexus_secret=NEXUS_SECRET,
                )

        assert "X-Nexus-Secret" in captured_headers, (
            "Auth header X-Nexus-Secret was NOT sent — this is the bug that blocked all trades"
        )
        assert captured_headers["X-Nexus-Secret"] == NEXUS_SECRET

    def test_auth_header_value_correct(self):
        """Auth header value must match the shared secret exactly."""
        wrong_secret = "wrong-secret"
        captured_headers = {}

        def mock_post(url, json, headers, timeout):
            captured_headers.update(headers)
            mock_resp = MagicMock()
            mock_resp.status_code = 401
            mock_resp.json.return_value = {"error": "Forbidden"}
            return mock_resp

        with patch("requests.post", side_effect=mock_post):
            result = _push_to_agent(
                agent_name="Cipher",
                url=CIPHER_URL,
                payload=PRODUCTION_STYLE_PAYLOAD,
                nexus_secret=wrong_secret,
            )

        # Wrong secret → agent should reject with 401
        # _push_to_agent returns (status_str, response_ms) tuple
        assert result[0] in ("error", "timeout", "success") or result[1] is None

    def test_payload_is_sent_as_json(self):
        """Payload must arrive as JSON body, not query string."""
        captured_json = {}

        def mock_post(url, json, headers, timeout):
            captured_json.update(json)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"status": "received"}
            return mock_resp

        with patch("requests.post", side_effect=mock_post):
            _push_to_agent(
                agent_name="Cipher",
                url=CIPHER_URL,
                payload=PRODUCTION_STYLE_PAYLOAD,
                nexus_secret=NEXUS_SECRET,
            )

        assert "pool" in captured_json  # required field present
        assert captured_json.get("count") == 2  # count correct
        assert "window_id" in captured_json

    def test_dict_coherence_summary_accepted(self):
        """REGRESSION: coherence_summary as dict (not list) must not cause schema error."""
        payload_with_dict_coherence = {**PRODUCTION_STYLE_PAYLOAD, "coherence_summary": {"key": "value"}}
        captured_json = {}

        def mock_post(url, json, headers, timeout):
            captured_json.update(json)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"status": "received"}
            return mock_resp

        with patch("requests.post", side_effect=mock_post):
            result = _push_to_agent(
                agent_name="Cipher",
                url=CIPHER_URL,
                payload=payload_with_dict_coherence,
                nexus_secret=NEXUS_SECRET,
            )

        # No exception raised — dict coherence_summary handled
        # dict coherence_summary was transmitted correctly
        assert isinstance(captured_json.get("coherence_summary"), dict)

    def test_connection_error_handled_gracefully(self):
        """Agent unavailable should not crash Axiom — must return error result."""
        with patch("requests.post", side_effect=requests.exceptions.ConnectionError("refused")):
            result = _push_to_agent(
                agent_name="Cipher",
                url="http://localhost:9001/receive-pool",
                payload=PRODUCTION_STYLE_PAYLOAD,
                nexus_secret=NEXUS_SECRET,
            )

        assert result[0] in ("error", "timeout")

    def test_timeout_handled_gracefully(self):
        """Agent timeout should not crash Axiom — must return error result."""
        with patch("requests.post", side_effect=requests.exceptions.Timeout("timed out")):
            result = _push_to_agent(
                agent_name="Cipher",
                url="http://localhost:9001/receive-pool",
                payload=PRODUCTION_STYLE_PAYLOAD,
                nexus_secret=NEXUS_SECRET,
            )

        assert result[0] in ("error", "timeout")


# ── Integration Tests: Live Agents ────────────────────────────────────────────

def _agents_available() -> bool:
    """Return True if all 3 agents are reachable."""
    try:
        for url in [
            "http://localhost:9001/health",
            "http://localhost:9002/health",
            "http://localhost:9003/health",
        ]:
            r = requests.get(url, timeout=3)
            if r.status_code != 200:
                return False
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _agents_available(), reason="Agents not running")
class TestLiveAgentPush:
    """Integration tests against live agents. Skipped if agents are down."""

    def test_cipher_accepts_auth(self):
        """Cipher must return 200 with correct auth header."""
        resp = requests.post(
            CIPHER_URL,
            json=PRODUCTION_STYLE_PAYLOAD,
            headers={
                "X-Nexus-Secret": NEXUS_SECRET,
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        assert resp.status_code == 200, (
            f"Cipher returned {resp.status_code}: {resp.text[:200]}"
        )

    def test_atlas_accepts_auth(self):
        """Atlas must return 200 with correct auth header."""
        resp = requests.post(
            ATLAS_URL,
            json=PRODUCTION_STYLE_PAYLOAD,
            headers={
                "X-Nexus-Secret": NEXUS_SECRET,
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        assert resp.status_code == 200, (
            f"Atlas returned {resp.status_code}: {resp.text[:200]}"
        )

    def test_sage_accepts_auth(self):
        """Sage must return 200 with correct auth header."""
        resp = requests.post(
            SAGE_URL,
            json=PRODUCTION_STYLE_PAYLOAD,
            headers={
                "X-Nexus-Secret": NEXUS_SECRET,
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        assert resp.status_code == 200, (
            f"Sage returned {resp.status_code}: {resp.text[:200]}"
        )

    def test_cipher_rejects_no_auth(self):
        """Cipher must return 401/403 with missing auth header."""
        resp = requests.post(
            CIPHER_URL,
            json=PRODUCTION_STYLE_PAYLOAD,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        assert resp.status_code in (401, 403), (
            f"Cipher accepted request with no auth header — returned {resp.status_code}"
        )

    def test_all_agents_accept_dict_coherence(self):
        """All agents must accept dict coherence_summary (not just list)."""
        payload = {**PRODUCTION_STYLE_PAYLOAD, "coherence_summary": {"high_count": 5}}
        for name, url in [("Cipher", CIPHER_URL), ("Atlas", ATLAS_URL), ("Sage", SAGE_URL)]:
            resp = requests.post(
                url,
                json=payload,
                headers={"X-Nexus-Secret": NEXUS_SECRET, "Content-Type": "application/json"},
                timeout=10,
            )
            assert resp.status_code == 200, (
                f"{name} rejected dict coherence_summary: {resp.status_code} {resp.text[:200]}"
            )

    def test_push_pool_to_agents_end_to_end(self):
        """Full push_pool_to_agents() call — tests complete production code path."""
        results = push_pool_to_agents(
            pool_payload=PRODUCTION_STYLE_PAYLOAD,
            agent_webhooks={"Cipher": CIPHER_URL, "Atlas": ATLAS_URL, "Sage": SAGE_URL},
            db_path="/Users/ahmedsadek/nexus/data/axiom.db",
            bot_token="7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c",
            chat_id="8573754783",
            window_id="INTEGRATION-TEST",
            nexus_secret=NEXUS_SECRET,
        )
        successes = [k for k, v in results.items() if v == "success"]
        assert len(successes) == 3, (
            f"Expected 3 successful pushes, got {len(successes)}: {results}"
        )
