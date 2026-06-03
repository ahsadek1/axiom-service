"""
Tests for Alert Broker — Layer 1.
Run: cd /Users/ahmedsadek/nexus/alert-broker && python3 -m pytest tests/ -v
"""
import sys
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Ensure alert-broker directory takes priority to prevent nexus/main.py shadowing broker main.py
_BROKER_DIR = str(Path(__file__).parent.parent)
if _BROKER_DIR not in sys.path:
    sys.path.insert(0, _BROKER_DIR)
else:
    # Move to front if already present
    sys.path.remove(_BROKER_DIR)
    sys.path.insert(0, _BROKER_DIR)

from dedup import DedupEngine, BatchEngine, AlertIn, DEDUP_WINDOW_S
from router import route_alert, _rate_limiter, _RateLimiter, _format_message


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Redirect SQLite DB to a temp file for each test."""
    import dedup as _dedup
    monkeypatch.setattr(_dedup, "DB_PATH", str(tmp_path / "test_alert_broker.db"))
    yield


@pytest.fixture()
def engine():
    return DedupEngine()


@pytest.fixture()
def rate_limiter():
    return _RateLimiter()


def _make_alert(level="WARNING", dedup_key="test_key", source="test-svc", targets=None):
    return AlertIn(
        source=source,
        level=level,
        title="Test alert",
        body="Test body",
        dedup_key=dedup_key,
        targets=targets or ["ahmed"],
    )


# ---------------------------------------------------------------------------
# T1: Dedup suppresses within 60s window
# ---------------------------------------------------------------------------

def test_dedup_suppresses_within_60s(engine):
    """Same dedup_key sent twice within 60s → second is duplicate."""
    assert not engine.is_duplicate("key1")
    engine.record_sent("key1")
    assert engine.is_duplicate("key1")


# ---------------------------------------------------------------------------
# T2: Dedup allows after window expires
# ---------------------------------------------------------------------------

def test_dedup_allows_after_window(engine, monkeypatch):
    """Same key at t=0 and t>60s → second allowed."""
    engine.record_sent("key2")
    assert engine.is_duplicate("key2")

    # Simulate time passing beyond dedup window
    import dedup as _dedup
    monkeypatch.setattr(_dedup, "DEDUP_WINDOW_S", 0)

    # Re-create engine to pick up new window
    engine2 = DedupEngine()
    assert not engine2.is_duplicate("key2")


# ---------------------------------------------------------------------------
# T3: CRITICAL bypasses batch — sent immediately
# ---------------------------------------------------------------------------

def test_critical_bypasses_batch():
    """CRITICAL alert calls flush_callback immediately, not after batch window."""
    received = []

    def cb(alerts):
        received.extend(alerts)

    be = BatchEngine(flush_callback=cb)
    alert = _make_alert(level="CRITICAL", dedup_key="critical_key")
    be.add(alert)

    # Should have been called synchronously (no sleep needed)
    assert len(received) == 1
    assert received[0].level == "CRITICAL"


# ---------------------------------------------------------------------------
# T4: WARNING alerts are batched (not sent immediately)
# ---------------------------------------------------------------------------

def test_warning_batched():
    """3 WARNING alerts in quick succession → 1 batched message (same source)."""
    received = []

    def cb(alerts):
        received.extend(alerts)

    be = BatchEngine(flush_callback=cb)
    for i in range(3):
        be.add(_make_alert(level="WARNING", dedup_key=f"warn_{i}", source="test-svc"))

    # Not yet flushed (batch window hasn't elapsed)
    assert len(received) == 0
    assert be.queue_depth() == 3

    # Force flush
    be._flush()
    # 3 same-source warnings → 1 combined alert
    assert len(received) == 1
    assert "3 alerts" in received[0].title or received[0].source == "test-svc"


# ---------------------------------------------------------------------------
# T5: Rate limit hard cap (10 per 5 min)
# ---------------------------------------------------------------------------

def test_rate_limit_hard_cap(rate_limiter):
    """11 sends to same target → first 10 allowed, 11th denied."""
    target = "ahmed_test"
    allowed = [rate_limiter.allow(target) for _ in range(11)]
    assert sum(allowed) == 10
    assert allowed[10] is False


# ---------------------------------------------------------------------------
# T6: Auth required (403 on missing/wrong secret)
# ---------------------------------------------------------------------------

def test_auth_required():
    """POST /alert with wrong secret → 403."""
    from fastapi.testclient import TestClient
    import broker as broker_main
    # Reinit engines for test
    broker_main.dedup_engine = DedupEngine()
    broker_main.batch_engine = BatchEngine(flush_callback=lambda a: None)

    client = TestClient(broker_main.app)
    resp = client.post(
        "/alert",
        json={"source": "test", "level": "INFO", "title": "test", "dedup_key": "k1"},
        headers={"X-Alert-Secret": "WRONG_SECRET"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# T7: alert_client fallback on broker down
# ---------------------------------------------------------------------------

def test_fallback_on_broker_down():
    """If broker returns 503, CRITICAL alert falls back to direct Telegram."""
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))
    import alert_client

    tg_calls = []

    def mock_post(url, **kwargs):
        if "telegram" in url:
            tg_calls.append(url)
            return MagicMock(status_code=200)
        return MagicMock(status_code=503)

    with patch("alert_client.requests.post", side_effect=mock_post):
        alert_client.send_alert(
            source="test-svc",
            level="CRITICAL",
            title="Broker down test",
            dedup_key="broker_down_test",
        )
        time.sleep(0.3)

    assert len(tg_calls) >= 1, "Expected Telegram fallback call"


# ---------------------------------------------------------------------------
# T8: Health endpoint returns accurate stats
# ---------------------------------------------------------------------------

def test_health_endpoint():
    """GET /health returns alerts_today and suppressed_today."""
    from fastapi.testclient import TestClient
    import broker as broker_main

    broker_main.dedup_engine = DedupEngine()
    broker_main.batch_engine = BatchEngine(flush_callback=lambda a: None)

    client = TestClient(broker_main.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "alerts_today" in data
    assert "suppressed_today" in data
    assert "queue_depth" in data
    assert data["status"] == "ok"


# ---------------------------------------------------------------------------
# T9: Suppression increments correctly
# ---------------------------------------------------------------------------

def test_suppression_counter(engine):
    """is_duplicate + increment_suppressed → suppressed count increments."""
    engine.record_sent("dup_key")
    engine.increment_suppressed()
    engine.increment_suppressed()
    stats = engine.get_stats()
    assert stats["suppressed"] >= 2


# ---------------------------------------------------------------------------
# T10: format_message includes level and source
# ---------------------------------------------------------------------------

def test_format_message_includes_level_and_source():
    """Formatted message contains level emoji and source."""
    msg = _format_message("nexus-integrity", "CRITICAL", "OMNI silent", "Detail here")
    assert "CRITICAL" in msg
    assert "nexus-integrity" in msg
    assert "OMNI silent" in msg
    assert "🚨" in msg
