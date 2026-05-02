"""
test_api.py — Alpha Execution API endpoint tests. All Alpaca calls mocked.
"""

import sys
import os
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

# conftest.py sets NEXUS_WEBHOOK_SECRET before this module loads — read it back.
# All setdefault calls here are no-ops if conftest already set the value.
os.environ.setdefault("NEXUS_WEBHOOK_SECRET", "x" * 64)
os.environ.setdefault("ALPHA_EXEC_DB_PATH",   tempfile.mktemp(suffix=".db"))
os.environ.setdefault("ALPACA_API_KEY",        "test-alpaca-key")
os.environ.setdefault("ALPACA_SECRET_KEY",     "test-alpaca-secret")
os.environ.setdefault("ALPHA_BUFFER_URL",      "http://localhost:8002")
os.environ.setdefault("TELEGRAM_BOT_TOKEN",    "9999:TEST")
os.environ.setdefault("AHMED_CHAT_ID",         "8573754783")
os.environ.setdefault("AXIOM_URL",             "http://localhost:8001")
os.environ.setdefault("AXIOM_SECRET",          "test-axiom-secret")

# SECRET must match whatever conftest set for NEXUS_WEBHOOK_SECRET.
SECRET  = os.environ["NEXUS_WEBHOOK_SECRET"]
HEADERS = {"X-Nexus-Secret": SECRET}

VALID_EXECUTE = {
    "ticker":            "NVDA",
    "direction":         "bullish",
    "pathway":           "P1",
    "weighted_score":    82.5,
    "agent_scores":      {"Cipher": 88, "Atlas": 82, "Sage": 76},
    "verdict":           "STRONG_GO",
    "sizing_mult":       1.0,
    "position_size_usd": 2000.0,
    "window_id":         "2026-04-10-0930",
    "echo_chamber":      False,
}


@pytest.fixture(scope="function")
def client():
    # Patch preflight, market hours, and contract resolution so tests run
    # without real Alpaca credentials or real options chain data.
    from contract_resolver import SpreadParams
    _mock_spread = SpreadParams(
        underlying="NVDA", direction="bullish", option_type="put",
        short_strike=880.0, long_strike=870.0, expiration_date="2026-05-16",
        target_dte=14, current_price=900.0, is_etf=False,
    )
    with patch("main._preflight_check", return_value=(True, "")), \
         patch("main._run_preflight_retry_loop"), \
         patch("main._is_market_hours", return_value=True), \
         patch("main.resolve_available_contract", return_value=_mock_spread), \
         patch("database.count_open_positions_alpaca", return_value=0):
        from main import app
        import main as _m
        _m._SERVICE_MODE = "active"
        from database import init_db
        init_db(os.environ["ALPHA_EXEC_DB_PATH"])
        with TestClient(app) as c:
            yield c


class TestHealth:
    def test_health_200(self, client):
        assert client.get("/health", headers=HEADERS).status_code == 200

    def test_health_says_alpha(self, client):
        assert "alpha-execution" in client.get("/health", headers=HEADERS).json()["service"]


class TestExecute:
    def test_rejects_no_secret(self, client):
        assert client.post("/execute", json=VALID_EXECUTE).status_code == 403

    def test_rejects_wrong_secret(self, client):
        assert client.post(
            "/execute", json=VALID_EXECUTE,
            headers={"X-Nexus-Secret": "wrong"}
        ).status_code == 403

    @patch("main.AlpacaClient")
    def test_rejects_when_no_price(self, MockAlpaca, client):
        inst = MockAlpaca.return_value
        inst.get_latest_price.return_value = None
        resp = client.post("/execute", json=VALID_EXECUTE, headers=HEADERS)
        assert resp.status_code == 503
        assert resp.json()["executed"] is False

    @patch("main.AlpacaClient")
    def test_successful_execution(self, MockAlpaca, client):
        inst = MockAlpaca.return_value
        inst.get_latest_price.return_value = 900.0
        inst.place_spread_order.return_value = {"id": "order-abc", "status": "accepted"}
        resp = client.post("/execute", json=VALID_EXECUTE, headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["executed"]     is True
        assert data["ticker"]       == "NVDA"
        assert data["direction"]    == "bullish"
        assert "position_id"        in data
        assert data["position_id"]  is not None

    @patch("main.AlpacaClient")
    def test_alpaca_failure_returns_503_no_db_record(self, MockAlpaca, client):
        """
        Adversarial fix #6: when Alpaca order placement fails, the endpoint must
        return 503 (not 200) and must NOT create a DB record. Phantom open positions
        from failed orders are eliminated — the DB is only written after confirmed execution.
        """
        inst = MockAlpaca.return_value
        inst.get_latest_price.return_value = 900.0
        inst.place_spread_order.side_effect = Exception("Alpaca down")
        resp = client.post(
            "/execute",
            json    = {**VALID_EXECUTE, "ticker": "AMD"},
            headers = HEADERS,
        )
        assert resp.status_code == 503
        data = resp.json()
        assert data["executed"]    is False
        assert data["position_id"] is None
        assert "Alpaca order placement failed" in data["reason"]

    @patch("main.AlpacaClient")
    def test_max_concurrent_limit(self, MockAlpaca, client):
        """After 10 open positions, further executions should be rejected."""
        inst = MockAlpaca.return_value
        inst.get_latest_price.return_value = 100.0
        inst.place_spread_order.return_value = {"id": "order-test", "status": "accepted"}

        # Open 10 positions (use unique tickers)
        tickers = [f"T{i:02d}" for i in range(10)]
        for t in tickers:
            client.post("/execute", json={**VALID_EXECUTE, "ticker": t}, headers=HEADERS)

        # 11th should be rejected
        resp = client.post("/execute", json={**VALID_EXECUTE, "ticker": "OVERFLOW"}, headers=HEADERS)
        assert resp.status_code == 429
        assert resp.json()["executed"] is False

    def test_invalid_direction_rejected(self, client):
        bad = {**VALID_EXECUTE, "direction": "sideways"}
        resp = client.post("/execute", json=bad, headers=HEADERS)
        assert resp.status_code == 422


class TestPositions:
    def test_requires_auth(self, client):
        assert client.get("/positions").status_code == 403

    def test_returns_positions_list(self, client):
        resp = client.get("/positions", headers=HEADERS)
        assert resp.status_code == 200
        assert "positions" in resp.json()
