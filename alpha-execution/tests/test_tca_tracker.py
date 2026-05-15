"""
Tests for alpha-execution/tca_tracker.py — Transaction Cost Analysis
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tca_tracker import (
    init_tca_schema, record_trade, get_daily_summary,
    get_recent_records, SLIPPAGE_ALERT_THRESHOLD_PCT
)


def _make_db() -> str:
    db = tempfile.mktemp(suffix=".db")
    init_tca_schema(db)
    return db


class TestSlippageCalculation(unittest.TestCase):
    """TC-1: Correct slippage calculation with known prices."""

    def test_tc1_slippage_computed_correctly(self):
        db = _make_db()
        rec = record_trade(
            db_path=db,
            trade_id="alpha-20260411-NVDA-001",
            ticker="NVDA",
            strategy="Bull Put Spread",
            position_size_usd=2000,
            decision_ts="2026-04-11T09:31:00+00:00",
            theoretical_price=1.84,
            actual_fill_price=1.62,
            fill_ts="2026-04-11T09:31:04+00:00",
        )
        self.assertEqual(rec["status"], "complete")
        self.assertAlmostEqual(rec["slippage_raw"], 1.62 - 1.84, places=4)
        expected_pct = ((1.62 - 1.84) / 1.84) * 100
        self.assertAlmostEqual(rec["slippage_pct"], expected_pct, places=2)
        self.assertEqual(rec["fill_latency_ms"], 4000)
        os.unlink(db)

    def test_fill_latency_computed(self):
        db = _make_db()
        rec = record_trade(
            db_path=db,
            trade_id="alpha-latency-001",
            ticker="AAPL",
            strategy="Bear Call Spread",
            position_size_usd=1000,
            decision_ts="2026-04-11T10:00:00+00:00",
            theoretical_price=2.00,
            actual_fill_price=1.90,
            fill_ts="2026-04-11T10:00:10+00:00",  # 10 seconds later
        )
        self.assertEqual(rec["fill_latency_ms"], 10000)
        os.unlink(db)


class TestIncompleteFill(unittest.TestCase):
    """TC-4: Fill price unavailable → INCOMPLETE, no crash, no estimate."""

    def test_tc4_fill_price_none_marks_incomplete(self):
        db = _make_db()
        rec = record_trade(
            db_path=db,
            trade_id="alpha-incomplete-001",
            ticker="SPY",
            strategy="Bull Put Spread",
            position_size_usd=1500,
            decision_ts="2026-04-11T09:31:00+00:00",
            theoretical_price=1.50,
            actual_fill_price=None,  # fill confirmation missing
        )
        self.assertEqual(rec["status"], "incomplete_fill")
        self.assertIsNone(rec["slippage_pct"])
        self.assertIsNone(rec["slippage_raw"])
        os.unlink(db)

    def test_theoretical_none_marks_tca_unavailable(self):
        db = _make_db()
        rec = record_trade(
            db_path=db,
            trade_id="alpha-notheory-001",
            ticker="QQQ",
            strategy="Bull Put Spread",
            position_size_usd=1000,
            decision_ts="2026-04-11T09:31:00+00:00",
            theoretical_price=None,
            actual_fill_price=1.80,
        )
        self.assertEqual(rec["status"], "tca_unavailable")
        self.assertIsNone(rec["slippage_pct"])
        os.unlink(db)


class TestSlippageAlert(unittest.TestCase):
    """TC-2: Slippage > 15% → Telegram alert fires."""

    @patch("tca_tracker.requests.post")
    def test_tc2_high_slippage_fires_alert(self, mock_post):
        mock_post.return_value.status_code = 200
        db = _make_db()
        # theoretical=2.00, fill=1.50 → slippage=-25% (> threshold)
        rec = record_trade(
            db_path=db,
            trade_id="alpha-highslip-001",
            ticker="NVDA",
            strategy="Bull Put Spread",
            position_size_usd=2000,
            decision_ts="2026-04-11T09:31:00+00:00",
            theoretical_price=2.00,
            actual_fill_price=1.50,
            bot_token="fake-token",
            chat_id="12345",
        )
        self.assertEqual(rec["status"], "complete")
        self.assertTrue(abs(rec["slippage_pct"]) > SLIPPAGE_ALERT_THRESHOLD_PCT)
        mock_post.assert_called_once()
        os.unlink(db)

    @patch("tca_tracker.requests.post")
    def test_normal_slippage_no_alert(self, mock_post):
        db = _make_db()
        # theoretical=2.00, fill=1.90 → slippage=-5% (< threshold)
        record_trade(
            db_path=db,
            trade_id="alpha-normalslip-001",
            ticker="AAPL",
            strategy="Bull Put Spread",
            position_size_usd=1000,
            decision_ts="2026-04-11T09:31:00+00:00",
            theoretical_price=2.00,
            actual_fill_price=1.90,
            bot_token="fake-token",
            chat_id="12345",
        )
        mock_post.assert_not_called()
        os.unlink(db)


class TestDailySummary(unittest.TestCase):
    """TC-3: 30-day rolling average + daily summary."""

    def setUp(self):
        self.db = _make_db()

    def tearDown(self):
        os.unlink(self.db)

    def _insert_complete(self, trade_id, theoretical, fill, size=1000):
        return record_trade(
            db_path=self.db,
            trade_id=trade_id,
            ticker="TEST",
            strategy="Bull Put Spread",
            position_size_usd=size,
            decision_ts="2026-05-07T09:31:00+00:00",
            theoretical_price=theoretical,
            actual_fill_price=fill,
        )

    def test_tc5_eod_summary_correct_values(self):
        self._insert_complete("t1", 2.00, 1.90)  # -5%
        self._insert_complete("t2", 1.50, 1.35)  # -10%
        self._insert_complete("t3", 3.00, 2.70)  # -10%

        summary = get_daily_summary(self.db, date="2026-05-07")
        self.assertEqual(summary["trades_analyzed"], 3)
        self.assertIsNotNone(summary["avg_slippage_pct"])
        # avg of -5, -10, -10 = -8.33...
        self.assertAlmostEqual(summary["avg_slippage_pct"], -8.33, delta=0.1)

    def test_empty_day_returns_zero_analyzed(self):
        summary = get_daily_summary(self.db, date="2000-01-01")
        self.assertEqual(summary["trades_analyzed"], 0)
        self.assertIsNone(summary["avg_slippage_pct"])

    def test_incomplete_trades_excluded_from_avg(self):
        self._insert_complete("t1", 2.00, 1.90)  # -5%, complete
        record_trade(  # incomplete
            db_path=self.db,
            trade_id="t2-incomplete",
            ticker="TEST",
            strategy="Bull Put Spread",
            position_size_usd=1000,
            decision_ts="2026-05-07T09:31:00+00:00",
            theoretical_price=2.00,
            actual_fill_price=None,
        )
        summary = get_daily_summary(self.db, date="2026-05-07")
        self.assertEqual(summary["trades_analyzed"], 1)

    def test_get_recent_records_returns_list(self):
        self._insert_complete("r1", 2.0, 1.9)
        self._insert_complete("r2", 1.5, 1.4)
        records = get_recent_records(self.db, limit=10)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["trade_id"], "r2")  # newest first


if __name__ == "__main__":
    unittest.main(verbosity=2)
