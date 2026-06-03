"""test_collector.py — Unit tests for the collector module."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from collector import collect_service


class TestCollector(unittest.TestCase):

    def test_collect_service_success(self):
        """collect_service returns UP snapshot with parsed data on HTTP 200."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "healthy", "trades_today": 0}
        with patch("collector.requests.get", return_value=mock_resp):
            snap = collect_service("alpha_execution", "http://127.0.0.1:8005/health")
        self.assertEqual(snap.status, "UP")
        self.assertEqual(snap.data["trades_today"], 0)
        self.assertIsNone(snap.error)

    def test_collect_service_down_on_non_200(self):
        """collect_service returns DOWN on HTTP 503."""
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        with patch("collector.requests.get", return_value=mock_resp):
            snap = collect_service("alpha_execution", "http://127.0.0.1:8005/health")
        self.assertEqual(snap.status, "DOWN")
        self.assertIn("503", snap.error)

    def test_collect_service_down_on_connection_error(self):
        """collect_service returns DOWN on connection refused."""
        with patch("collector.requests.get", side_effect=Exception("connection refused")):
            snap = collect_service("alpha_execution", "http://127.0.0.1:8005/health")
        self.assertEqual(snap.status, "DOWN")
        self.assertIsNotNone(snap.error)

    def test_collect_service_name_preserved(self):
        """collect_service preserves the service name in the snapshot."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        with patch("collector.requests.get", return_value=mock_resp):
            snap = collect_service("omni", "http://127.0.0.1:8004/health")
        self.assertEqual(snap.name, "omni")


if __name__ == "__main__":
    unittest.main()
