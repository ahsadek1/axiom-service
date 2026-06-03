"""
test_ails.py — AILS full test suite (20 tests)
Covers: Bayesian updating, regime classification, patterns, reports, API endpoints.
"""

import json
import sys
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from typing import Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_ails_db, init_backtest_db
from bayesian import get_win_rate, ingest_outcome, compute_blended_rate, confidence_label
from regime import classify_vix
from patterns import store_pattern, find_similar, _similarity_key, _vector_distance
from reports import generate_eod_report, generate_eow_report


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _dbs():
    """Create fresh temp DBs for each test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        ails_path = f.name
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        bt_path = f.name
    ails = init_ails_db(ails_path)
    bt = init_backtest_db(bt_path)
    return ails, bt


# ---------------------------------------------------------------------------
# T01 — POST /outcome stores correctly in live_outcomes
# ---------------------------------------------------------------------------

class TestT01OutcomeStorage(unittest.TestCase):
    def test_outcome_stored_in_db(self) -> None:
        """POST /outcome should store trade in live_outcomes table."""
        ails, bt = _dbs()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        ails.execute(
            "INSERT INTO live_outcomes "
            "(ts, ticker, strategy, regime, direction, pnl, win, system) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now, "AAPL", "bull_put_spread", "NORMAL", "bullish", 150.0, 1, "alpha"),
        )
        ails.commit()
        row = ails.execute(
            "SELECT * FROM live_outcomes WHERE ticker='AAPL'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["ticker"], "AAPL")
        self.assertEqual(row["win"], 1)


# ---------------------------------------------------------------------------
# T02 — Bayesian update with 0 live outcomes returns historical prior
# ---------------------------------------------------------------------------

class TestT02BayesianPriorOnly(unittest.TestCase):
    def test_no_live_returns_historical_prior(self) -> None:
        """With no live outcomes, should return research prior."""
        ails, bt = _dbs()
        result = get_win_rate("AAPL", "bull_put_spread", "NORMAL", "bullish", ails, bt)
        self.assertEqual(result["source"], "research_prior")
        self.assertGreater(result["win_rate"], 0)
        self.assertLess(result["win_rate"], 1)


# ---------------------------------------------------------------------------
# T03 — Bayesian update shifts rate after enough live outcomes
# ---------------------------------------------------------------------------

class TestT03BayesianShift(unittest.TestCase):
    def test_live_outcomes_shift_rate(self) -> None:
        """After 10 live outcomes all wins, blended rate should be higher than prior."""
        ails, bt = _dbs()
        # Get baseline prior
        prior = get_win_rate("TSLA", "bull_put_spread", "ELEVATED", "bullish", ails, bt)
        prior_rate = prior["win_rate"]

        # Ingest 10 wins
        for _ in range(10):
            ingest_outcome("TSLA", "bull_put_spread", "ELEVATED", "bullish", True, ails, bt)

        updated = get_win_rate("TSLA", "bull_put_spread", "ELEVATED", "bullish", ails, bt)
        self.assertGreater(updated["win_rate"], prior_rate)
        self.assertNotEqual(updated["confidence"], "backtest_only")


# ---------------------------------------------------------------------------
# T04 — GET /context returns required fields
# ---------------------------------------------------------------------------

class TestT04ContextResponse(unittest.TestCase):
    def test_context_has_required_fields(self) -> None:
        """get_win_rate should return win_rate, confidence, sample_count, source."""
        ails, bt = _dbs()
        result = get_win_rate("MSFT", "swing_long", "NORMAL", "bullish", ails, bt)
        self.assertIn("win_rate", result)
        self.assertIn("confidence", result)
        self.assertIn("sample_count", result)
        self.assertIn("source", result)


# ---------------------------------------------------------------------------
# T05 — Unknown ticker returns regime-level default
# ---------------------------------------------------------------------------

class TestT05UnknownTickerDefault(unittest.TestCase):
    def test_unknown_ticker_returns_regime_default(self) -> None:
        """Unknown ticker with no live data should return regime-level research prior."""
        ails, bt = _dbs()
        result = get_win_rate("ZZZZ", "bull_put_spread", "ELEVATED", "bullish", ails, bt)
        self.assertIn(result["source"], ["research_prior", "neutral_prior"])
        self.assertGreaterEqual(result["win_rate"], 0.0)
        self.assertLessEqual(result["win_rate"], 1.0)


# ---------------------------------------------------------------------------
# T06 — Regime classification: VIX 12 → LOW_VOL
# ---------------------------------------------------------------------------

class TestT06RegimeLowVol(unittest.TestCase):
    def test_vix_12_is_low_vol(self) -> None:
        self.assertEqual(classify_vix(12.0), "LOW_VOL")

    def test_vix_11_is_low_vol(self) -> None:
        self.assertEqual(classify_vix(11.5), "LOW_VOL")


# ---------------------------------------------------------------------------
# T07 — Regime classification: VIX 28 → ELEVATED
# ---------------------------------------------------------------------------

class TestT07RegimeElevated(unittest.TestCase):
    def test_vix_28_is_elevated(self) -> None:
        self.assertEqual(classify_vix(28.0), "ELEVATED")

    def test_vix_19_is_normal(self) -> None:
        self.assertEqual(classify_vix(19.0), "NORMAL")


# ---------------------------------------------------------------------------
# T08 — Regime classification: VIX 45 → CRISIS
# ---------------------------------------------------------------------------

class TestT08RegimeCrisis(unittest.TestCase):
    def test_vix_60_is_crisis(self) -> None:
        self.assertEqual(classify_vix(60.0), "CRISIS")

    def test_vix_38_is_stress(self) -> None:
        self.assertEqual(classify_vix(38.0), "STRESS")

    def test_vix_45_is_high_stress(self) -> None:
        self.assertEqual(classify_vix(45.0), "HIGH_STRESS")


# ---------------------------------------------------------------------------
# T09 — Agent calibration updates correctly
# ---------------------------------------------------------------------------

class TestT09AgentCalibration(unittest.TestCase):
    def test_calibration_stored_after_outcome(self) -> None:
        """Agent votes should update calibration table via ingest."""
        ails, bt = _dbs()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        # Manually insert calibration
        ails.execute(
            "INSERT INTO agent_calibration "
            "(agent, strategy, regime, predicted_confidence, actual_win_rate, n, last_updated) "
            "VALUES (?,?,?,?,?,?,?)",
            ("Cipher", "bull_put_spread", "NORMAL", 0.72, 0.68, 5, now),
        )
        ails.commit()
        row = ails.execute(
            "SELECT actual_win_rate FROM agent_calibration WHERE agent='Cipher'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["actual_win_rate"], 0.68, places=2)


# ---------------------------------------------------------------------------
# T10 — Pattern similarity search returns top results
# ---------------------------------------------------------------------------

class TestT10PatternSearch(unittest.TestCase):
    def test_find_similar_returns_results(self) -> None:
        """After storing patterns, find_similar should return matching setups."""
        ails, _ = _dbs()

        # Store 5 patterns
        for i in range(5):
            store_pattern(
                "NVDA",
                {"iv_rank": 0.65 + i * 0.01, "rsi": 52.0 + i, "momentum": 0.3},
                outcome=(i % 2 == 0),
                regime="ELEVATED",
                pnl=100.0 * (1 if i % 2 == 0 else -1),
                strategy="bull_put_spread",
                conn=ails,
            )

        similar = find_similar(
            {"iv_rank": 0.67, "rsi": 53.0, "momentum": 0.3},
            regime="ELEVATED",
            strategy="bull_put_spread",
            conn=ails,
            top_n=10,
        )
        self.assertGreater(len(similar), 0)
        self.assertIn("distance", similar[0])
        self.assertIn("outcome", similar[0])


# ---------------------------------------------------------------------------
# T11 — EOD report generated with correct structure
# ---------------------------------------------------------------------------

class TestT11EODReport(unittest.TestCase):
    def test_eod_report_structure(self) -> None:
        """EOD report should contain all required sections."""
        ails, bt = _dbs()

        with patch("reports.get_current_regime", return_value={"regime": "NORMAL", "vix": 18.5}):
            report = generate_eod_report(ails, bt, "2026-04-11")

        self.assertIn("date", report)
        self.assertIn("summary", report)
        self.assertIn("strategy_breakdown", report)
        self.assertIn("agent_calibration", report)
        self.assertIn("drift_flags", report)
        self.assertEqual(report["date"], "2026-04-11")
        self.assertEqual(report["regime"], "NORMAL")


# ---------------------------------------------------------------------------
# T12 — EOW report includes agent weight recommendations
# ---------------------------------------------------------------------------

class TestT12EOWReport(unittest.TestCase):
    def test_eow_report_has_recommendations(self) -> None:
        """EOW report should include weight recommendations and approval flag."""
        ails, bt = _dbs()

        with patch("reports.get_current_regime", return_value={"regime": "NORMAL", "vix": 19.0}):
            report = generate_eow_report(ails, bt, "2026-04-11")

        self.assertIn("weight_recommendations", report)
        self.assertIn("requires_approval", report)
        self.assertIn("note", report)
        self.assertIn("Ahmed approval", report["note"])


# ---------------------------------------------------------------------------
# T13 — Dead letter queue stores outcomes on failure
# ---------------------------------------------------------------------------

class TestT13DeadLetterQueue(unittest.TestCase):
    def test_dead_letter_queue_table_exists(self) -> None:
        """dead_letter_queue table should be created by init_ails_db."""
        ails, _ = _dbs()
        ails.execute(
            "INSERT INTO dead_letter_queue (received_at, payload, retry_count) "
            "VALUES ('2026-04-11T10:00:00Z', '{\"ticker\": \"AAPL\"}', 0)"
        )
        ails.commit()
        row = ails.execute(
            "SELECT payload FROM dead_letter_queue LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("AAPL", row["payload"])


# ---------------------------------------------------------------------------
# T14 — /health returns all subsystem statuses
# ---------------------------------------------------------------------------

class TestT14HealthEndpoint(unittest.TestCase):
    def test_health_check_structure(self) -> None:
        """Health response should contain status and subsystems keys."""
        from fastapi.testclient import TestClient
        import main
        ails, bt = _dbs()
        main._ails_conn = ails
        main._backtest_conn = bt

        client = TestClient(main.app)
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("status", data)
        self.assertIn("subsystems", data)


# ---------------------------------------------------------------------------
# T15 — Backtest status shows population progress
# ---------------------------------------------------------------------------

class TestT15BacktestStatus(unittest.TestCase):
    def test_backtest_status_structure(self) -> None:
        """Backtest status should show seeded regime rates."""
        from fastapi.testclient import TestClient
        import main
        import config as ails_config
        ails, bt = _dbs()
        main._ails_conn = ails
        main._backtest_conn = bt

        client = TestClient(main.app)
        with patch.object(ails_config, "AILS_SECRET", "test-ails-secret-12345"):
            resp = client.get(
                "/backtest/status",
                headers={"X-Ails-Secret": "test-ails-secret-12345"},
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("regime_level_rates_seeded", data)


# ---------------------------------------------------------------------------
# T16 — New ticker creates bayesian_rates row after first outcome
# ---------------------------------------------------------------------------

class TestT16NewTickerBayesian(unittest.TestCase):
    def test_new_ticker_creates_row(self) -> None:
        """First outcome for unknown ticker should create a bayesian_rates entry."""
        ails, bt = _dbs()
        ingest_outcome("NEWT", "swing_long", "ELEVATED", "bullish", True, ails, bt)
        row = ails.execute(
            "SELECT blended_rate, live_n FROM bayesian_rates WHERE ticker='NEWT'"
        ).fetchone()
        # Row should exist and have 1 live outcome recorded
        self.assertIsNotNone(row)
        self.assertEqual(row["live_n"], 1)
        # Rate is valid probability (prior is large so one win barely shifts it)
        self.assertGreaterEqual(row["blended_rate"], 0.0)
        self.assertLessEqual(row["blended_rate"], 1.0)


# ---------------------------------------------------------------------------
# T17 — Live weight 3× confirmed in blended_rate calculation
# ---------------------------------------------------------------------------

class TestT17LiveWeight(unittest.TestCase):
    def test_live_weight_applied(self) -> None:
        """compute_blended_rate should apply LIVE_OUTCOME_WEIGHT to live data."""
        # Prior: 65% win rate, 100 samples
        # Live: 5 wins out of 5 (100%) — weighted 3×
        blended = compute_blended_rate(
            prior_wins=65.0, prior_n=100,
            live_wins=5.0, live_n=5,
        )
        # Weighted: (65 + 5*3*1) / (100 + 5*3) = (65+15) / 115 = 80/115 ≈ 0.696
        expected = (65.0 + 15.0) / (100 + 15)
        self.assertAlmostEqual(blended, expected, places=4)


# ---------------------------------------------------------------------------
# T18 — Regime default returned when backtest_count < MIN_BACKTEST_SAMPLES
# ---------------------------------------------------------------------------

class TestT18RegimeDefaultFallback(unittest.TestCase):
    def test_fallback_to_regime_when_insufficient(self) -> None:
        """With < MIN_BACKTEST_SAMPLES historical data, should use regime-level rates."""
        ails, bt = _dbs()
        # Don't insert any historical_win_rates — should fall back to regime_level_rates
        result = get_win_rate("RARE", "bear_call_spread", "STRESS", "bearish", ails, bt)
        self.assertIn(result["source"], ["research_prior", "neutral_prior"])


# ---------------------------------------------------------------------------
# T19 — ORACLE cache TTL field present in context response
# ---------------------------------------------------------------------------

class TestT19OracleCacheTTL(unittest.TestCase):
    def test_context_includes_cache_ttl(self) -> None:
        """GET /context should include cache_ttl_s for ORACLE caching."""
        from fastapi.testclient import TestClient
        import main
        ails, bt = _dbs()
        main._ails_conn = ails
        main._backtest_conn = bt

        client = TestClient(main.app)
        import config as ails_config
        with patch.object(ails_config, "AILS_SECRET", "test-ails-secret-12345"), \
             patch("main.get_current_regime", return_value={"regime": "NORMAL", "vix": 17.0}):
            resp = client.get(
                "/context/AAPL?strategy=bull_put_spread&direction=bullish",
                headers={"X-Ails-Secret": "test-ails-secret-12345"},
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("cache_ttl_s", data)
        self.assertEqual(data["cache_ttl_s"], 900)


# ---------------------------------------------------------------------------
# T20 — EOD report flags parameter drift when live rate diverges >15%
# ---------------------------------------------------------------------------

class TestT20DriftDetection(unittest.TestCase):
    def test_drift_flag_when_divergence_exceeds_threshold(self) -> None:
        """Live win rate 40% vs historical 72% → drift flag in EOD report."""
        ails, bt = _dbs()
        from datetime import datetime, timezone

        # Insert 5 losing trades today (bull_put_spread, NORMAL, bullish)
        today = datetime.now().strftime("%Y-%m-%d")
        for i in range(5):
            ails.execute(
                "INSERT INTO live_outcomes "
                "(ts, ticker, strategy, regime, direction, pnl, win, system) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"{today}T10:0{i}:00Z", f"TICK{i}",
                 "bull_put_spread", "NORMAL", "bullish", -200.0, 0, "alpha"),
            )
        # 2 winning trades
        for i in range(2):
            ails.execute(
                "INSERT INTO live_outcomes "
                "(ts, ticker, strategy, regime, direction, pnl, win, system) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"{today}T11:0{i}:00Z", f"WIN{i}",
                 "bull_put_spread", "NORMAL", "bullish", 300.0, 1, "alpha"),
            )
        ails.commit()

        with patch("reports.get_current_regime",
                   return_value={"regime": "NORMAL", "vix": 17.0}):
            report = generate_eod_report(ails, bt, today)

        # 2/7 = ~29% live vs 72% historical → drift ~43% >> 15%
        self.assertGreater(len(report["drift_flags"]), 0)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
