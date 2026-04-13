"""
test_api.py — Prime Execution API tests. All Alpaca calls mocked.
"""

import sys
import os
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

os.environ.setdefault("NEXUS_PRIME_SECRET",  "test-prime-exec-secret")
os.environ.setdefault("PRIME_EXEC_DB_PATH",   tempfile.mktemp(suffix=".db"))
os.environ.setdefault("ALPACA_API_KEY",        "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY",     "test-secret")
os.environ.setdefault("PRIME_BUFFER_URL",      "http://localhost:8003")
os.environ.setdefault("TELEGRAM_BOT_TOKEN",    "9999:TEST")
os.environ.setdefault("AHMED_CHAT_ID",         "8573754783")

SECRET  = "test-prime-exec-secret"
HEADERS = {"X-Nexus-Prime-Secret": SECRET}

VALID_EXECUTE = {
    "ticker":            "TSLA",
    "direction":         "bullish",
    "pathway":           "P1",
    "weighted_score":    81.0,
    "agent_scores":      {"Cipher": 85, "Atlas": 79, "Sage": 76},
    "verdict":           "GO",
    "sizing_mult":       1.0,
    "position_size_usd": 2000.0,
    "window_id":         "2026-04-10-1000",
    "echo_chamber":      False,
}


@pytest.fixture(scope="module")
def client():
    from main import app
    from database import init_db
    init_db(os.environ["PRIME_EXEC_DB_PATH"])
    with TestClient(app) as c:
        yield c


class TestHealth:
    def test_health_200(self, client):
        assert client.get("/health").status_code == 200

    def test_health_says_prime(self, client):
        assert "prime-execution" in client.get("/health").json()["service"]

    def test_health_has_reconcile_field(self, client):
        assert "execution_paused" in client.get("/health").json()


class TestExecute:
    def test_alpha_secret_rejected(self, client):
        """Prime's endpoint must reject Alpha's secret."""
        resp = client.post("/execute", json=VALID_EXECUTE,
                           headers={"X-Nexus-Prime-Secret": "test-alpha-secret"})
        assert resp.status_code == 403

    @patch("main.AlpacaClient")
    def test_successful_execution(self, MockAlpaca, client):
        inst = MockAlpaca.return_value
        inst.get_latest_price.return_value = 175.0
        inst.place_order.return_value = {"id": "order-xyz", "status": "accepted"}
        resp = client.post("/execute", json=VALID_EXECUTE, headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["executed"]     is True
        assert data["ticker"]       == "TSLA"
        assert data["side"]         == "buy"
        assert data["position_id"]  is not None
        assert data["shares"]       > 0

    @patch("main.AlpacaClient")
    def test_bullish_places_buy(self, MockAlpaca, client):
        inst = MockAlpaca.return_value
        inst.get_latest_price.return_value = 200.0
        inst.place_order.return_value = {"id": "buy-order", "status": "accepted"}
        resp = client.post("/execute", json={**VALID_EXECUTE, "ticker": "NVDA"}, headers=HEADERS)
        assert resp.json()["side"] == "buy"

    @patch("main.AlpacaClient")
    def test_bearish_places_sell(self, MockAlpaca, client):
        inst = MockAlpaca.return_value
        inst.get_latest_price.return_value = 200.0
        inst.place_order.return_value = {"id": "sell-order", "status": "accepted"}
        resp = client.post(
            "/execute",
            json    = {**VALID_EXECUTE, "ticker": "AMZN", "direction": "bearish"},
            headers = HEADERS,
        )
        assert resp.json()["side"] == "sell"

    @patch("main.AlpacaClient")
    def test_alpaca_failure_returns_503_no_phantom_position(self, MockAlpaca, client):
        """
        Adversarial fix #3: Alpaca order failure must return 503 and must NOT
        create a DB record. Phantom open positions from failed orders eliminated.
        """
        inst = MockAlpaca.return_value
        inst.get_latest_price.return_value = 150.0
        inst.place_order.side_effect = Exception("Alpaca unavailable")
        resp = client.post(
            "/execute",
            json    = {**VALID_EXECUTE, "ticker": "GOOG"},
            headers = HEADERS,
        )
        assert resp.status_code == 503
        data = resp.json()
        assert data["executed"]    is False
        assert data["position_id"] is None
        assert "Alpaca order placement failed" in data["reason"]

    def test_reconciler_pause_blocks_execution(self, client):
        from main import app_state
        app_state["execution_paused"] = True
        try:
            resp = client.post("/execute", json=VALID_EXECUTE, headers=HEADERS)
            assert resp.status_code == 503
            assert resp.json()["executed"] is False
        finally:
            app_state["execution_paused"] = False

    def test_invalid_direction_422(self, client):
        resp = client.post("/execute",
                           json={**VALID_EXECUTE, "direction": "sideways"},
                           headers=HEADERS)
        assert resp.status_code == 422


class TestTechnicalStop:
    def test_requires_auth(self, client):
        assert client.post("/technical-stop", json={"position_id": 1, "reason": "test"}).status_code == 403

    @patch("main.AlpacaClient")
    def test_flags_open_position(self, MockAlpaca, client):
        # First create a position
        inst = MockAlpaca.return_value
        inst.get_latest_price.return_value = 100.0
        inst.place_order.return_value = {"id": "o1"}
        resp = client.post("/execute",
                           json={**VALID_EXECUTE, "ticker": "TSLA2"},
                           headers=HEADERS)
        pos_id = resp.json()["position_id"]

        # Flag it for technical stop
        flag_resp = client.post("/technical-stop",
                                json={"position_id": pos_id, "reason": "breakdown"},
                                headers=HEADERS)
        assert flag_resp.status_code == 200
        assert flag_resp.json()["flagged"] is True

    def test_404_on_unknown_position(self, client):
        resp = client.post("/technical-stop",
                           json={"position_id": 99999, "reason": "test"},
                           headers=HEADERS)
        assert resp.status_code == 404


class TestPositions:
    def test_requires_auth(self, client):
        assert client.get("/positions").status_code == 403

    def test_returns_positions(self, client):
        data = client.get("/positions", headers=HEADERS).json()
        assert "positions" in data
        assert "execution_paused" in data
