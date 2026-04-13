"""
test_api.py — Integration tests for Alpha Buffer API endpoints.
"""

import sys
import os
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

os.environ.setdefault("NEXUS_WEBHOOK_SECRET", "test-alpha-secret")
os.environ.setdefault("ALPHA_DB_PATH",         tempfile.mktemp(suffix=".db"))
os.environ.setdefault("OMNI_WEBHOOK_URL",      "http://localhost:8004/concordance")
os.environ.setdefault("TELEGRAM_BOT_TOKEN",    "9999:TEST")
os.environ.setdefault("AHMED_CHAT_ID",         "8573754783")
os.environ.setdefault("SOLO_ENTRIES_ENABLED",  "true")

SECRET  = "test-alpha-secret"
HEADERS = {"X-Nexus-Secret": SECRET}


@pytest.fixture(scope="module")
def client():
    from main import app
    from database import init_db, init_circuit_breaker
    from config import load_settings
    db_path = os.environ["ALPHA_DB_PATH"]
    init_db(db_path)
    init_circuit_breaker(db_path)
    with TestClient(app) as c:
        yield c


class TestHealth:
    def test_health_200(self, client):
        assert client.get("/health").status_code == 200

    def test_health_no_auth(self, client):
        """Health has no auth — 200 always."""
        resp = client.get("/health")
        assert resp.status_code not in (401, 403)


class TestSubmitPick:
    def test_rejects_no_secret(self, client):
        resp = client.post("/submit", json={
            "agent": "Cipher", "ticker": "NVDA", "direction": "bullish", "score": 75
        })
        assert resp.status_code == 403

    def test_rejects_wrong_secret(self, client):
        resp = client.post(
            "/submit",
            json    = {"agent": "Cipher", "ticker": "NVDA", "direction": "bullish", "score": 75},
            headers = {"X-Nexus-Secret": "wrong"},
        )
        assert resp.status_code == 403

    def test_accepts_valid_submission(self, client):
        resp = client.post(
            "/submit",
            json    = {"agent": "Cipher", "ticker": "AAPL", "direction": "bullish", "score": 75},
            headers = HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] is True
        assert data["ticker"]   == "AAPL"
        assert data["agent"]    == "Cipher"

    def test_rejects_unknown_agent(self, client):
        resp = client.post(
            "/submit",
            json    = {"agent": "Trident", "ticker": "NVDA", "direction": "bullish", "score": 75},
            headers = HEADERS,
        )
        assert resp.status_code == 422

    def test_rejects_score_below_minimum(self, client):
        resp = client.post(
            "/submit",
            json    = {"agent": "Cipher", "ticker": "NVDA", "direction": "bullish", "score": 50},
            headers = HEADERS,
        )
        assert resp.status_code == 422

    def test_rejects_invalid_direction(self, client):
        resp = client.post(
            "/submit",
            json    = {"agent": "Cipher", "ticker": "NVDA", "direction": "long", "score": 75},
            headers = HEADERS,
        )
        assert resp.status_code == 422

    def test_ticker_normalized_uppercase(self, client):
        resp = client.post(
            "/submit",
            json    = {"agent": "Atlas", "ticker": "msft", "direction": "bullish", "score": 70},
            headers = HEADERS,
        )
        assert resp.json()["ticker"] == "MSFT"

    def test_p3_forms_on_solo_high_conviction(self, client):
        """Solo agent with score ≥ 90 should form P3 when enabled."""
        with patch("main.dispatch_to_omni", return_value=(True, "HTTP 200")):
            resp = client.post(
                "/submit",
                json    = {"agent": "Cipher", "ticker": "SOLOSOLO", "direction": "bullish", "score": 93},
                headers = HEADERS,
            )
        data = resp.json()
        assert data["accepted"] is True
        assert data["concordance"] is not None
        assert data["concordance"]["pathway"] == "P3"


class TestStatus:
    def test_status_requires_auth(self, client):
        assert client.get("/status").status_code == 403

    def test_status_returns_circuit_breaker(self, client):
        resp = client.get("/status", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert "circuit_breaker" in data
        assert "window_id"       in data


class TestCircuitBreakerReset:
    def test_reset_requires_auth(self, client):
        assert client.post("/circuit-breaker/reset").status_code == 403

    def test_reset_returns_normal(self, client):
        resp = client.post("/circuit-breaker/reset", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["status"] == "NORMAL"


class TestTradeOutcome:
    def test_trade_outcome_requires_auth(self, client):
        resp = client.post("/trade-outcome", json={"ticker": "NVDA", "won": True, "pnl_pct": 0.35})
        assert resp.status_code == 403

    def test_records_win(self, client):
        resp = client.post(
            "/trade-outcome",
            json    = {"ticker": "NVDA", "won": True, "pnl_pct": 0.40},
            headers = HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == "win"

    def test_records_loss(self, client):
        resp = client.post(
            "/trade-outcome",
            json    = {"ticker": "AMD", "won": False, "pnl_pct": -0.15},
            headers = HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == "loss"
