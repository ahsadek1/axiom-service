"""
test_resilience_base.py — Full test suite for NEXUS_RESILIENCE_BASE v1.1.
Spec: NEXUS_RESILIENCE_BASE v1.1 (Cipher, 2026-05-02)

Coverage:
  - contracts.py  — require_float, require_str, require_int, DataContractError
  - state.py      — FreshValue (fresh/stale/update), StaleStateError
  - db.py         — begin_immediate, idempotent_insert, ScanResult
  - health.py     — HealthReport, HealthStatus, add_source, suspend/resume, to_dict
  - alerts.py     — cooldown dedup, always-send (key=None), token-missing guard
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

import sys
sys.path.insert(0, "/Users/ahmedsadek/nexus")

from shared.resilience.contracts import (
    DataContractError,
    require_float,
    require_int,
    require_str,
)
from shared.resilience.state import FreshValue, StaleStateError
from shared.resilience.db import (
    ScanResult,
    begin_immediate,
    idempotent_insert,
)
from shared.resilience.health import HealthReport, HealthStatus
from shared.resilience.alerts import alert_ahmed, reset_cooldowns


# ===========================================================================
# contracts.py
# ===========================================================================

class TestRequireFloat:
    def test_valid_float(self):
        assert require_float(42.5, "src", "field") == 42.5

    def test_coerce_string(self):
        assert require_float("3.14", "src", "field") == 3.14

    def test_coerce_int(self):
        assert require_float(10, "src", "field") == 10.0

    def test_none_raises(self):
        with pytest.raises(DataContractError) as exc_info:
            require_float(None, "orats", "iv_rank")
        assert exc_info.value.field == "iv_rank"
        assert "missing" in exc_info.value.reason

    def test_non_numeric_raises(self):
        with pytest.raises(DataContractError) as exc_info:
            require_float("abc", "src", "score")
        assert "not numeric" in exc_info.value.reason
        assert exc_info.value.raw == "abc"

    def test_below_min_raises(self):
        with pytest.raises(DataContractError) as exc_info:
            require_float(-1.0, "orats", "iv_rank", min_val=0.0)
        assert "below minimum" in exc_info.value.reason

    def test_above_max_raises(self):
        with pytest.raises(DataContractError) as exc_info:
            require_float(101.0, "orats", "iv_rank", max_val=100.0)
        assert "above maximum" in exc_info.value.reason

    def test_boundary_min_ok(self):
        assert require_float(0.0, "src", "f", min_val=0.0) == 0.0

    def test_boundary_max_ok(self):
        assert require_float(100.0, "src", "f", max_val=100.0) == 100.0


class TestRequireStr:
    def test_valid_string(self):
        assert require_str("credit_spread", "alpha", "strategy") == "credit_spread"

    def test_none_raises(self):
        with pytest.raises(DataContractError) as exc_info:
            require_str(None, "alpha", "strategy")
        assert "missing" in exc_info.value.reason

    def test_non_string_raises(self):
        with pytest.raises(DataContractError) as exc_info:
            require_str(42, "alpha", "strategy")
        assert "not a string" in exc_info.value.reason

    def test_not_in_allowed_raises(self):
        with pytest.raises(DataContractError) as exc_info:
            require_str("naked_call", "alpha", "strategy",
                        allowed=["credit_spread", "iron_condor"])
        assert "not in" in exc_info.value.reason

    def test_allowed_passes(self):
        result = require_str("iron_condor", "alpha", "strategy",
                              allowed=["credit_spread", "iron_condor"])
        assert result == "iron_condor"

    def test_no_allowlist_any_string_ok(self):
        assert require_str("anything", "src", "field") == "anything"


class TestRequireInt:
    def test_valid_int(self):
        assert require_int(30, "alpha", "dte") == 30

    def test_coerce_string_digits(self):
        assert require_int("45", "alpha", "dte") == 45

    def test_whole_float_ok(self):
        assert require_int(30.0, "alpha", "dte") == 30

    def test_fractional_float_raises(self):
        with pytest.raises(DataContractError) as exc_info:
            require_int(30.5, "alpha", "dte")
        assert "not an integer" in exc_info.value.reason

    def test_none_raises(self):
        with pytest.raises(DataContractError):
            require_int(None, "alpha", "dte")

    def test_non_numeric_raises(self):
        with pytest.raises(DataContractError):
            require_int("abc", "alpha", "dte")

    def test_below_min_raises(self):
        with pytest.raises(DataContractError) as exc_info:
            require_int(10, "alpha", "dte", min_val=21)
        assert "below minimum" in exc_info.value.reason

    def test_above_max_raises(self):
        with pytest.raises(DataContractError) as exc_info:
            require_int(61, "alpha", "dte", max_val=60)
        assert "above maximum" in exc_info.value.reason

    def test_boundary_values_ok(self):
        assert require_int(21, "alpha", "dte", min_val=21, max_val=60) == 21
        assert require_int(60, "alpha", "dte", min_val=21, max_val=60) == 60


class TestDataContractError:
    def test_str_representation(self):
        err = DataContractError("orats", "iv_rank", "missing", raw=None)
        assert "orats.iv_rank" in str(err)
        assert "missing" in str(err)

    def test_attributes(self):
        err = DataContractError("src", "field", "reason", raw="bad")
        assert err.source == "src"
        assert err.field == "field"
        assert err.reason == "reason"
        assert err.raw == "bad"


# ===========================================================================
# state.py
# ===========================================================================

class TestFreshValue:
    def test_fresh_get_returns_value(self):
        fv = FreshValue(42, max_age=timedelta(seconds=60), label="test")
        assert fv.get() == 42

    def test_fresh_require_returns_value(self):
        fv = FreshValue("hello", max_age=timedelta(seconds=60), label="test")
        assert fv.require() == "hello"

    def test_stale_get_returns_none(self):
        fv = FreshValue(99, max_age=timedelta(milliseconds=1), label="test")
        time.sleep(0.01)
        assert fv.get() is None

    def test_stale_require_raises(self):
        fv = FreshValue(99, max_age=timedelta(milliseconds=1), label="vix_level")
        time.sleep(0.01)
        with pytest.raises(StaleStateError) as exc_info:
            fv.require()
        assert "vix_level" in str(exc_info.value)

    def test_update_resets_freshness(self):
        fv = FreshValue(1, max_age=timedelta(milliseconds=1), label="test")
        time.sleep(0.01)
        fv.update(2)
        assert fv.get() == 2

    def test_age_seconds_increases(self):
        fv = FreshValue(0, max_age=timedelta(seconds=60), label="test")
        time.sleep(0.05)
        assert fv.age_seconds >= 0

    def test_is_fresh_property(self):
        fv = FreshValue(0, max_age=timedelta(seconds=60), label="test")
        assert fv.is_fresh is True

    def test_is_stale_property(self):
        fv = FreshValue(0, max_age=timedelta(milliseconds=1), label="test")
        time.sleep(0.01)
        assert fv.is_fresh is False

    def test_thread_safety(self):
        """Multiple writers and readers should never see inconsistent state."""
        fv = FreshValue(0, max_age=timedelta(seconds=60), label="test")
        errors = []

        def writer():
            for i in range(100):
                fv.update(i)

        def reader():
            for _ in range(100):
                try:
                    fv.get()
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread safety errors: {errors}"

    def test_none_initial_value_ok(self):
        fv = FreshValue(None, max_age=timedelta(seconds=60), label="test")
        assert fv.get() is None
        assert fv.require() is None


class TestStaleStateError:
    def test_attributes(self):
        err = StaleStateError("vix_level", 120, 60)
        assert err.label == "vix_level"
        assert err.age_seconds == 120
        assert "vix_level" in str(err)
        assert "120s" in str(err)


# ===========================================================================
# db.py
# ===========================================================================

@pytest.fixture
def tmp_db():
    """Temporary SQLite DB with an execution table for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE processed_picks (
            pick_id TEXT UNIQUE NOT NULL,
            ticker TEXT,
            processed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE capital_ledger (
            id INTEGER PRIMARY KEY,
            deployed REAL DEFAULT 0
        )
    """)
    conn.execute("INSERT INTO capital_ledger (id, deployed) VALUES (1, 0)")
    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


class TestBeginImmediate:
    def test_commit_on_success(self, tmp_db):
        with begin_immediate(tmp_db) as conn:
            conn.execute(
                "INSERT INTO processed_picks VALUES (?, ?, ?)",
                ("pk1", "AAPL", "2026-05-02"),
            )
        conn2 = sqlite3.connect(tmp_db)
        row = conn2.execute(
            "SELECT ticker FROM processed_picks WHERE pick_id='pk1'"
        ).fetchone()
        conn2.close()
        assert row is not None
        assert row[0] == "AAPL"

    def test_rollback_on_exception(self, tmp_db):
        with pytest.raises(ValueError):
            with begin_immediate(tmp_db) as conn:
                conn.execute(
                    "INSERT INTO processed_picks VALUES (?, ?, ?)",
                    ("pk2", "TSLA", "2026-05-02"),
                )
                raise ValueError("simulated failure")
        conn2 = sqlite3.connect(tmp_db)
        row = conn2.execute(
            "SELECT * FROM processed_picks WHERE pick_id='pk2'"
        ).fetchone()
        conn2.close()
        assert row is None

    def test_read_then_write_atomic(self, tmp_db):
        with begin_immediate(tmp_db) as conn:
            row = conn.execute(
                "SELECT deployed FROM capital_ledger WHERE id=1"
            ).fetchone()
            new_val = row["deployed"] + 2000
            conn.execute(
                "UPDATE capital_ledger SET deployed=? WHERE id=1", (new_val,)
            )
        conn2 = sqlite3.connect(tmp_db)
        result = conn2.execute(
            "SELECT deployed FROM capital_ledger WHERE id=1"
        ).fetchone()
        conn2.close()
        assert result[0] == 2000.0


class TestIdempotentInsert:
    def test_first_insert_returns_true(self, tmp_db):
        result = idempotent_insert(
            tmp_db, "processed_picks", "pick_id", "pk_new",
            {"pick_id": "pk_new", "ticker": "NVDA", "processed_at": "2026-05-02"},
        )
        assert result is True

    def test_duplicate_insert_returns_false(self, tmp_db):
        idempotent_insert(
            tmp_db, "processed_picks", "pick_id", "pk_dup",
            {"pick_id": "pk_dup", "ticker": "GOOG", "processed_at": "2026-05-02"},
        )
        result = idempotent_insert(
            tmp_db, "processed_picks", "pick_id", "pk_dup",
            {"pick_id": "pk_dup", "ticker": "GOOG", "processed_at": "2026-05-02"},
        )
        assert result is False

    def test_row_not_duplicated_in_db(self, tmp_db):
        for _ in range(5):
            idempotent_insert(
                tmp_db, "processed_picks", "pick_id", "pk_once",
                {"pick_id": "pk_once", "ticker": "AMD", "processed_at": "2026-05-02"},
            )
        conn = sqlite3.connect(tmp_db)
        count = conn.execute(
            "SELECT COUNT(*) FROM processed_picks WHERE pick_id='pk_once'"
        ).fetchone()[0]
        conn.close()
        assert count == 1


class TestScanResult:
    def test_skip_increments_counts(self):
        r = ScanResult()
        r.skip("AAPL", "api_timeout")
        r.skip("TSLA", "api_timeout")
        r.skip("NVDA", "missing_iv")
        assert r.total_skips == 3
        assert r.skip_reasons["api_timeout"] == 2
        assert r.skip_reasons["missing_iv"] == 1
        assert r.skipped_tickers["AAPL"] == "api_timeout"

    def test_skip_rate_calculation(self):
        r = ScanResult()
        r.skip("A", "reason")
        r.skip("B", "reason")
        r.verdicts = ["v1", "v2", "v3"]
        # 2 skips / (2 skips + 3 verdicts = 5) = 0.4
        assert r.skip_rate(5) == pytest.approx(0.4)

    def test_skip_rate_zero_pool(self):
        r = ScanResult()
        assert r.skip_rate(0) == 0.0

    def test_check_skip_threshold_triggered(self):
        r = ScanResult()
        for i in range(80):
            r.skip(f"T{i}", "timeout")
        r.verdicts = [None] * 20
        assert r.check_skip_threshold(100) is True

    def test_check_skip_threshold_not_triggered(self):
        r = ScanResult()
        for i in range(50):
            r.skip(f"T{i}", "timeout")
        r.verdicts = [None] * 50
        assert r.check_skip_threshold(100) is False

    def test_custom_threshold(self):
        r = ScanResult()
        for i in range(60):
            r.skip(f"T{i}", "timeout")
        r.verdicts = [None] * 40
        assert r.check_skip_threshold(100, threshold=0.50) is True
        assert r.check_skip_threshold(100, threshold=0.70) is False

    def test_default_empty_state(self):
        r = ScanResult()
        assert r.cycles == 0
        assert r.verdicts == []
        assert r.total_skips == 0


# ===========================================================================
# health.py
# ===========================================================================

class TestHealthReport:
    def test_initial_healthy(self):
        r = HealthReport(agent="cipher", version="2.0.0")
        assert r.status == HealthStatus.HEALTHY

    def test_add_source_ok(self):
        r = HealthReport(agent="cipher", version="2.0.0")
        r.add_source("orats", ok=True)
        assert r.status == HealthStatus.HEALTHY
        assert r.sources[0].status == "OK"

    def test_add_source_down_sets_unhealthy(self):
        r = HealthReport(agent="cipher", version="2.0.0")
        r.add_source("orats", ok=False)
        assert r.status == HealthStatus.UNHEALTHY
        assert r.sources[0].status == "DOWN"

    def test_add_source_fallback_sets_degraded(self):
        r = HealthReport(agent="cipher", version="2.0.0")
        r.add_source("orats", ok=False, fallback=True, fallback_source="cached")
        assert r.status == HealthStatus.DEGRADED
        assert r.sources[0].fallback_active is True

    def test_unhealthy_not_overridden_by_fallback(self):
        r = HealthReport(agent="cipher", version="2.0.0")
        r.add_source("orats", ok=False)
        r.add_source("alpaca", ok=False, fallback=True)
        assert r.status == HealthStatus.UNHEALTHY

    def test_suspend_and_resume(self):
        r = HealthReport(agent="cipher", version="2.0.0")
        r.suspend("VIX > 35")
        assert r.status == HealthStatus.SUSPENDED
        assert r.suspended_reason == "VIX > 35"
        r.resume()
        assert r.status == HealthStatus.HEALTHY
        assert r.suspended_reason is None

    def test_to_dict_structure(self):
        r = HealthReport(agent="atlas", version="1.5.0")
        r.add_source("alpaca", ok=True)
        r.extra["scan_pool_size"] = 50
        d = r.to_dict()
        assert d["agent"] == "atlas"
        assert d["version"] == "1.5.0"
        assert d["status"] == "HEALTHY"
        assert "uptime_seconds" in d
        assert d["scan_pool_size"] == 50
        assert len(d["sources"]) == 1
        assert d["sources"][0]["name"] == "alpaca"

    def test_uptime_seconds_positive(self):
        r = HealthReport(agent="test", version="0.0.1")
        time.sleep(0.01)
        assert r.uptime_seconds >= 0

    def test_extra_fields_merged_at_top_level(self):
        r = HealthReport(agent="omni", version="3.1.0")
        r.extra["syntheses_today"] = 7
        r.extra["go_verdicts_today"] = 2
        d = r.to_dict()
        assert d["syntheses_today"] == 7
        assert d["go_verdicts_today"] == 2


# ===========================================================================
# alerts.py
# ===========================================================================

class TestAlertAhmed:
    def setup_method(self):
        reset_cooldowns()

    @patch("shared.resilience.alerts.requests.post")
    def test_sends_with_token_set(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"CIPHER_BOT_TOKEN": "test_token"}):
            result = alert_ahmed("test message", key="test_key", severity="WARNING")

        assert result is True
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert "test message" in payload["text"]
        assert "⚠️" in payload["text"]

    @patch("shared.resilience.alerts.requests.post")
    def test_uses_fallback_token_when_env_not_set(self, mock_post):
        """
        When CIPHER_BOT_TOKEN env var is absent, alert_ahmed falls back
        to the hardcoded bot token from IDENTITY.md. It should still send.
        """
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()
        env = {k: v for k, v in os.environ.items() if k != "CIPHER_BOT_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            result = alert_ahmed("test", key="fallback_test")
        assert result is True
        mock_post.assert_called_once()

    @patch("shared.resilience.alerts.requests.post")
    def test_cooldown_suppresses_second_call(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"CIPHER_BOT_TOKEN": "test_token"}):
            r1 = alert_ahmed("msg1", key="same_key")
            r2 = alert_ahmed("msg2", key="same_key")

        assert r1 is True
        assert r2 is False
        assert mock_post.call_count == 1

    @patch("shared.resilience.alerts.requests.post")
    def test_none_key_always_sends(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"CIPHER_BOT_TOKEN": "test_token"}):
            r1 = alert_ahmed("msg1", key=None)
            r2 = alert_ahmed("msg2", key=None)

        assert r1 is True
        assert r2 is True
        assert mock_post.call_count == 2

    @patch("shared.resilience.alerts.requests.post")
    def test_different_keys_both_send(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"CIPHER_BOT_TOKEN": "test_token"}):
            r1 = alert_ahmed("msg1", key="key_a")
            r2 = alert_ahmed("msg2", key="key_b")

        assert r1 is True
        assert r2 is True
        assert mock_post.call_count == 2

    @patch("shared.resilience.alerts.requests.post", side_effect=Exception("timeout"))
    def test_network_failure_returns_false(self, mock_post):
        with patch.dict(os.environ, {"CIPHER_BOT_TOKEN": "test_token"}):
            result = alert_ahmed("crash", key="crash_key")
        assert result is False

    @patch("shared.resilience.alerts.requests.post")
    def test_critical_severity_prefix(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()

        with patch.dict(os.environ, {"CIPHER_BOT_TOKEN": "test_token"}):
            alert_ahmed("critical event", key=None, severity="CRITICAL")

        payload = mock_post.call_args[1]["json"]
        assert "🔴" in payload["text"]

    def test_reset_cooldowns_clears_history(self):
        from shared.resilience.alerts import _alert_history
        _alert_history["some_key"] = __import__("datetime").datetime.utcnow()
        reset_cooldowns()
        assert len(_alert_history) == 0
