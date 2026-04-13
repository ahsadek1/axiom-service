"""
test_api.py — OMNI API endpoint integration tests.

All external calls (brains, Axiom, execution, Telegram) mocked.
"""

import sys
import os
import json
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

os.environ.setdefault("NEXUS_WEBHOOK_SECRET", "test-nexus-secret")
os.environ.setdefault("NEXUS_PRIME_SECRET",   "test-nexus-secret")   # Pass B fix #1
os.environ.setdefault("OMNI_SECRET",          "test-omni-secret")
os.environ.setdefault("ANTHROPIC_API_KEY",    "test-anthropic")
os.environ.setdefault("OPENAI_API_KEY",       "test-openai")
os.environ.setdefault("GEMINI_API_KEY",       "test-gemini")
os.environ.setdefault("DEEPSEEK_API_KEY",     "test-deepseek")
os.environ.setdefault("AXIOM_URL",            "http://localhost:8001")
os.environ.setdefault("AXIOM_SECRET",         "test-axiom-secret")
os.environ.setdefault("ALPHA_EXECUTION_URL",  "http://localhost:8005")
os.environ.setdefault("PRIME_EXECUTION_URL",  "http://localhost:8006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN",   "9999:TEST")
os.environ.setdefault("AHMED_CHAT_ID",        "8573754783")
os.environ.setdefault("OMNI_DB_PATH",         tempfile.mktemp(suffix=".db"))

SECRET  = "test-nexus-secret"
HEADERS = {"X-Nexus-Secret": SECRET}

VALID_CONCORDANCE = {
    "ticker":          "NVDA",
    "direction":       "bullish",
    "system":          "alpha",
    "pathway":         "P1",
    "weighted_score":  82.5,
    "agents_involved": ["Cipher", "Atlas", "Sage"],
    "scores":          {"Cipher": 88, "Atlas": 82, "Sage": 76},
    "verdict":         "GO",
    "sizing_mult":     1.0,
    "window_id":       "2026-04-10-0930",
    "echo_chamber":    False,
    "notes":           [],
}

GO_BRAIN_RESULTS = {
    brain: {
        "vote":         "GO",
        "confidence":   85,
        "concern_1":    "None",
        "concern_2":    "None",
        "echo_chamber": False,
        "reasoning":    "Strong signal",
    }
    for brain in ("claude", "o3mini", "gemini", "deepseek")
}

AXIOM_CLEAN = {
    "ticker":     "NVDA",
    "risk_score": 2.5,
    "sizing_mult": 1.0,
    "hard_stops": [],
    "in_pool":    True,
    "concern_1":  "None",
    "concern_2":  "None",
}


@pytest.fixture(scope="module")
def client():
    from main import app
    from database import init_db
    init_db(os.environ["OMNI_DB_PATH"])
    with TestClient(app) as c:
        yield c


class TestHealth:
    def test_health_200(self, client):
        assert client.get("/health").status_code == 200

    def test_health_says_omni(self, client):
        assert client.get("/health").json()["service"] == "omni"

    def test_health_no_auth_needed(self, client):
        assert client.get("/health").status_code not in (401, 403)


class TestConcordanceEndpoint:
    def test_rejects_no_secret(self, client):
        resp = client.post("/concordance", json=VALID_CONCORDANCE)
        assert resp.status_code == 403

    def test_rejects_wrong_secret(self, client):
        resp = client.post(
            "/concordance",
            json    = VALID_CONCORDANCE,
            headers = {"X-Nexus-Secret": "wrong"},
        )
        assert resp.status_code == 403

    @patch("main.run_all_brains", return_value=GO_BRAIN_RESULTS)
    @patch("main.assess_ticker", return_value=AXIOM_CLEAN)
    @patch("main.get_regime",    return_value={"classification": "NORMAL", "vix": 18.0})
    @patch("main.route_to_execution", return_value=(True, "HTTP 200"))
    @patch("main.send_synthesis_card", return_value=True)
    def test_full_pipeline_strong_go(self, mock_tg, mock_exec, mock_regime, mock_axiom, mock_brains, client):
        resp = client.post("/concordance", json=VALID_CONCORDANCE, headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"]   == "STRONG_GO"
        assert data["votes_go"]  == 4
        assert data["execution_ok"] is True

    @patch("main.run_all_brains", return_value={
        **{b: {"vote":"GO","confidence":80,"concern_1":"None","concern_2":"None","echo_chamber":False,"reasoning":"test"}
           for b in ("claude","o3mini","gemini")},
        "deepseek": {"vote":"NO_GO","confidence":70,"concern_1":"Overextended","concern_2":"None","echo_chamber":False,"reasoning":"extended"},
    })
    @patch("main.assess_ticker", return_value=AXIOM_CLEAN)
    @patch("main.get_regime", return_value={"classification": "NORMAL", "vix": 18.0})
    @patch("main.route_to_execution", return_value=(True, "HTTP 200"))
    @patch("main.send_synthesis_card", return_value=True)
    def test_3_of_4_is_go(self, mock_tg, mock_exec, mock_regime, mock_axiom, mock_brains, client):
        resp = client.post("/concordance", json=VALID_CONCORDANCE, headers=HEADERS)
        data = resp.json()
        assert data["verdict"]  == "GO"
        assert data["votes_go"] == 3

    @patch("main.run_all_brains", return_value={
        **{b: {"vote":"GO","confidence":80,"concern_1":"None","concern_2":"None","echo_chamber":False,"reasoning":"test"}
           for b in ("claude","o3mini")},
        "gemini":   {"vote":"NO_GO","confidence":70,"concern_1":"None","concern_2":"None","echo_chamber":False,"reasoning":"test"},
        "deepseek": {"vote":"NO_GO","confidence":70,"concern_1":"None","concern_2":"None","echo_chamber":False,"reasoning":"test"},
    })
    @patch("main.assess_ticker", return_value=AXIOM_CLEAN)
    @patch("main.get_regime", return_value={"classification": "NORMAL", "vix": 18.0})
    @patch("main.send_conditional_alert", return_value=True)
    def test_2_of_4_is_conditional(self, mock_alert, mock_regime, mock_axiom, mock_brains, client):
        resp = client.post("/concordance", json=VALID_CONCORDANCE, headers=HEADERS)
        data = resp.json()
        assert data["verdict"]    == "CONDITIONAL"
        assert data["execution_ok"] is None

    @patch("main.run_all_brains", return_value=GO_BRAIN_RESULTS)
    @patch("main.assess_ticker", return_value={
        "ticker": "NVDA", "risk_score": 10, "sizing_mult": 0.0,
        "hard_stops": ["CRISIS regime — all entries halted"],
        "in_pool": False, "concern_1": "None", "concern_2": "None",
    })
    @patch("main.get_regime", return_value={"classification": "CRISIS", "vix": 38.0})
    def test_axiom_hard_stop_blocks_execution(self, mock_regime, mock_axiom, mock_brains, client):
        resp = client.post("/concordance", json=VALID_CONCORDANCE, headers=HEADERS)
        data = resp.json()
        assert data["verdict"]     == "NO_GO"
        assert data["axiom_blocked"] is True
        assert data["execution_ok"] is None

    def test_axiom_unreachable_blocks_execution(self, client):
        """
        Pass B fix (V9): Axiom timeout must BLOCK execution, not silently allow it.
        Original code returned hard_stops=[] on timeout — trades fired without regime check.
        Fixed: axiom_client returns hard_stops=["AXIOM_UNREACHABLE"] on any failure.
        """
        axiom_unreachable = {
            "ticker": "AAPL", "error": "timeout",
            "hard_stops": ["AXIOM_UNREACHABLE"],
            "sizing_mult": 0.0, "risk_score": None,
        }
        with patch("main.run_all_brains", return_value=GO_BRAIN_RESULTS), \
             patch("main.assess_ticker", return_value=axiom_unreachable), \
             patch("main.get_regime", return_value={"classification": "NORMAL", "vix": 18.0}):
            resp = client.post("/concordance", json=VALID_CONCORDANCE, headers=HEADERS)
        data = resp.json()
        assert data["axiom_blocked"] is True, (
            "Axiom unreachable must block execution. "
            f"Got verdict={data.get('verdict')}, axiom_blocked={data.get('axiom_blocked')}"
        )
        assert data["execution_ok"] is None

    def test_rejects_unknown_system(self, client):
        bad = {**VALID_CONCORDANCE, "system": "gamma"}
        resp = client.post("/concordance", json=bad, headers=HEADERS)
        assert resp.status_code == 422

    def test_rejects_invalid_pathway(self, client):
        bad = {**VALID_CONCORDANCE, "pathway": "P9"}
        resp = client.post("/concordance", json=bad, headers=HEADERS)
        assert resp.status_code == 422

    def test_prime_system_accepted(self, client):
        prime = {**VALID_CONCORDANCE, "system": "prime"}
        with patch("main.run_all_brains", return_value=GO_BRAIN_RESULTS), \
             patch("main.assess_ticker", return_value=AXIOM_CLEAN), \
             patch("main.get_regime", return_value={"classification":"NORMAL","vix":18.0}), \
             patch("main.route_to_execution", return_value=(True, "HTTP 200")), \
             patch("main.send_synthesis_card", return_value=True):
            resp = client.post("/concordance", json=prime, headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["system"] == "prime"

    def test_prime_buffer_header_accepted(self, client):
        """
        Pass B fix verification: Prime Buffer sends X-Nexus-Prime-Secret, not
        X-Nexus-Secret. OMNI must accept both. This test was missing — the Prime
        execution path was silently rejected with 403 in production.
        """
        prime = {**VALID_CONCORDANCE, "system": "prime"}
        prime_headers = {"X-Nexus-Prime-Secret": "test-nexus-secret"}
        with patch("main.run_all_brains", return_value=GO_BRAIN_RESULTS), \
             patch("main.assess_ticker", return_value=AXIOM_CLEAN), \
             patch("main.get_regime", return_value={"classification":"NORMAL","vix":18.0}), \
             patch("main.route_to_execution", return_value=(True, "HTTP 200")), \
             patch("main.send_synthesis_card", return_value=True):
            resp = client.post("/concordance", json=prime, headers=prime_headers)
        assert resp.status_code == 200, (
            f"Prime Buffer header rejected — Prime execution path broken. "
            f"Status: {resp.status_code}, Body: {resp.text[:200]}"
        )


class TestStatus:
    def test_requires_auth(self, client):
        assert client.get("/status").status_code == 403

    def test_returns_synthesis_count(self, client):
        data = client.get("/status", headers=HEADERS).json()
        assert "syntheses_today" in data
        assert "recent_syntheses" in data


class TestDatabase:
    def test_duplicate_window_ticker_upserts(self, client):
        """Submitting same window/ticker/direction/system twice should not error."""
        with patch("main.run_all_brains", return_value=GO_BRAIN_RESULTS), \
             patch("main.assess_ticker", return_value=AXIOM_CLEAN), \
             patch("main.get_regime", return_value={"classification":"NORMAL","vix":18.0}), \
             patch("main.route_to_execution", return_value=(True, "HTTP 200")), \
             patch("main.send_synthesis_card", return_value=True):
            resp1 = client.post("/concordance", json=VALID_CONCORDANCE, headers=HEADERS)
            resp2 = client.post("/concordance", json=VALID_CONCORDANCE, headers=HEADERS)
        assert resp1.status_code == 200
        assert resp2.status_code == 200
