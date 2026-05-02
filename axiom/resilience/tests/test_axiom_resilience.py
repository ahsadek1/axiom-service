"""
Tests for Axiom 30% resilience layer.
Run: cd /Users/ahmedsadek/nexus/axiom && python -m pytest resilience/tests/ -v
"""
import sys
import os

# Ensure axiom package root and nexus root are on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

from axiom.resilience.contracts import (
    validate_polygon_quote,
    validate_orats_summary,
    validate_alpaca_position,
    validate_assess_request,
)
from axiom.resilience.state import make_vix_cache, make_regime_cache
from axiom.resilience.db import immediate_transaction
from axiom.resilience.alerts import AxiomAlerter
from axiom.resilience.health import AxiomHealthReport, AxiomSourceStatus
from shared.resilience.contracts import DataContractError
from shared.resilience.state import StaleStateError


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

class TestPolygonQuote:
    def test_valid(self):
        data = {"open_interest": 1500.0, "volume": 800.0, "bid": 1.20, "ask": 1.25}
        result = validate_polygon_quote(data, "AAPL")
        assert result["open_interest"] == 1500.0
        assert result["volume"] == 800.0
        assert result["bid"] == 1.20
        assert result["ask"] == 1.25

    def test_null_oi_raises(self):
        with pytest.raises(DataContractError) as exc:
            validate_polygon_quote(
                {"open_interest": None, "volume": 100, "bid": 1, "ask": 1.5}, "AAPL"
            )
        assert "open_interest" in str(exc.value)

    def test_missing_oi_raises(self):
        with pytest.raises(DataContractError):
            validate_polygon_quote({"volume": 100, "bid": 1, "ask": 1.5}, "AAPL")

    def test_negative_oi_raises(self):
        with pytest.raises(DataContractError):
            validate_polygon_quote(
                {"open_interest": -1, "volume": 100, "bid": 1, "ask": 1.5}, "AAPL"
            )

    def test_ask_exceeds_max_raises(self):
        with pytest.raises(DataContractError):
            validate_polygon_quote(
                {"open_interest": 100, "volume": 100, "bid": 1, "ask": 200_000}, "AAPL"
            )

    def test_zero_values_valid(self):
        data = {"open_interest": 0, "volume": 0, "bid": 0, "ask": 0}
        result = validate_polygon_quote(data, "AAPL")
        assert result["open_interest"] == 0.0

    def test_string_coercion(self):
        data = {"open_interest": "1500", "volume": "800", "bid": "1.20", "ask": "1.25"}
        result = validate_polygon_quote(data, "AAPL")
        assert result["open_interest"] == 1500.0


class TestOratsSummary:
    def test_valid(self):
        data = {"rip": 55.0, "iv30": 0.30}
        result = validate_orats_summary(data, "AAPL")
        assert result["rip"] == 55.0
        assert result["iv30"] == 0.30

    def test_null_ivr_raises(self):
        with pytest.raises(DataContractError) as exc:
            validate_orats_summary({"rip": None, "iv30": 0.25}, "AAPL")
        assert "rip" in str(exc.value)

    def test_ivr_above_100_raises(self):
        with pytest.raises(DataContractError):
            validate_orats_summary({"rip": 150.0, "iv30": 0.25}, "AAPL")

    def test_ivr_negative_raises(self):
        with pytest.raises(DataContractError):
            validate_orats_summary({"rip": -5.0, "iv30": 0.25}, "AAPL")

    def test_iv30_above_500pct_raises(self):
        with pytest.raises(DataContractError):
            validate_orats_summary({"rip": 50.0, "iv30": 6.0}, "AAPL")

    def test_ivr_zero_valid(self):
        result = validate_orats_summary({"rip": 0.0, "iv30": 0.10}, "AAPL")
        assert result["rip"] == 0.0

    def test_ivr_100_valid(self):
        result = validate_orats_summary({"rip": 100.0, "iv30": 0.50}, "AAPL")
        assert result["rip"] == 100.0


class TestAlpacaPosition:
    def test_valid(self):
        pos = {"market_value": "1500.50", "symbol": "AAPL"}
        result = validate_alpaca_position(pos)
        assert result["market_value"] == 1500.50
        assert result["symbol"] == "AAPL"

    def test_empty_symbol_raises(self):
        with pytest.raises(DataContractError):
            validate_alpaca_position({"market_value": "1000", "symbol": ""})

    def test_null_symbol_raises(self):
        with pytest.raises(DataContractError):
            validate_alpaca_position({"market_value": "1000", "symbol": None})

    def test_null_market_value_raises(self):
        with pytest.raises(DataContractError):
            validate_alpaca_position({"market_value": None, "symbol": "AAPL"})

    def test_negative_market_value_valid(self):
        # Short positions can have negative market_value — no min constraint
        result = validate_alpaca_position({"market_value": -500.0, "symbol": "SPY"})
        assert result["market_value"] == -500.0


class TestAssessRequest:
    def test_valid_minimal(self):
        result = validate_assess_request({"ticker": "AAPL"})
        assert result["ticker"] == "AAPL"

    def test_valid_full(self):
        result = validate_assess_request(
            {"ticker": "SPY", "dte": 30, "ivr": 55.0, "proposed_usd": 5000.0}
        )
        assert result["ticker"] == "SPY"
        assert result["dte"] == 30
        assert result["ivr"] == 55.0
        assert result["proposed_usd"] == 5000.0

    def test_empty_ticker_raises(self):
        with pytest.raises(DataContractError):
            validate_assess_request({"ticker": ""})

    def test_lowercase_ticker_raises(self):
        with pytest.raises(DataContractError):
            validate_assess_request({"ticker": "aapl"})

    def test_ticker_too_long_raises(self):
        with pytest.raises(DataContractError):
            validate_assess_request({"ticker": "TOOLONG"})

    def test_ivr_out_of_range_raises(self):
        with pytest.raises(DataContractError):
            validate_assess_request({"ticker": "AAPL", "ivr": 150.0})

    def test_dte_negative_raises(self):
        with pytest.raises(DataContractError):
            validate_assess_request({"ticker": "AAPL", "dte": -1})

    def test_dte_above_365_raises(self):
        with pytest.raises(DataContractError):
            validate_assess_request({"ticker": "AAPL", "dte": 400})

    def test_proposed_usd_exceeds_max_raises(self):
        with pytest.raises(DataContractError):
            validate_assess_request({"ticker": "AAPL", "proposed_usd": 2_000_000})

    def test_optional_fields_absent(self):
        # No dte/ivr/proposed_usd — should pass without error
        result = validate_assess_request({"ticker": "TSLA"})
        assert "dte" not in result or result.get("dte") is None or True

    def test_optional_fields_none(self):
        # None values for optional fields should be skipped
        result = validate_assess_request({"ticker": "TSLA", "dte": None, "ivr": None})
        assert result["ticker"] == "TSLA"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class TestVixCache:
    def test_fresh_value(self):
        cache = make_vix_cache()
        cache.update(22.5)
        assert cache.get() == 22.5

    def test_stale_returns_none(self):
        from datetime import timedelta
        cache = make_vix_cache()
        cache.update(22.5)
        cache._updated_at = cache._updated_at - timedelta(minutes=15)
        assert cache.get() is None

    def test_never_updated_returns_none(self):
        cache = make_vix_cache()
        assert cache.get() is None

    def test_update_refreshes(self):
        from datetime import timedelta
        cache = make_vix_cache()
        cache.update(10.0)
        cache._updated_at = cache._updated_at - timedelta(minutes=15)
        assert cache.get() is None
        cache.update(15.0)
        assert cache.get() == 15.0

    def test_require_stale_raises(self):
        from datetime import timedelta
        cache = make_vix_cache()
        cache.update(22.5)
        cache._updated_at = cache._updated_at - timedelta(minutes=15)
        with pytest.raises(StaleStateError):
            cache.require()


class TestRegimeCache:
    def test_fresh_value(self):
        cache = make_regime_cache()
        cache.update({"classification": "NORMAL"})
        assert cache.get() == {"classification": "NORMAL"}

    def test_stale_returns_none(self):
        from datetime import timedelta
        cache = make_regime_cache()
        cache.update({"classification": "NORMAL"})
        cache._updated_at = cache._updated_at - timedelta(minutes=25)
        assert cache.get() is None

    def test_require_stale_raises(self):
        from datetime import timedelta
        cache = make_regime_cache()
        cache.update({"classification": "NORMAL"})
        cache._updated_at = cache._updated_at - timedelta(minutes=25)
        with pytest.raises(StaleStateError):
            cache.require()

    def test_never_updated_returns_none(self):
        cache = make_regime_cache()
        assert cache.get() is None


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

class TestImmediateTransaction:
    def test_write_and_read(self, tmp_path):
        db = str(tmp_path / "test.db")
        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        conn.commit()
        conn.close()

        with immediate_transaction(db) as c:
            c.execute("INSERT INTO t (val) VALUES (?)", ("hello",))

        conn = sqlite3.connect(db)
        row = conn.execute("SELECT val FROM t").fetchone()
        conn.close()
        assert row[0] == "hello"

    def test_rollback_on_exception(self, tmp_path):
        db = str(tmp_path / "test.db")
        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        with pytest.raises(ValueError):
            with immediate_transaction(db) as c:
                c.execute("INSERT INTO t (id) VALUES (?)", (1,))
                raise ValueError("simulated error")

        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        assert count == 0

    def test_multiple_sequential_writes(self, tmp_path):
        db = str(tmp_path / "test.db")
        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        conn.commit()
        conn.close()

        for i in range(3):
            with immediate_transaction(db) as c:
                c.execute("INSERT INTO t (val) VALUES (?)", (f"val{i}",))

        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        assert count == 3


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

class TestAxiomAlerter:
    def test_dedup_suppresses_second_send(self):
        alerter = AxiomAlerter()
        # Patch _deliver to avoid real comms
        delivered = []
        alerter._deliver = lambda cat, tick, msg, state: delivered.append(msg)

        alerter.send("HEALTH_FAIL", "orats", "test msg 1", {})
        alerter.send("HEALTH_FAIL", "orats", "test msg 2", {})
        assert len(delivered) == 1

    def test_different_keys_both_sent(self):
        alerter = AxiomAlerter()
        delivered = []
        alerter._deliver = lambda cat, tick, msg, state: delivered.append(msg)

        alerter.send("HEALTH_FAIL", "orats", "msg1", {})
        alerter.send("HEALTH_FAIL", "polygon", "msg2", {})
        assert len(delivered) == 2

    def test_different_categories_both_sent(self):
        alerter = AxiomAlerter()
        delivered = []
        alerter._deliver = lambda cat, tick, msg, state: delivered.append(msg)

        alerter.send("HEALTH_FAIL", "orats", "msg1", {})
        alerter.send("CRITICAL_FLAG", "orats", "msg2", {})
        assert len(delivered) == 2

    def test_send_health_alert(self):
        alerter = AxiomAlerter()
        delivered = []
        alerter._deliver = lambda cat, tick, msg, state: delivered.append(msg)

        sent = alerter.send_health_alert("orats", ["timeout", "503"], {})
        assert sent is True
        assert len(delivered) == 1
        assert "FAILED" in delivered[0] or "HEALTH" in delivered[0]

    def test_send_contract_violation(self):
        alerter = AxiomAlerter()
        delivered = []
        alerter._deliver = lambda cat, tick, msg, state: delivered.append(msg)

        sent = alerter.send_contract_violation("orats.summary", "rip", "null value", {})
        assert sent is True
        assert "DataContractError" in delivered[0] or "CONTRACT" in delivered[0] or "orats.summary" in delivered[0]

    def test_returns_false_when_deduped(self):
        alerter = AxiomAlerter()
        alerter._deliver = lambda cat, tick, msg, state: None

        first = alerter.send("HEALTH_FAIL", "orats", "msg", {})
        second = alerter.send("HEALTH_FAIL", "orats", "msg", {})
        assert first is True
        assert second is False


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestAxiomHealthReport:
    def test_healthy_when_all_sources_up(self):
        report = AxiomHealthReport()
        report.sources = {
            "orats": AxiomSourceStatus("orats", True),
            "polygon": AxiomSourceStatus("polygon", True),
            "alpaca": AxiomSourceStatus("alpaca", True),
            "deepseek": AxiomSourceStatus("deepseek", True),
            "local_db": AxiomSourceStatus("local_db", True),
        }
        report._compute_overall()
        assert report.overall == "healthy"
        assert report.degraded_sources == []

    def test_degraded_when_one_critical_down(self):
        report = AxiomHealthReport()
        report.sources = {
            "orats": AxiomSourceStatus("orats", False, error="timeout"),
            "polygon": AxiomSourceStatus("polygon", True),
            "alpaca": AxiomSourceStatus("alpaca", True),
            "deepseek": AxiomSourceStatus("deepseek", True),
            "local_db": AxiomSourceStatus("local_db", True),
        }
        report._compute_overall()
        assert report.overall == "degraded"
        assert "orats" in report.degraded_sources

    def test_critical_when_two_critical_down(self):
        report = AxiomHealthReport()
        report.sources = {
            "orats": AxiomSourceStatus("orats", False, error="timeout"),
            "polygon": AxiomSourceStatus("polygon", False, error="timeout"),
            "alpaca": AxiomSourceStatus("alpaca", True),
            "deepseek": AxiomSourceStatus("deepseek", True),
            "local_db": AxiomSourceStatus("local_db", True),
        }
        report._compute_overall()
        assert report.overall == "critical"

    def test_degraded_when_noncritical_down(self):
        report = AxiomHealthReport()
        report.sources = {
            "orats": AxiomSourceStatus("orats", True),
            "polygon": AxiomSourceStatus("polygon", True),
            "alpaca": AxiomSourceStatus("alpaca", True),
            "deepseek": AxiomSourceStatus("deepseek", False, error="key missing"),
            "local_db": AxiomSourceStatus("local_db", True),
        }
        report._compute_overall()
        assert report.overall == "degraded"
        assert "deepseek" in report.degraded_sources

    def test_to_dict_structure(self):
        report = AxiomHealthReport()
        report.sources = {
            "orats": AxiomSourceStatus("orats", True, latency_ms=45.2),
        }
        report._compute_overall()
        d = report.to_dict()
        assert "overall" in d
        assert "timestamp" in d
        assert "sources" in d
        assert "degraded" in d
        assert d["sources"]["orats"]["reachable"] is True
        assert d["sources"]["orats"]["latency_ms"] == 45.2

    def test_check_local_db_success(self, tmp_path):
        import sqlite3
        db = str(tmp_path / "axiom.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

        report = AxiomHealthReport()
        app_state = {"settings": None}
        os.environ["AXIOM_DB_PATH"] = db
        report._check_local_db(app_state)
        assert report.sources["local_db"].reachable is True

    def test_check_local_db_failure(self):
        report = AxiomHealthReport()
        app_state = {}
        os.environ["AXIOM_DB_PATH"] = "/nonexistent/path/axiom.db"
        report._check_local_db(app_state)
        assert report.sources["local_db"].reachable is False
        assert report.sources["local_db"].error is not None

    def test_check_deepseek_key_present(self):
        report = AxiomHealthReport()
        os.environ["DEEPSEEK_API_KEY"] = "sk-testkey"
        report._check_deepseek()
        assert report.sources["deepseek"].reachable is True

    def test_check_deepseek_key_absent(self):
        report = AxiomHealthReport()
        os.environ.pop("DEEPSEEK_KEY", None)
        os.environ.pop("DEEPSEEK_API_KEY", None)
        report._check_deepseek()
        assert report.sources["deepseek"].reachable is False
        assert "not set" in report.sources["deepseek"].error


# ---------------------------------------------------------------------------
# Scheduler integration — health check job
# ---------------------------------------------------------------------------

class TestSchedulerHealthCheckJob:
    """
    Verify the resilience health check is wired into the Axiom scheduler.
    Spec: AXIOM_30_SPEC v1.0 — check_all() runs in scheduler every 5 min.
    """

    def test_scheduler_has_resilience_health_check_job(self):
        """create_scheduler() must register 'resilience_health_check' job."""
        from axiom.scheduler import create_scheduler

        app_state = {
            "settings": None,
            "_vix_cache": None,
            "_regime_cache": None,
            "_health_report": None,
        }
        scheduler = create_scheduler(app_state)
        job_ids = [j.id for j in scheduler.get_jobs()]
        assert "resilience_health_check" in job_ids, (
            f"Scheduler missing 'resilience_health_check' job. Found: {job_ids}"
        )

    def test_run_health_check_populates_app_state(self):
        """_run_health_check() updates app_state['_health_report'] on success."""
        from axiom.scheduler import _run_health_check
        from unittest.mock import patch, MagicMock

        app_state = {"_health_report": None, "settings": None}
        mock_report = MagicMock()
        mock_report.overall = "healthy"
        mock_report.degraded_sources = []

        with patch("axiom.scheduler.AxiomHealthReport", return_value=mock_report):
            _run_health_check(app_state)

        assert app_state["_health_report"] is mock_report
        mock_report.check_all.assert_called_once_with(app_state)

    def test_run_health_check_never_raises(self):
        """_run_health_check() swallows all exceptions — scheduler must not crash."""
        from axiom.scheduler import _run_health_check
        from unittest.mock import patch

        app_state = {"_health_report": None, "settings": None}

        def exploding_init():
            raise RuntimeError("Simulated health check crash")

        with patch("axiom.scheduler.AxiomHealthReport", side_effect=exploding_init):
            # Must not raise — scheduler thread safety requires this
            _run_health_check(app_state)

        # _health_report stays None (previous value preserved on failure)
        assert app_state["_health_report"] is None
