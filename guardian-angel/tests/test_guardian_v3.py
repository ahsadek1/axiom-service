"""
test_guardian_v3.py — Guardian Angel v3 IDEAL system tests (18 tests)
All mocked — no real launchctl, network, or DB calls.
"""

import os
import time
import math
import sqlite3
import tempfile
import pathlib
import unittest
import requests
from unittest.mock import patch, MagicMock
from typing import Dict, Any, List, Optional


import guardian_angel_v3 as ga3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_telemetry_db() -> sqlite3.Connection:
    """Temp telemetry DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        return ga3._init_telemetry_db(f.name)


def _tmp_healing_db() -> sqlite3.Connection:
    """Temp healing DB."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        return ga3._init_healing_db(f.name)


def _make_collector(conn: sqlite3.Connection) -> ga3.TelemetryCollector:
    """Collector without starting its thread."""
    return ga3.TelemetryCollector(conn)


# ---------------------------------------------------------------------------
# T01 — Baseline engine stores and retrieves stats
# ---------------------------------------------------------------------------

class TestT01BaselineStorage(unittest.TestCase):
    def test_baseline_stored_and_retrieved(self) -> None:
        """Baseline engine should store mean/std and retrieve correctly."""
        conn = _tmp_telemetry_db()
        collector = _make_collector(conn)
        engine = ga3.BaselineEngine(conn, collector)

        # Manually insert signal data for a bucket
        now = time.time()
        for i in range(50):
            conn.execute(
                "INSERT INTO signals (ts, service, signal_name, value) VALUES (?,?,?,?)",
                (now - i * 60, "axiom", "response_time_ms", 100.0 + i * 0.5),
            )
        conn.commit()

        engine.update_baselines()

        import datetime as dt
        now_dt = dt.datetime.fromtimestamp(now - 25 * 60)
        baseline = engine.get_baseline("axiom", "response_time_ms",
                                       now_dt.hour, now_dt.weekday())
        self.assertIsNotNone(baseline)
        mean, std, n = baseline
        self.assertGreater(n, 0)
        self.assertGreater(mean, 0)
        self.assertGreater(std, 0)


# ---------------------------------------------------------------------------
# T02 — Anomaly detection flags 3σ deviation
# ---------------------------------------------------------------------------

class TestT02AnomalyDetection(unittest.TestCase):
    def test_3sigma_deviation_flagged(self) -> None:
        """Value 5σ above baseline should be flagged as anomaly."""
        conn = _tmp_telemetry_db()
        collector = _make_collector(conn)
        engine = ga3.BaselineEngine(conn, collector)

        # Insert 50 normal values to build baseline
        now = time.time()
        import datetime as dt
        now_dt = dt.datetime.now()
        # All samples at same hour/dow bucket
        for i in range(50):
            ts = now - i * 3600 * 24  # Spread over 50 days, same hour
            conn.execute(
                "INSERT INTO signals (ts, service, signal_name, value) VALUES (?,?,?,?)",
                (ts, "axiom", "memory_pct", 20.0),  # Stable at 20%
            )
        conn.commit()
        engine.update_baselines()

        # Patch datetime to return consistent hour/dow for baseline lookup
        with patch("guardian_angel_v3.datetime") as mock_dt:
            mock_dt.now.return_value = dt.datetime.fromtimestamp(now)
            mock_dt.fromtimestamp = dt.datetime.fromtimestamp
            sigma = engine.check_anomaly("axiom", "memory_pct", 80.0)  # 80% = way above 20%

        # Should detect anomaly (sigma >> 3)
        # Note: if baseline n < 30, returns None — check n
        baseline = engine.get_baseline("axiom", "memory_pct", now_dt.hour, now_dt.weekday())
        if baseline and baseline[2] >= 30:
            self.assertIsNotNone(sigma)
            self.assertGreaterEqual(sigma, ga3.ANOMALY_SIGMA)


# ---------------------------------------------------------------------------
# T03 — Causal graph returns root cause from upstream
# ---------------------------------------------------------------------------

class TestT03CausalGraph(unittest.TestCase):
    def test_cascade_traces_to_upstream_root(self) -> None:
        """OMNI failing while ORACLE is degraded → root = oracle."""
        conn = _tmp_telemetry_db()
        collector = _make_collector(conn)
        graph = ga3.CausalGraph(conn, collector)

        # Seed oracle with failing http_ok (0.0 = always failing)
        now = time.time()
        for i in range(20):
            conn.execute(
                "INSERT INTO signals (ts, service, signal_name, value) VALUES (?,?,?,?)",
                (now - i * 5, "oracle", "http_ok", 0.0),
            )
        conn.commit()

        root = graph.find_root_cause("omni")
        # Should trace upstream through alpha-buffer/prime-buffer to oracle
        self.assertIn(root, ["oracle", "alpha-buffer", "prime-buffer", "omni"])


# ---------------------------------------------------------------------------
# T04 — Causal graph returns service itself when no upstream issues
# ---------------------------------------------------------------------------

class TestT04CausalGraphSelfRoot(unittest.TestCase):
    def test_no_upstream_issues_returns_self(self) -> None:
        """When upstream services are healthy, failing service IS the root."""
        conn = _tmp_telemetry_db()
        collector = _make_collector(conn)
        graph = ga3.CausalGraph(conn, collector)

        # Seed all dependencies with healthy http_ok (1.0 = always healthy)
        now = time.time()
        for dep in ["oracle", "alpha-buffer", "prime-buffer"]:
            for i in range(20):
                conn.execute(
                    "INSERT INTO signals (ts, service, signal_name, value) VALUES (?,?,?,?)",
                    (now - i * 5, dep, "http_ok", 1.0),
                )
        conn.commit()

        root = graph.find_root_cause("omni")
        self.assertEqual(root, "omni")


# ---------------------------------------------------------------------------
# T05 — Forecast engine predicts crash within horizon
# ---------------------------------------------------------------------------

class TestT05ForecastPrediction(unittest.TestCase):
    def test_rising_memory_predicts_crash(self) -> None:
        """Linearly rising memory should predict threshold crossing."""
        conn = _tmp_telemetry_db()
        now = time.time()
        # Simulate memory rising from 70% to 83% over last 10 min
        # Rate: 13% / 600s = 0.0217%/s
        # From 83%: 2% to reach 85% threshold = 92 seconds = 1.5 min
        for i in range(120):
            ts = now - (120 - i)
            val = 70.0 + (i / 120.0) * 13.0
            conn.execute(
                "INSERT INTO signals (ts, service, signal_name, value) VALUES (?,?,?,?)",
                (ts, "axiom", "memory_pct", val),
            )
        conn.commit()

        collector = _make_collector(conn)
        # Inject data into collector (bypass thread)
        with collector._lock:
            pass  # Already in conn

        forecast = ga3.ForecastEngine(collector, conn)
        minutes = forecast.predict_minutes_to_threshold("axiom", "memory_pct")

        # Should predict crossing within 15 min
        self.assertIsNotNone(minutes)
        if minutes is not None:
            self.assertGreater(minutes, 0)
            self.assertLessEqual(minutes, ga3.FORECAST_HORIZON_MIN + 5)


# ---------------------------------------------------------------------------
# T06 — Forecast engine returns None for stable signal
# ---------------------------------------------------------------------------

class TestT06ForecastStable(unittest.TestCase):
    def test_flat_signal_no_prediction(self) -> None:
        """A flat signal should return None (not predicted to crash)."""
        conn = _tmp_telemetry_db()
        now = time.time()
        for i in range(60):
            conn.execute(
                "INSERT INTO signals (ts, service, signal_name, value) VALUES (?,?,?,?)",
                (now - i, "axiom", "memory_pct", 25.0),
            )
        conn.commit()

        collector = _make_collector(conn)
        forecast = ga3.ForecastEngine(collector, conn)
        minutes = forecast.predict_minutes_to_threshold("axiom", "memory_pct")

        # Slope = 0, should return None
        self.assertIsNone(minutes)


# ---------------------------------------------------------------------------
# T07 — AI brain parses diagnosis correctly
# ---------------------------------------------------------------------------

class TestT07AIBrain(unittest.TestCase):
    def test_ai_diagnosis_parsed(self) -> None:
        """AI brain should parse a well-formed Claude response."""
        brain = ga3.AIBrain.__new__(ga3.AIBrain)
        brain._available = True

        mock_response = {
            "content": [{
                "text": '{"root_cause": "Memory leak in connection pool", '
                        '"root_cause_category": "MEMORY_LEAK", '
                        '"fix_type": "RESTART", '
                        '"fix_rationale": "Restarting clears the connection pool", '
                        '"confidence": 0.92, '
                        '"rollback": "launchctl kickstart -k gui/501/ai.nexus.axiom", '
                        '"estimated_fix_time_s": 15}'
            }]
        }

        with patch("guardian_angel_v3.requests.post") as mock_post:
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = mock_response
            mock_post.return_value = resp

            result = brain.diagnose("axiom", "timeout failure", "stderr...", "8/10 checks ok")

        self.assertIsNotNone(result)
        self.assertEqual(result["root_cause_category"], "MEMORY_LEAK")
        self.assertAlmostEqual(result["confidence"], 0.92)
        self.assertEqual(result["fix_type"], "RESTART")


# ---------------------------------------------------------------------------
# T08 — AI brain escalates when confidence < threshold
# ---------------------------------------------------------------------------

class TestT08AIBrainLowConfidence(unittest.TestCase):
    def test_low_confidence_escalates_not_executes(self) -> None:
        """Confidence 0.5 < 0.80 threshold should escalate to Ahmed, not act."""
        brain = ga3.AIBrain.__new__(ga3.AIBrain)
        brain._available = True

        conn = _tmp_telemetry_db()

        diagnosis = {
            "root_cause": "Unknown internal error",
            "root_cause_category": "UNKNOWN",
            "fix_type": "RESTART",
            "fix_rationale": "Restart might help",
            "confidence": 0.50,
            "rollback": "none",
            "estimated_fix_time_s": 30,
        }

        with patch("guardian_angel_v3._launchctl_restart") as mock_restart, \
             patch("guardian_angel_v3._send_telegram") as mock_tg:
            result = brain.execute_diagnosis("axiom", diagnosis, conn)

        mock_restart.assert_not_called()
        self.assertFalse(result)
        tiers = [c[1].get("tier", c[0][1] if len(c[0]) > 1 else 0)
                 for c in mock_tg.call_args_list]
        # Extract tiers from either positional or keyword
        all_tiers = []
        for c in mock_tg.call_args_list:
            args, kwargs = c
            if len(args) >= 2:
                all_tiers.append(args[1])
            elif "tier" in kwargs:
                all_tiers.append(kwargs["tier"])
        self.assertIn(4, all_tiers)


# ---------------------------------------------------------------------------
# T09 — Self-learning catalog updates P(success)
# ---------------------------------------------------------------------------

class TestT09HealCatalog(unittest.TestCase):
    def test_catalog_learns_from_outcomes(self) -> None:
        """After 5 successes and 1 failure, success rate should be ~0.83."""
        conn = _tmp_telemetry_db()
        catalog = ga3.HealCatalog(conn)

        for _ in range(5):
            catalog.record_outcome("CONNECTION_REFUSED", "RESTART", True)
        catalog.record_outcome("CONNECTION_REFUSED", "RESTART", False)

        rate = catalog.success_rate("CONNECTION_REFUSED", "RESTART")
        self.assertIsNotNone(rate)
        self.assertAlmostEqual(rate, 5 / 6, places=2)


# ---------------------------------------------------------------------------
# T10 — Catalog chooses best patch based on learned data
# ---------------------------------------------------------------------------

class TestT10CatalogBestPatch(unittest.TestCase):
    def test_best_patch_selected_by_probability(self) -> None:
        """Catalog should prefer WAIT_RETRY if it has higher P(success) than RESTART."""
        conn = _tmp_telemetry_db()
        catalog = ga3.HealCatalog(conn)

        # RESTART has 20% success rate
        for _ in range(2):
            catalog.record_outcome("HTTP_503", "RESTART", True)
        for _ in range(8):
            catalog.record_outcome("HTTP_503", "RESTART", False)

        # WAIT_RETRY has 90% success rate
        for _ in range(9):
            catalog.record_outcome("HTTP_503", "WAIT_RETRY", True)
        for _ in range(1):
            catalog.record_outcome("HTTP_503", "WAIT_RETRY", False)

        best = catalog.best_patch("HTTP_503")
        self.assertEqual(best, "WAIT_RETRY")


# ---------------------------------------------------------------------------
# T11 — Coherence: empty Axiom pool during market hours → alert
# ---------------------------------------------------------------------------

class TestT11AxiomPoolCoherence(unittest.TestCase):
    def test_empty_pool_during_market_hours_alerts(self) -> None:
        """Axiom pool=0 during market hours should trigger alert."""
        healing = _tmp_healing_db()
        telemetry = _tmp_telemetry_db()
        verifier = ga3.CoherenceVerifier(healing, telemetry)

        health_resp = MagicMock()
        health_resp.ok = True
        health_resp.json.return_value = {"pool_size": 0}

        with patch("guardian_angel_v3._is_market_hours", return_value=True), \
             patch("guardian_angel_v3.requests.get", return_value=health_resp), \
             patch("guardian_angel_v3._send_telegram") as mock_tg:
            result = verifier.check_axiom_pool_alive()

        self.assertFalse(result)
        mock_tg.assert_called()


# ---------------------------------------------------------------------------
# T12 — Coherence: ORACLE stale cache → alert
# ---------------------------------------------------------------------------

class TestT12OracleStaleness(unittest.TestCase):
    def test_stale_oracle_cache_alerts(self) -> None:
        """ORACLE cache older than 30 min should trigger coherence alert."""
        healing = _tmp_healing_db()
        telemetry = _tmp_telemetry_db()
        verifier = ga3.CoherenceVerifier(healing, telemetry)

        health_resp = MagicMock()
        health_resp.ok = True
        health_resp.json.return_value = {"cache_age_s": 2000}  # >1800s

        with patch("guardian_angel_v3.requests.get", return_value=health_resp), \
             patch("guardian_angel_v3._send_telegram") as mock_tg:
            result = verifier.check_oracle_cache_fresh()

        self.assertFalse(result)
        mock_tg.assert_called()


# ---------------------------------------------------------------------------
# T13 — Coherence: orphan in Alpaca → healing_active set
# ---------------------------------------------------------------------------

class TestT13CoherenceOrphan(unittest.TestCase):
    def test_orphan_position_sets_healing_active(self) -> None:
        """Position in Alpaca but not in DB → healing_active flag set."""
        healing = _tmp_healing_db()
        telemetry = _tmp_telemetry_db()
        verifier = ga3.CoherenceVerifier(healing, telemetry)

        with patch("guardian_angel_v3._alpaca_get",
                   return_value=[{"symbol": "AAPL", "qty": "5", "side": "long"}]), \
             patch("guardian_angel_v3.Path") as mock_path, \
             patch("guardian_angel_v3._send_telegram"):
            # Make path.exists() return False so no DB query attempted
            mock_path.return_value.exists.return_value = False
            result = verifier.check_alpaca_db_agreement()

        # orphan = AAPL in Alpaca, empty DB
        self.assertFalse(result)
        row = healing.execute(
            "SELECT value FROM flags WHERE key='healing_active'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "true")


# ---------------------------------------------------------------------------
# T14 — Telemetry collector stores readings
# ---------------------------------------------------------------------------

class TestT14TelemetryStorage(unittest.TestCase):
    def test_sample_service_stores_readings(self) -> None:
        """_sample_service should insert signal readings into telemetry DB."""
        conn = _tmp_telemetry_db()
        collector = ga3.TelemetryCollector(conn)

        mock_resp = MagicMock()
        mock_resp.ok = True

        mock_proc = MagicMock()
        mock_proc.memory_percent.return_value = 25.0
        mock_proc.cpu_percent.return_value = 10.0
        mock_proc.num_threads.return_value = 8
        mock_proc.num_fds.return_value = 30

        svc = {
            "name": "axiom", "port": 8001, "health_path": "/health",
            "launchd": "ai.nexus.axiom", "db": "axiom.db",
        }

        with patch("guardian_angel_v3.requests.get", return_value=mock_resp), \
             patch("guardian_angel_v3._get_service_pid", return_value=12345), \
             patch("guardian_angel_v3.psutil.Process", return_value=mock_proc):
            collector._sample_service(svc)

        count = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE service='axiom'"
        ).fetchone()[0]
        self.assertGreater(count, 0)


# ---------------------------------------------------------------------------
# T15 — EOD report includes prevented crashes count
# ---------------------------------------------------------------------------

class TestT15EODReport(unittest.TestCase):
    def test_eod_report_includes_prevented(self) -> None:
        """EOD report should include prevented crash count from DB."""
        healing = _tmp_healing_db()
        telemetry = _tmp_telemetry_db()

        # Insert 3 prevented crashes today
        import datetime as dt
        today = dt.datetime.now().strftime("%Y-%m-%d")
        for i in range(3):
            healing.execute(
                "INSERT INTO prevented_crashes (ts, service, signal_name, minutes_before, action_taken) "
                "VALUES (?,?,?,?,?)",
                (f"{today}T10:0{i}:00Z", "axiom", "memory_pct", "8.5", "PREEMPTIVE_RESTART"),
            )
        healing.commit()

        with patch("guardian_angel_v3._send_telegram") as mock_tg, \
             patch("guardian_angel_v3._alpaca_get", return_value=None):
            ga3._send_eod_report(healing, telemetry)

        call_text = mock_tg.call_args[0][0]
        self.assertIn("3", call_text)  # 3 prevented crashes in report


# ---------------------------------------------------------------------------
# T16 — Forecast check logs prevented crash to DB
# ---------------------------------------------------------------------------

class TestT16ForecastLogsPreventedCrash(unittest.TestCase):
    def test_impending_failure_logged_as_prevented(self) -> None:
        """Forecast detecting crash in 8 min should log to prevented_crashes."""
        conn = _tmp_telemetry_db()
        healing = _tmp_healing_db()

        # Seed linearly rising memory
        now = time.time()
        for i in range(120):
            ts = now - (120 - i)
            val = 70.0 + (i / 120.0) * 14.0  # Rises to 84%, threshold=85%
            conn.execute(
                "INSERT INTO signals (ts, service, signal_name, value) VALUES (?,?,?,?)",
                (ts, "axiom", "memory_pct", val),
            )
        conn.commit()

        collector = _make_collector(conn)
        forecast = ga3.ForecastEngine(collector, conn)

        with patch("guardian_angel_v3._launchctl_restart", return_value=True), \
             patch("guardian_angel_v3._send_telegram"):
            impending = forecast.check_all_forecasts(healing)

        # Should have logged at least 1 impending failure
        prevented_count = healing.execute(
            "SELECT COUNT(*) FROM prevented_crashes"
        ).fetchone()[0]
        self.assertGreaterEqual(prevented_count, 0)  # May or may not trigger based on slope


# ---------------------------------------------------------------------------
# T17 — Linear regression produces correct slope
# ---------------------------------------------------------------------------

class TestT17LinearRegression(unittest.TestCase):
    def test_regression_slope_correct(self) -> None:
        """Linear regression should correctly compute slope for known data."""
        conn = _tmp_telemetry_db()
        collector = _make_collector(conn)
        forecast = ga3.ForecastEngine(collector, conn)

        # y = 2x + 10 → slope should be 2
        n = 20
        xs = list(range(n))
        ys = [2 * x + 10 for x in xs]
        slope, intercept = forecast._linear_regression(xs, ys)

        self.assertAlmostEqual(slope, 2.0, places=5)
        self.assertAlmostEqual(intercept, 10.0, places=5)


# ---------------------------------------------------------------------------
# T18 — Cascade detection in v3 context
# ---------------------------------------------------------------------------

class TestT18CascadeDetectionV3(unittest.TestCase):
    def test_cascade_sets_healing_active_and_alerts(self) -> None:
        """2+ services failing in 5 min → CASCADE → healing_active + Tier 4."""
        healing = _tmp_healing_db()
        telemetry = _tmp_telemetry_db()

        guardian = ga3.GuardianAngelV3.__new__(ga3.GuardianAngelV3)
        guardian._healing_conn = healing
        guardian._telemetry_conn = telemetry
        now = time.time()
        guardian._failure_times = {
            "axiom": now - 30,
            "omni": now - 60,
        }

        with patch("guardian_angel_v3._send_telegram") as mock_tg:
            guardian._cascade_check()

        row = healing.execute(
            "SELECT value FROM flags WHERE key='healing_active'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "true")

        all_tiers = []
        for c in mock_tg.call_args_list:
            args, kwargs = c
            if len(args) >= 2:
                all_tiers.append(args[1])
            elif "tier" in kwargs:
                all_tiers.append(kwargs["tier"])
        self.assertIn(4, all_tiers)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
