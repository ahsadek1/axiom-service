"""
test_quad_intelligence.py — Quad Intelligence unit tests.

All API calls mocked — no real AI calls in tests.
Tests parallel execution, error handling, response parsing, and timeout recovery.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import pytest
from unittest.mock import patch, MagicMock
from quad_intelligence import (
    run_all_brains,
    _parse_brain_response,
    _error_result,
    _call_brain,
    BRAIN_CLAUDE,
    BRAIN_O3MINI,
    BRAIN_GEMINI,
    BRAIN_DEEPSEEK,
)

VALID_RESPONSE = json.dumps({
    "vote":          "GO",
    "confidence":    85,
    "concern_1":     "Earnings in 10 days",
    "concern_2":     "None",
    "echo_chamber":  False,
    "reasoning":     "Strong momentum with institutional backing.",
})

VALID_RESPONSE_NO_GO = json.dumps({
    "vote":          "NO_GO",
    "confidence":    70,
    "concern_1":     "Overbought RSI",
    "concern_2":     "Resistance at 52-week high",
    "echo_chamber":  False,
    "reasoning":     "Extended move, poor risk/reward at this level.",
})


class TestParseResponse:
    """Test brain response parsing."""

    def test_parses_valid_go(self):
        result = _parse_brain_response(VALID_RESPONSE, BRAIN_CLAUDE)
        assert result["vote"]       == "GO"
        assert result["confidence"] == 85
        assert result["concern_1"]  == "Earnings in 10 days"
        assert result["echo_chamber"] is False
        assert "error" not in result

    def test_parses_no_go(self):
        result = _parse_brain_response(VALID_RESPONSE_NO_GO, BRAIN_O3MINI)
        assert result["vote"] == "NO_GO"

    def test_strips_markdown_fences(self):
        wrapped = f"```json\n{VALID_RESPONSE}\n```"
        result  = _parse_brain_response(wrapped, BRAIN_GEMINI)
        assert result["vote"] == "GO"
        assert "error" not in result

    def test_handles_invalid_json(self):
        result = _parse_brain_response("This is not JSON at all", BRAIN_DEEPSEEK)
        assert "error" in result
        assert result["vote"] is None

    def test_normalizes_invalid_vote(self):
        bad = json.dumps({
            "vote": "MAYBE", "confidence": 50,
            "concern_1": "None", "concern_2": "None",
            "echo_chamber": False, "reasoning": "test",
        })
        result = _parse_brain_response(bad, BRAIN_CLAUDE)
        assert result["vote"] == "CONDITIONAL"

    def test_clamps_confidence(self):
        out_of_range = json.dumps({
            "vote": "GO", "confidence": 150,
            "concern_1": "None", "concern_2": "None",
            "echo_chamber": False, "reasoning": "test",
        })
        result = _parse_brain_response(out_of_range, BRAIN_CLAUDE)
        assert result["confidence"] == 100

    def test_truncates_long_concerns(self):
        long_concern = "A" * 200
        resp = json.dumps({
            "vote": "GO", "confidence": 80,
            "concern_1": long_concern, "concern_2": "None",
            "echo_chamber": False, "reasoning": "test",
        })
        result = _parse_brain_response(resp, BRAIN_CLAUDE)
        assert len(result["concern_1"]) <= 120

    def test_includes_brain_and_dimension(self):
        result = _parse_brain_response(VALID_RESPONSE, BRAIN_CLAUDE)
        assert result["brain"]     == BRAIN_CLAUDE
        assert result["dimension"] == "synthesis"


class TestErrorResult:
    def test_error_result_has_error_key(self):
        result = _error_result(BRAIN_CLAUDE, "timeout")
        assert "error" in result
        assert result["error"] == "timeout"
        assert result["vote"] is None

    def test_error_result_has_brain_name(self):
        result = _error_result(BRAIN_O3MINI, "API error")
        assert result["brain"] == BRAIN_O3MINI


class TestRunAllBrains:
    """Test parallel brain execution with mocked API calls."""

    def _make_mock_response(self, content: str) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "content": [{"text": content}],              # Anthropic format
            "choices": [{"message": {"content": content}}],  # OpenAI/DeepSeek format
            "candidates": [{"content": {"parts": [{"text": content}]}}],  # Gemini format
        }
        resp.raise_for_status = MagicMock()
        return resp

    @patch("quad_intelligence.requests.post")
    def test_all_4_brains_called(self, mock_post):
        mock_post.return_value = self._make_mock_response(VALID_RESPONSE)
        results = run_all_brains(
            context           = {"ticker": "NVDA"},
            anthropic_api_key = "test-anthropic",
            openai_api_key    = "test-openai",
            gemini_api_key    = "test-gemini",
            deepseek_api_key  = "test-deepseek",
        )
        assert len(results) == 4
        assert BRAIN_CLAUDE   in results
        assert BRAIN_O3MINI   in results
        assert BRAIN_GEMINI   in results
        assert BRAIN_DEEPSEEK in results
        assert mock_post.call_count == 4

    @patch("quad_intelligence.requests.post")
    def test_brain_error_captured_not_raised(self, mock_post):
        """One brain's API failure should not crash the whole run."""
        def side_effect(url, **kwargs):
            if "anthropic" in url:
                raise Exception("API down")
            return self._make_mock_response(VALID_RESPONSE)

        mock_post.side_effect = side_effect
        results = run_all_brains(
            context           = {"ticker": "AAPL"},
            anthropic_api_key = "test",
            openai_api_key    = "test",
            gemini_api_key    = "test",
            deepseek_api_key  = "test",
        )
        # All 4 keys should be present
        assert len(results) == 4
        # Claude should have error
        assert "error" in results[BRAIN_CLAUDE]
        # Others should be valid
        assert results[BRAIN_O3MINI].get("vote") == "GO"

    @patch("quad_intelligence.requests.post")
    def test_go_votes_counted_correctly(self, mock_post):
        mock_post.return_value = self._make_mock_response(VALID_RESPONSE)
        results = run_all_brains(
            context           = {"ticker": "MSFT"},
            anthropic_api_key = "test",
            openai_api_key    = "test",
            gemini_api_key    = "test",
            deepseek_api_key  = "test",
        )
        go_votes = sum(1 for r in results.values() if r.get("vote") == "GO")
        assert go_votes == 4


class TestCallBrainIndividual:
    """Test individual brain call routing."""

    @patch("quad_intelligence._call_anthropic", return_value=VALID_RESPONSE)
    def test_claude_routes_to_anthropic(self, mock_fn):
        result = _call_brain(BRAIN_CLAUDE, '{"ticker":"NVDA"}', "test-key")
        mock_fn.assert_called_once()
        assert result["vote"] == "GO"

    @patch("quad_intelligence._call_openai", return_value=VALID_RESPONSE)
    def test_o3mini_routes_to_openai(self, mock_fn):
        result = _call_brain(BRAIN_O3MINI, '{"ticker":"NVDA"}', "test-key")
        mock_fn.assert_called_once()
        assert result["vote"] == "GO"

    @patch("quad_intelligence._call_gemini", return_value=VALID_RESPONSE)
    def test_gemini_routes_to_gemini(self, mock_fn):
        result = _call_brain(BRAIN_GEMINI, '{"ticker":"NVDA"}', "test-key")
        mock_fn.assert_called_once()

    @patch("quad_intelligence._call_deepseek", return_value=VALID_RESPONSE)
    def test_deepseek_routes_to_deepseek(self, mock_fn):
        result = _call_brain(BRAIN_DEEPSEEK, '{"ticker":"NVDA"}', "test-key")
        mock_fn.assert_called_once()
