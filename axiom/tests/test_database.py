"""
test_database.py — Unit tests for Axiom database layer.

Uses temporary SQLite databases — never touches production data.
"""

import sys
import os
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from database import (
    init_db,
    save_pool_snapshot,
    load_last_pool,
    save_anchor_stocks,
    load_anchor_stocks,
    log_agent_push,
    update_agent_health,
    get_agent_health,
)


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary database for each test."""
    db_path = str(tmp_path / "test_axiom.db")
    init_db(db_path)
    return db_path


class TestInitDb:
    def test_creates_database(self, tmp_path):
        db_path = str(tmp_path / "new.db")
        init_db(db_path)
        assert os.path.exists(db_path)

    def test_idempotent(self, tmp_db):
        """Calling init_db twice should not raise."""
        init_db(tmp_db)


class TestPoolSnapshots:
    def test_save_and_load_pool(self, tmp_db):
        tickers = ["NVDA", "AAPL", "MSFT"]
        regime  = {"classification": "NORMAL", "vix": 18.2}
        save_pool_snapshot(tmp_db, "2026-04-10-1415", tickers, regime)

        result = load_last_pool(tmp_db)
        assert result is not None
        assert result["tickers"] == tickers
        assert result["pool_size"] == 3
        assert result["window_id"] == "2026-04-10-1415"
        assert result["regime"]["classification"] == "NORMAL"

    def test_upsert_same_window(self, tmp_db):
        """Saving same window_id twice should update, not duplicate."""
        save_pool_snapshot(tmp_db, "2026-04-10-1415", ["NVDA"], {"vix": 18})
        save_pool_snapshot(tmp_db, "2026-04-10-1415", ["AAPL", "MSFT"], {"vix": 19})

        result = load_last_pool(tmp_db)
        assert result["tickers"] == ["AAPL", "MSFT"]
        assert result["pool_size"] == 2

    def test_load_returns_none_when_empty(self, tmp_db):
        assert load_last_pool(tmp_db) is None


class TestAnchorStocks:
    def test_save_and_load_anchor_stocks(self, tmp_db):
        tickers = ["NVDA", "AAPL", "TSLA"]
        save_anchor_stocks(tmp_db, tickers, "2026-04-10", "morning")

        result = load_anchor_stocks(tmp_db, "2026-04-10")
        assert sorted(result) == sorted(tickers)

    def test_load_empty_date(self, tmp_db):
        result = load_anchor_stocks(tmp_db, "2099-01-01")
        assert result == []

    def test_replaces_on_same_run_type(self, tmp_db):
        save_anchor_stocks(tmp_db, ["NVDA", "AAPL"], "2026-04-10", "morning")
        save_anchor_stocks(tmp_db, ["MSFT", "META", "GOOGL"], "2026-04-10", "morning")

        result = load_anchor_stocks(tmp_db, "2026-04-10")
        assert "NVDA" not in result
        assert "MSFT" in result


class TestAgentHealth:
    def test_success_sets_healthy(self, tmp_db):
        count = update_agent_health(tmp_db, "Cipher", success=True)
        assert count == 0

        health = get_agent_health(tmp_db)
        assert health["Cipher"]["status"] == "healthy"
        assert health["Cipher"]["consecutive_failures"] == 0

    def test_failure_increments_count(self, tmp_db):
        update_agent_health(tmp_db, "Sage", success=False)
        update_agent_health(tmp_db, "Sage", success=False)
        count = update_agent_health(tmp_db, "Sage", success=False)
        assert count == 3

        health = get_agent_health(tmp_db)
        assert health["Sage"]["status"] == "down"
        assert health["Sage"]["consecutive_failures"] == 3

    def test_success_resets_failures(self, tmp_db):
        update_agent_health(tmp_db, "Atlas", success=False)
        update_agent_health(tmp_db, "Atlas", success=False)
        update_agent_health(tmp_db, "Atlas", success=True)

        count = update_agent_health(tmp_db, "Atlas", success=True)
        assert count == 0

        health = get_agent_health(tmp_db)
        assert health["Atlas"]["consecutive_failures"] == 0
        assert health["Atlas"]["status"] == "healthy"


class TestAgentPushLog:
    def test_log_success(self, tmp_db):
        """Logging a successful push should not raise."""
        log_agent_push(tmp_db, "2026-04-10-1415", "Cipher", "success", 245)

    def test_log_timeout(self, tmp_db):
        log_agent_push(tmp_db, "2026-04-10-1415", "Sage", "timeout", 5001)

    def test_log_error(self, tmp_db):
        log_agent_push(tmp_db, "2026-04-10-1415", "Atlas", "error", None)
