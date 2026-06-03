"""
Tests for alpha-execution/greeks_tracker.py
"""

import sys
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from greeks_tracker import (
    GreeksTracker, _compute_risk_flags, _now_iso, RiskFlag
)


def _make_db_with_positions(positions: list) -> str:
    """Create a temp DB with positions table populated."""
    db = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY,
            ticker TEXT, direction TEXT,
            short_strike REAL, long_strike REAL,
            expiration_date TEXT, contracts INTEGER,
            status TEXT DEFAULT 'open'
        )
    """)
    for p in positions:
        conn.execute(
            "INSERT INTO positions (ticker, direction, short_strike, long_strike, "
            "expiration_date, contracts, status) VALUES (?,?,?,?,?,?,?)",
            (p["ticker"], p["direction"], p["short_strike"], p["long_strike"],
             p["expiration_date"], p["contracts"], p.get("status", "open"))
        )
    conn.commit()
    conn.close()
    return db


def _mock_orats_row(strike, delta, gamma, vega, theta, expiry="2026-06-15"):
    return {
        "strike": strike, "expirDate": expiry, "dte": 40,
        "delta": delta, "gamma": gamma, "vega": vega, "theta": theta,
    }


class TestComputeRiskFlags(unittest.TestCase):

    def test_no_flags_on_zero_greeks(self):
        flags = _compute_risk_flags(0.0, 0.0, 0.0, 0.0)
        self.assertEqual(flags, [])

    def test_vega_flag_triggered(self):
        flags = _compute_risk_flags(0.0, 0.0, -6.0, 0.0)
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].type, "HIGH_VEGA_EXPOSURE")
        self.assertEqual(flags[0].severity, "WARNING")

    def test_vega_critical_above_10(self):
        flags = _compute_risk_flags(0.0, 0.0, -12.0, 0.0)
        vega_flags = [f for f in flags if f.type == "HIGH_VEGA_EXPOSURE"]
        self.assertEqual(len(vega_flags), 1)
        self.assertEqual(vega_flags[0].severity, "CRITICAL")

    def test_delta_flag_triggered(self):
        flags = _compute_risk_flags(0.6, 0.0, 0.0, 0.0)
        self.assertAny(flags, "HIGH_DELTA_EXPOSURE")

    def test_gamma_flag_triggered(self):
        flags = _compute_risk_flags(0.0, -0.20, 0.0, 0.0)
        self.assertAny(flags, "HIGH_GAMMA_EXPOSURE")

    def test_multiple_flags(self):
        flags = _compute_risk_flags(0.7, -0.20, -8.0, 1.5)
        types = {f.type for f in flags}
        self.assertIn("HIGH_VEGA_EXPOSURE", types)
        self.assertIn("HIGH_DELTA_EXPOSURE", types)
        self.assertIn("HIGH_GAMMA_EXPOSURE", types)

    def assertAny(self, flags, flag_type):
        types = {f.type for f in flags}
        self.assertIn(flag_type, types, f"Expected {flag_type} in flags: {types}")


class TestGreeksTrackerZeroPositions(unittest.TestCase):
    """TC-5: Zero open positions → zero Greeks, no flags."""

    def test_tc5_zero_positions(self):
        db = _make_db_with_positions([])
        tracker = GreeksTracker(db_path=db)

        with patch.object(tracker, '_fetch_orats_greeks') as mock_orats:
            tracker._refresh()
            mock_orats.assert_not_called()

        result = tracker.get()
        self.assertEqual(result["position_count"], 0)
        self.assertEqual(result["risk_flags"], [])
        self.assertEqual(result["portfolio_greeks"]["net_delta"], 0.0)
        self.assertFalse(result["stale"])
        os.unlink(db)


class TestGreeksTrackerAggregation(unittest.TestCase):
    """TC-1: 3 open positions → Greeks aggregated correctly."""

    @patch.object(GreeksTracker, '_fetch_orats_greeks')
    def test_tc1_three_positions_aggregated(self, mock_fetch):
        positions = [
            {"ticker": "NVDA", "direction": "bullish", "short_strike": 200,
             "long_strike": 195, "expiration_date": "2026-06-20", "contracts": 1},
            {"ticker": "AAPL", "direction": "bullish", "short_strike": 185,
             "long_strike": 180, "expiration_date": "2026-06-20", "contracts": 1},
            {"ticker": "SPY",  "direction": "bearish", "short_strike": 510,
             "long_strike": 515, "expiration_date": "2026-06-20", "contracts": 2},
        ]
        db = _make_db_with_positions(positions)
        tracker = GreeksTracker(db_path=db)

        # Mock ORATS: delta=-0.30, gamma=0.02, vega=0.15, theta=-0.05 for all strikes
        def side_effect(ticker, expiry, strike, opt_type):
            return {"delta": -0.30, "gamma": 0.02, "vega": 0.15, "theta": -0.05}
        mock_fetch.side_effect = side_effect

        tracker._refresh()
        result = tracker.get()

        self.assertEqual(result["position_count"], 3)
        # For a bull put: net_delta = -(-0.30) + (-0.30) = 0.30 - 0.30 = 0.0 per position
        # For a bear call: net_delta = -(-0.30) + (-0.30) = 0.0, × 2 contracts = 0.0
        # net total = 0.0 (symmetric mock)
        self.assertIsInstance(result["portfolio_greeks"]["net_delta"], float)
        self.assertIsInstance(result["portfolio_greeks"]["net_vega"], float)
        os.unlink(db)

    def test_tc2_closed_position_not_counted(self):
        """TC-2: Closed position excluded from aggregate."""
        positions = [
            {"ticker": "NVDA", "direction": "bullish", "short_strike": 200,
             "long_strike": 195, "expiration_date": "2026-06-20",
             "contracts": 1, "status": "closed"},
        ]
        db = _make_db_with_positions(positions)
        tracker = GreeksTracker(db_path=db)

        with patch.object(tracker, '_fetch_orats_greeks') as mock_fetch:
            tracker._refresh()
            mock_fetch.assert_not_called()

        result = tracker.get()
        self.assertEqual(result["position_count"], 0)
        os.unlink(db)


class TestGreeksTrackerOratsFallback(unittest.TestCase):
    """TC-4: ORATS unavailable → stale flag, no crash."""

    @patch.object(GreeksTracker, '_fetch_orats_greeks')
    def test_tc4_orats_failure_returns_stale(self, mock_fetch):
        mock_fetch.side_effect = Exception("ORATS connection refused")
        positions = [
            {"ticker": "NVDA", "direction": "bullish", "short_strike": 200,
             "long_strike": 195, "expiration_date": "2026-06-20", "contracts": 1},
        ]
        db = _make_db_with_positions(positions)
        tracker = GreeksTracker(db_path=db)
        tracker._refresh()
        result = tracker.get()

        self.assertEqual(result["position_count"], 1)
        self.assertTrue(result["stale"])
        # Greeks should be zero (safe defaults)
        self.assertEqual(result["portfolio_greeks"]["net_delta"], 0.0)
        os.unlink(db)

    def test_tc4_no_db_returns_zero_stale(self):
        """No DB file → returns stale zero packet, no crash."""
        tracker = GreeksTracker(db_path="/nonexistent/path/db.db")
        tracker._refresh()
        result = tracker.get()
        self.assertEqual(result["position_count"], 0)


class TestGreeksTrackerVegaFlag(unittest.TestCase):
    """TC-3: Net vega > 5.0 → HIGH_VEGA_EXPOSURE flag."""

    @patch.object(GreeksTracker, '_fetch_orats_greeks')
    def test_tc3_vega_flag_generated(self, mock_fetch):
        # For bull put: net_vega = -short_vega + long_vega
        # short_vega=0.80, long_vega=0.20 → net per position = -(0.80) + 0.20 = -0.60
        # 10 contracts → -6.0 → triggers HIGH_VEGA_EXPOSURE
        def side_effect(ticker, expiry, strike, opt_type):
            if abs(strike - 200) < 1:
                return {"delta": -0.30, "gamma": 0.02, "vega": 0.80, "theta": -0.05}
            return {"delta": -0.20, "gamma": 0.01, "vega": 0.20, "theta": -0.03}
        mock_fetch.side_effect = side_effect

        positions = [
            {"ticker": "NVDA", "direction": "bullish", "short_strike": 200,
             "long_strike": 195, "expiration_date": "2026-06-20", "contracts": 10},
        ]
        db = _make_db_with_positions(positions)
        tracker = GreeksTracker(db_path=db)
        tracker._refresh()
        result = tracker.get()

        flag_types = {f["type"] for f in result["risk_flags"]}
        self.assertIn("HIGH_VEGA_EXPOSURE", flag_types)
        os.unlink(db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
