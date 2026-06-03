"""
test_api.py — Integration tests for Axiom FastAPI endpoints.

Tests all endpoints with TestClient. Mocks external dependencies.
"""

import sys
import os
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


# ── Mock environment before importing main ────────────────────────────────────
os.environ.setdefault("AXIOM_SECRET",        "test-secret-12345")
os.environ.setdefault("POLYGON_API_KEY",     "test-polygon")
os.environ.setdefault("ALPHA_VANTAGE_KEY",   "test-av")
os.environ.setdefault("FRED_API_KEY",        "test-fred")
os.environ.setdefault("TELEGRAM_BOT_TOKEN",  "9999:TEST")
os.environ.setdefault("AHMED_CHAT_ID",       "8573754783")
os.environ.setdefault("CIPHER_WEBHOOK_URL",  "http://localhost:9001/receive-pool")
os.environ.setdefault("SAGE_WEBHOOK_URL",    "http://localhost:9002/receive-pool")
os.environ.setdefault("ATLAS_WEBHOOK_URL",   "http://localhost:9003/receive-pool")
os.environ.setdefault("ORACLE_URL",          "http://localhost:8007")
os.environ.setdefault("ORACLE_SECRET",       "test-oracle-secret")

_tmp_db = tempfile.mktemp(suffix=".db")
os.environ.setdefault("AXIOM_DB_PATH", _tmp_db)


@pytest.fixture(scope="module")
def client():
    """Create test client with mocked scheduler."""
    with patch("scheduler.create_scheduler") as mock_sched:
        mock_instance = MagicMock()
        mock_instance.get_jobs.return_value = []
        mock_instance.running = True
        mock_sched.return_value = mock_instance

        from main import app
        from database import init_db
        init_db(_tmp_db)

        with TestClient(app) as c:
            yield c


class TestHealthEndpoint:
    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_contains_required_fields(self, client):
        data = client.get("/health").json()
        assert "status"   in data
        assert "service"  in data
        assert "version"  in data
        assert "pool_size" in data

    def test_health_never_401(self, client):
        """Health endpoint has no auth — should never return 401."""
        resp = client.get("/health")
        assert resp.status_code != 401
        assert resp.status_code != 403


class TestPoolEndpoint:
    def test_pool_requires_auth(self, client):
        resp = client.get("/pool")
        assert resp.status_code == 403

    def test_pool_wrong_secret(self, client):
        resp = client.get("/pool", headers={"X-Axiom-Secret": "wrong"})
        assert resp.status_code == 403

    def test_pool_correct_secret(self, client):
        resp = client.get("/pool", headers={"X-Axiom-Secret": "test-secret-12345"})
        assert resp.status_code == 200

    def test_pool_response_structure(self, client):
        data = client.get("/pool", headers={"X-Axiom-Secret": "test-secret-12345"}).json()
        assert "pool"      in data
        assert "count"     in data
        assert "window_id" in data
        assert "regime"    in data
        assert isinstance(data["pool"], list)


class TestRegimeEndpoint:
    def test_regime_requires_auth(self, client):
        resp = client.get("/regime")
        assert resp.status_code == 403

    def test_regime_correct_secret(self, client):
        resp = client.get("/regime", headers={"X-Axiom-Secret": "test-secret-12345"})
        assert resp.status_code == 200


class TestAssessEndpoint:
    def test_assess_requires_auth(self, client):
        resp = client.post("/assess", json={"ticker": "NVDA"})
        assert resp.status_code == 403

    def test_assess_valid_ticker(self, client):
        resp = client.post(
            "/assess",
            json    = {"ticker": "NVDA"},
            headers = {"X-Axiom-Secret": "test-secret-12345"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ticker"]      == "NVDA"
        assert "risk_score"        in data
        assert "sizing_mult"       in data
        assert "hard_stops"        in data
        assert 0 <= data["risk_score"] <= 10
        assert 0 <= data["sizing_mult"] <= 1.0

    def test_assess_ticker_normalized_uppercase(self, client):
        resp = client.post(
            "/assess",
            json    = {"ticker": "nvda"},
            headers = {"X-Axiom-Secret": "test-secret-12345"},
        )
        assert resp.json()["ticker"] == "NVDA"


class TestAnchorEndpoint:
    def test_anchor_requires_auth(self, client):
        resp = client.get("/anchor")
        assert resp.status_code == 403

    def test_anchor_returns_list(self, client):
        resp = client.get("/anchor", headers={"X-Axiom-Secret": "test-secret-12345"})
        assert resp.status_code == 200
        data = resp.json()
        assert "anchor_stocks" in data
        assert isinstance(data["anchor_stocks"], list)
