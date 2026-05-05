"""
tests/test_cipher.py — Cipher Agent Test Suite

10 tests covering: happy path, threshold enforcement, duplicate window guard,
ORACLE timeout isolation, buffer retry, auth, brain failure, and scoring rules.
"""

import json
import sqlite3
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def set_env(tmp_path, monkeypatch):
    """Set required env vars before each test."""
    db = str(tmp_path / "cipher_test.db")
    monkeypatch.setenv("NEXUS_SECRET", "test-nexus-secret")
    monkeypatch.setenv("NEXUS_PRIME_SECRET", "test-prime-secret")
    monkeypatch.setenv("ORACLE_SECRET", "test-oracle-secret")
    monkeypatch.setenv("ORACLE_URL", "http://localhost:8007")
    monkeypatch.setenv("ALPHA_BUFFER_URL", "http://localhost:8002")
    monkeypatch.setenv("PRIME_BUFFER_URL", "http://localhost:8003")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("DB_PATH", db)
    return db


@pytest.fixture
def client(set_env):
    """Create test client with fresh app."""
    from main import app
    with TestClient(app) as c:
        yield c


def _pool_payload(window_id: str = "test-2026-001", tickers: list = None) -> dict:
    """Build a minimal pool payload."""
    return {
        "pool": tickers or ["AAPL", "NVDA"],
        "count": len(tickers or ["AAPL", "NVDA"]),
        "window_id": window_id,
        "updated_at": "2026-04-13T10:00:00-04:00",
        "market_open": True,
        "regime": {
            "classification": "NORMAL",
            "strategy_bias": "Balanced",
            "alpha_credit_allowed": True,
            "alpha_debit_allowed": True,
            "prime_allowed": True,
        },
        "coherence_summary": [],
        "echo_chamber_risk": [],
        "oracle_warmed": True,
    }


# ── Test 1: Health endpoint returns 200 ──────────────────────────────────────
def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["agent"] == "Cipher"


# ── Test 2: Invalid auth returns 401 ─────────────────────────────────────────
def test_invalid_auth_returns_401(client):
    resp = client.post(
        "/receive-pool",
        json=_pool_payload(),
        headers={"X-Nexus-Secret": "wrong-secret"},
    )
    assert resp.status_code == 401


# ── Test 3: Missing auth returns 401 ─────────────────────────────────────────
def test_missing_auth_returns_401(client):
    resp = client.post("/receive-pool", json=_pool_payload())
    assert resp.status_code == 401


# ── Test 4: Valid pool accepted immediately (< 500ms) ────────────────────────
@patch("main.threading.Thread")
def test_pool_accepted_fast(mock_thread, client):
    mock_thread.return_value = MagicMock(start=MagicMock())
    start = time.time()
    resp = client.post(
        "/receive-pool",
        json=_pool_payload(),
        headers={"X-Nexus-Secret": "test-nexus-secret"},
    )
    elapsed = time.time() - start
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert elapsed < 0.5, f"Response too slow: {elapsed:.2f}s"


# ── Test 5: Duplicate window returns 'duplicate', no analysis ────────────────
@patch("main.threading.Thread")
def test_duplicate_window_skipped(mock_thread, client):
    mock_thread.return_value = MagicMock(start=MagicMock())
    headers = {"X-Nexus-Secret": "test-nexus-secret"}

    # First push
    r1 = client.post("/receive-pool", json=_pool_payload("window-dup-001"), headers=headers)
    assert r1.status_code == 200
    assert r1.json()["status"] == "accepted"

    # Second push same window_id
    r2 = client.post("/receive-pool", json=_pool_payload("window-dup-001"), headers=headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate"

    # Thread only started once
    assert mock_thread.call_count == 1


# ── Test 6: ORACLE timeout skips ticker, others analyzed ─────────────────────
def test_oracle_timeout_isolation(set_env):
    """Confirm _analyze_pool skips ORACLE-timed-out tickers without crashing."""
    analyze_call_count = [0]

    def mock_fetch(ticker, url, headers):
        # AAPL times out (returns None), NVDA succeeds
        return None if ticker == "AAPL" else {"price": {"last": 900}}

    def mock_analyze_fn(ticker, context, regime, api_key):
        analyze_call_count[0] += 1
        return {"direction": "bullish", "score": 72.0, "reasoning": "Strong IV setup"}

    with patch("main.fetch_context", side_effect=mock_fetch), \
         patch("main.analyze", side_effect=mock_analyze_fn), \
         patch("main.submit_to_alpha", return_value=True), \
         patch("main.submit_to_prime", return_value=True), \
         patch("main.record_pick"), \
         patch("main.complete_window"):

        from main import _analyze_pool
        payload = _pool_payload("window-oracle-test", ["AAPL", "NVDA"])
        _analyze_pool(payload)
        # AAPL skipped (ORACLE None), only NVDA analyzed
        assert analyze_call_count[0] == 1


# ── Test 7: Score below Alpha threshold — not submitted to Alpha ──────────────
def test_score_below_alpha_threshold_not_submitted():
    from buffer_client import submit_to_alpha
    with patch("buffer_client.requests.post") as mock_post:
        result = submit_to_alpha(
            "AAPL", "Cipher", "bullish", 55.0, "Weak setup",
            "http://localhost:8002", {"X-Nexus-Secret": "secret"},
        )
        assert result is False
        mock_post.assert_not_called()


# ── Test 8: Score below Prime threshold — not submitted to Prime ──────────────
def test_score_below_prime_threshold_not_submitted():
    from buffer_client import submit_to_prime
    with patch("buffer_client.requests.post") as mock_post:
        result = submit_to_prime(
            "NVDA", "Cipher", "bullish", 60.0, "Below prime floor",
            "http://localhost:8003", {"X-Nexus-Prime-Secret": "secret"},
        )
        assert result is False
        mock_post.assert_not_called()


# ── Test 9: Buffer submission failure triggers single retry ──────────────────
def test_buffer_retry_on_failure():
    from buffer_client import submit_to_alpha
    call_count = [0]

    def mock_post(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise Exception("Connection refused")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        return mock_resp

    with patch("buffer_client.requests.post", side_effect=mock_post), \
         patch("buffer_client.time.sleep"):  # don't actually wait 30s
        result = submit_to_alpha(
            "AAPL", "Cipher", "bullish", 72.0, "Good IV",
            "http://localhost:8002", {"X-Nexus-Secret": "secret"},
        )
    assert result is True
    assert call_count[0] == 2  # Failed once, retried once


# ── Test 10: Brain failure counter increments and triggers alert at threshold ─
def test_brain_failure_alert():
    with patch("main.fetch_context", return_value={"price": {}}), \
         patch("main.analyze", return_value=None), \
         patch("main.alert_brain_down") as mock_alert, \
         patch("main.record_pick"), \
         patch("main.complete_window"):

        from main import _analyze_pool, _settings
        import main
        main._consecutive_brain_failures = 2  # Already 2 failures

        payload = _pool_payload("window-brain-test", ["AAPL"])
        _analyze_pool(payload)

        # Third failure should trigger alert and reset counter to 0
        mock_alert.assert_called_once()
        assert main._consecutive_brain_failures == 0  # reset after alert fires
