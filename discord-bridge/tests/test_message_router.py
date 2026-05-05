"""
test_message_router.py — Tests for MessageRouter classification.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from message_router import MessageRouter


class TestMessageRouter(unittest.TestCase):

    def setUp(self) -> None:
        self._router = MessageRouter()

    def test_ticker_routes_to_omni(self) -> None:
        """TC: 'AAPL' → agent=omni, action=ticker_query, context=AAPL"""
        result = self._router.classify("AAPL")
        self.assertEqual(result.agent, "omni")
        self.assertEqual(result.action, "ticker_query")
        self.assertEqual(result.context, "AAPL")

    def test_ticker_in_sentence_routes_to_omni(self) -> None:
        """TC: 'What do you think about NVDA?' → omni"""
        result = self._router.classify("What do you think about NVDA?")
        self.assertEqual(result.agent, "omni")
        self.assertEqual(result.action, "ticker_query")
        self.assertEqual(result.context, "NVDA")

    def test_status_routes_to_sovereign(self) -> None:
        """TC: 'status' → agent=sovereign, action=health_sweep"""
        result = self._router.classify("status")
        self.assertEqual(result.agent, "sovereign")
        self.assertEqual(result.action, "health_sweep")

    def test_health_routes_to_sovereign(self) -> None:
        result = self._router.classify("how is the system health?")
        self.assertEqual(result.agent, "sovereign")

    def test_execution_routes_to_alpha(self) -> None:
        """TC: 'trades' → agent=alpha-execution"""
        result = self._router.classify("show me today's trades")
        self.assertEqual(result.agent, "alpha-execution")
        self.assertEqual(result.action, "trades_summary")

    def test_positions_routes_to_alpha(self) -> None:
        result = self._router.classify("what are the current positions?")
        self.assertEqual(result.agent, "alpha-execution")

    def test_build_routes_to_genesis(self) -> None:
        """TC: 'build queue' → agent=genesis"""
        result = self._router.classify("what's in the build queue?")
        self.assertEqual(result.agent, "genesis")
        self.assertEqual(result.action, "build_status")

    def test_pipeline_routes_to_genesis(self) -> None:
        result = self._router.classify("show me the pipeline")
        self.assertEqual(result.agent, "genesis")

    def test_pause_routes_to_sovereign_with_action(self) -> None:
        """TC: 'pause execution' → agent=sovereign, action=pause_execution"""
        result = self._router.classify("pause execution")
        self.assertEqual(result.agent, "sovereign")
        self.assertEqual(result.action, "pause_execution")

    def test_resume_routes_to_sovereign_with_action(self) -> None:
        result = self._router.classify("resume execution now")
        self.assertEqual(result.agent, "sovereign")
        self.assertEqual(result.action, "resume_execution")

    def test_catch_all_routes_to_sovereign(self) -> None:
        """TC: unrecognized text → agent=sovereign, action=general_query"""
        result = self._router.classify("good morning")
        self.assertEqual(result.agent, "sovereign")
        self.assertEqual(result.action, "general_query")

    def test_empty_string_routes_to_sovereign(self) -> None:
        result = self._router.classify("")
        self.assertEqual(result.agent, "sovereign")

    def test_common_words_not_detected_as_tickers(self) -> None:
        """Words like IS, THE, AND must not be detected as tickers."""
        result = self._router.classify("what IS the status?")
        # Should route via 'status' keyword, not IS ticker
        self.assertEqual(result.agent, "sovereign")
        self.assertNotEqual(result.action, "ticker_query")

    def test_omni_keyword_routes_to_omni(self) -> None:
        result = self._router.classify("what does omni think about TSLA?")
        # 'omni' keyword matches omni concordance route
        self.assertEqual(result.agent, "omni")

    def test_thesis_routes_to_thesis(self) -> None:
        result = self._router.classify("what's the macro outlook?")
        self.assertEqual(result.agent, "thesis")

    def test_investigate_routes_to_vector(self) -> None:
        result = self._router.classify("investigate this ticker")
        self.assertEqual(result.agent, "vector")

    def test_pause_takes_priority_over_ticker(self) -> None:
        """'pause execution' must route to sovereign not ticker detection."""
        result = self._router.classify("pause execution now")
        self.assertEqual(result.agent, "sovereign")
        self.assertEqual(result.action, "pause_execution")


class TestTickerDetection(unittest.TestCase):

    def setUp(self) -> None:
        self._router = MessageRouter()

    def test_detects_simple_ticker(self) -> None:
        self.assertEqual(self._router._detect_ticker("AAPL"), "AAPL")

    def test_detects_ticker_in_sentence(self) -> None:
        self.assertEqual(self._router._detect_ticker("Looking at MSFT today"), "MSFT")

    def test_excludes_common_words(self) -> None:
        self.assertIsNone(self._router._detect_ticker("is it ok"))

    def test_excludes_single_letter(self) -> None:
        self.assertIsNone(self._router._detect_ticker("I want to know"))

    def test_detects_ticker_with_numbers_in_sentence(self) -> None:
        # NVDA is a valid ticker
        self.assertEqual(self._router._detect_ticker("NVDA looks good"), "NVDA")


if __name__ == "__main__":
    unittest.main()
