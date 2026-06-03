"""
test_api.py — Prime Buffer API endpoint integration tests.

Prime uses X-Nexus-Prime-Secret header. Min score 63.
"""

import sys
import os
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

os.environ.setdefault("NEXUS_PRIME_SECRET",  "test-prime-secret")
os.environ.setdefault("PRIME_DB_PATH",        tempfile.mktemp(suffix=".db"))
os.environ.setdefault("OMNI_WEBHOOK_URL",     "http://localhost:8004/concordance")
os.environ.setdefault("TELEGRAM_BOT_TOKEN",   "9999:TEST")
os.environ.setdefault("AHMED_CHAT_ID",        "8573754783")
os.environ.setdefault("SOLO_ENTRIES_ENABLED", "true")

SECRET  = "test-prime-secret"
HEADERS = {"X-Nexus-Prime-Secret": SECRET}


@pytest.fixture(scope="module")
def client():
    from main import app
    from database import init_db, init_circuit_breaker
    db_path = os.environ["PRIME_DB_PATH"]
    init_db(db_path)
    init_circuit_breaker(db_path)
    with TestClient(app) as c:
        yield c


class TestHealth:
    def test_health_200(self, client):
        assert client.get("/health").status_code == 200

    def test_health_says_prime(self, client):
        assert client.get("/health").json()["service"] == "prime-buffer"


class TestPrimeSubmit:
    def test_rejects_no_secret(self, client):
        resp = client.post("/submit", json={
            "agent": "Cipher", "ticker": "NVDA", "direction": "bullish", "score": 75
        })
        assert resp.status_code == 403

    def test_alpha_secret_rejected(self, client):
        """Alpha's secret must not work on Prime's endpoint."""
        resp = client.post(
            "/submit",
            json    = {"agent": "Cipher", "ticker": "NVDA", "direction": "bullish", "score": 75},
            headers = {"X-Nexus-Prime-Secret": "test-alpha-secret"},
        )
        assert resp.status_code == 403

    def test_accepts_valid_prime_submission(self, client):
        resp = client.post(
            "/submit",
            json    = {"agent": "Cipher", "ticker": "GOOG", "direction": "bullish", "score": 75},
            headers = HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["accepted"] is True

    def test_rejects_unknown_agent(self, client):
        resp = client.post(
            "/submit",
            json    = {"agent": "Trident", "ticker": "NVDA", "direction": "bullish", "score": 75},
            headers = HEADERS,
        )
        assert resp.status_code == 422

    def test_prime_score_minimum_63(self, client):
        """Score 62 must be rejected (Prime minimum is 63)."""
        resp = client.post(
            "/submit",
            json    = {"agent": "Cipher", "ticker": "NVDA", "direction": "bullish", "score": 62},
            headers = HEADERS,
        )
        assert resp.status_code == 422

    def test_score_63_accepted(self, client):
        """Score exactly 63 must be accepted for Prime."""
        resp = client.post(
            "/submit",
            json    = {"agent": "Atlas", "ticker": "PRIMETEST", "direction": "bullish", "score": 63},
            headers = HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["accepted"] is True

    def test_score_58_rejected_by_prime(self, client):
        """Score 58 passes Alpha (58 min) but fails Prime (63 min)."""
        resp = client.post(
            "/submit",
            json    = {"agent": "Sage", "ticker": "NVDA", "direction": "bearish", "score": 58},
            headers = HEADERS,
        )
        assert resp.status_code == 422

    def test_concordance_result_labeled_prime(self, client):
        """When concordance forms, system label must be 'prime'."""
        with patch("main.dispatch_to_omni", return_value=(True, "HTTP 200")):
            resp = client.post(
                "/submit",
                json    = {"agent": "Cipher", "ticker": "PRLABEL", "direction": "bullish", "score": 93},
                headers = HEADERS,
            )
        data = resp.json()
        assert data["concordance"] is not None
        assert data["concordance"]["system"] == "prime"

    def test_p3_solo_high_conviction(self, client):
        """Solo agent with score ≥ 90 forms P3 when enabled."""
        with patch("main.dispatch_to_omni", return_value=(True, "HTTP 200")):
            resp = client.post(
                "/submit",
                json    = {"agent": "Cipher", "ticker": "PRIMESOLOTEST", "direction": "bullish", "score": 92},
                headers = HEADERS,
            )
        data = resp.json()
        assert data["concordance"]["pathway"] == "P3"


class TestPrimeStatus:
    def test_requires_prime_secret(self, client):
        assert client.get("/status").status_code == 403

    def test_alpha_secret_rejected_on_status(self, client):
        resp = client.get("/status", headers={"X-Nexus-Prime-Secret": "wrong-secret"})
        assert resp.status_code == 403

    def test_status_returns_service_name(self, client):
        data = client.get("/status", headers=HEADERS).json()
        assert data["service"] == "prime-buffer"


class TestPrimeCircuitBreaker:
    def test_reset_requires_auth(self, client):
        assert client.post("/circuit-breaker/reset").status_code == 403

    def test_reset_returns_normal(self, client):
        resp = client.post("/circuit-breaker/reset", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["status"] == "NORMAL"


class TestTradeOutcome:
    def test_win_recorded(self, client):
        resp = client.post(
            "/trade-outcome",
            json    = {"ticker": "TSLA", "won": True, "pnl_pct": 0.42},
            headers = HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == "win"

    def test_loss_recorded(self, client):
        resp = client.post(
            "/trade-outcome",
            json    = {"ticker": "AMD", "won": False, "pnl_pct": -0.15},
            headers = HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == "loss"
