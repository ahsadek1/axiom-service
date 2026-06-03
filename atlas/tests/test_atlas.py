"""
tests/test_atlas.py — Atlas Agent Test Suite
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def set_env(tmp_path, monkeypatch):
    db = str(tmp_path / "atlas_test.db")
    monkeypatch.setenv("NEXUS_SECRET", "test-nexus-secret")
    monkeypatch.setenv("NEXUS_PRIME_SECRET", "test-prime-secret")
    monkeypatch.setenv("ORACLE_SECRET", "test-oracle-secret")
    monkeypatch.setenv("ORACLE_URL", "http://localhost:8007")
    monkeypatch.setenv("ALPHA_BUFFER_URL", "http://localhost:8002")
    monkeypatch.setenv("PRIME_BUFFER_URL", "http://localhost:8003")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("DB_PATH", db)
    return db


@pytest.fixture
def client(set_env):
    from main import app
    with TestClient(app) as c:
        yield c


def _pool(window_id="test-atlas-001", tickers=None, echo_risk=None):
    return {
        "pool": tickers or ["NVDA", "AAPL"],
        "count": len(tickers or ["NVDA", "AAPL"]),
        "window_id": window_id,
        "updated_at": "2026-04-13T10:00:00-04:00",
        "market_open": True,
        "regime": {"classification": "NORMAL", "strategy_bias": "Balanced",
                   "alpha_credit_allowed": True, "alpha_debit_allowed": True, "prime_allowed": True},
        "coherence_summary": [],
        "echo_chamber_risk": echo_risk or [],
        "oracle_warmed": True,
    }


# Test 1: Health endpoint
def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["agent"] == "Atlas"


# Test 2: Invalid auth → 401
def test_invalid_auth(client):
    resp = client.post("/receive-pool", json=_pool(), headers={"X-Nexus-Secret": "wrong"})
    assert resp.status_code == 401


# Test 3: Missing auth → 401
def test_missing_auth(client):
    resp = client.post("/receive-pool", json=_pool())
    assert resp.status_code == 401


# Test 4: Pool accepted fast (< 500ms)
@patch("main.threading.Thread")
def test_pool_accepted_fast(mock_thread, client):
    mock_thread.return_value = MagicMock(start=MagicMock())
    start = time.time()
    resp = client.post("/receive-pool", json=_pool(), headers={"X-Nexus-Secret": "test-nexus-secret"})
    assert resp.status_code == 200
    assert time.time() - start < 0.5


# Test 5: Duplicate window skipped
@patch("main.threading.Thread")
def test_duplicate_window(mock_thread, client):
    mock_thread.return_value = MagicMock(start=MagicMock())
    headers = {"X-Nexus-Secret": "test-nexus-secret"}
    r1 = client.post("/receive-pool", json=_pool("dup-001"), headers=headers)
    assert r1.json()["status"] == "accepted"
    r2 = client.post("/receive-pool", json=_pool("dup-001"), headers=headers)
    assert r2.json()["status"] == "duplicate"
    assert mock_thread.call_count == 1


# Test 6: ORACLE timeout skips ticker
def test_oracle_timeout_skips_ticker():
    analyze_calls = [0]

    def mock_fetch(ticker, url, headers):
        return None if ticker == "NVDA" else {"price": {"last": 200}}

    def mock_analyze(ticker, context, regime, echo, api_key):
        analyze_calls[0] += 1
        return {"direction": "bullish", "score": 70.0, "reasoning": "Strong breakout"}

    with patch("main.fetch_context", side_effect=mock_fetch), \
         patch("main.analyze", side_effect=mock_analyze), \
         patch("main.submit_to_alpha", return_value=True), \
         patch("main.submit_to_prime", return_value=True):
        from main import _analyze_pool
        _analyze_pool(_pool("oracle-test", ["NVDA", "AAPL"]))
        assert analyze_calls[0] == 1  # Only AAPL analyzed (NVDA timed out)


# Test 7: Echo chamber discount applied in prompt
def test_echo_chamber_flag_in_prompt():
    from analyzer import build_prompt
    prompt = build_prompt("AAPL", {}, {"classification": "NORMAL"}, ["AAPL"])
    assert "echo chamber" in prompt.lower() or "over-watched" in prompt.lower()


# Test 8: Score below Alpha threshold → not submitted
def test_below_alpha_threshold_not_submitted():
    from buffer_client import submit_to_alpha
    with patch("buffer_client.requests.post") as mock_post:
        result = submit_to_alpha("NVDA", "Atlas", "bullish", 50.0, "Weak",
                                  "http://localhost:8002", {"X-Nexus-Secret": "s"})
        assert result is False
        mock_post.assert_not_called()


# Test 9: Buffer retry on failure
def test_buffer_retry():
    from buffer_client import submit_to_alpha
    calls = [0]

    def mock_post(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise Exception("Timeout")
        m = MagicMock()
        m.status_code = 200
        return m

    with patch("buffer_client.requests.post", side_effect=mock_post), \
         patch("buffer_client.time.sleep"):
        result = submit_to_alpha("NVDA", "Atlas", "bullish", 75.0, "Breakout",
                                  "http://localhost:8002", {"X-Nexus-Secret": "s"})
    assert result is True
    assert calls[0] == 2


# Test 10: Brain failure alert at threshold
def test_brain_failure_alert_at_threshold():
    with patch("main.fetch_context", return_value={"price": {}}), \
         patch("main.analyze", return_value=None), \
         patch("main.alert_brain_down") as mock_alert, \
         patch("main.complete_window"):
        import main
        main._consecutive_brain_failures = 2
        main._analyze_pool(_pool("brain-test", ["AAPL"]))
        mock_alert.assert_called_once()
