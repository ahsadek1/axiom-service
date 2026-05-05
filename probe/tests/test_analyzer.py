"""
test_analyzer.py — PROBE analyzer unit tests
"""

import json
import sys
import os
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analyzer import (
    compute_assessment,
    parse_findings,
    run_analysis,
    AGENT_NAME,
    BRAIN_MODEL,
)


# ── parse_findings ─────────────────────────────────────────────────────────────

def test_parse_findings_valid():
    brain_output = {
        "findings": [
            {"severity": "P0", "category": "SQL Injection", "description": "Unparameterized query",
             "file": "db.py", "line_hint": "42", "recommendation": "Use parameterized queries"},
            {"severity": "P2", "category": "Code smell", "description": "Long function",
             "file": "main.py", "line_hint": "100", "recommendation": "Refactor"},
        ]
    }
    result = parse_findings(brain_output)
    assert len(result) == 2
    assert result[0]["severity"] == "P0"
    assert result[1]["severity"] == "P2"


def test_parse_findings_normalizes_invalid_severity():
    brain_output = {"findings": [{"severity": "CRITICAL", "category": "test", "description": "x",
                                   "file": "a.py", "line_hint": "", "recommendation": "fix"}]}
    result = parse_findings(brain_output)
    assert result[0]["severity"] == "P3"  # unknown → P3


def test_parse_findings_empty():
    assert parse_findings({"findings": []}) == []


def test_parse_findings_missing_key():
    assert parse_findings({}) == []


def test_parse_findings_non_list():
    result = parse_findings({"findings": "not a list"})
    assert result == []


# ── compute_assessment ────────────────────────────────────────────────────────

def test_compute_assessment_fail_on_p0():
    findings = [{"severity": "P0", "category": "x", "description": "", "file": "", "line_hint": "", "recommendation": ""}]
    assessment, p0, p1, p2, p3, confidence = compute_assessment(findings)
    assert assessment == "FAIL"
    assert p0 == 1


def test_compute_assessment_conditional_on_many_p1():
    findings = [{"severity": "P1", "category": "x", "description": "", "file": "", "line_hint": "", "recommendation": ""} for _ in range(4)]
    assessment, p0, p1, p2, p3, confidence = compute_assessment(findings)
    assert assessment == "CONDITIONAL"
    assert p1 == 4


def test_compute_assessment_pass():
    findings = [{"severity": "P2", "category": "x", "description": "", "file": "", "line_hint": "", "recommendation": ""}]
    assessment, p0, p1, p2, p3, confidence = compute_assessment(findings)
    assert assessment == "PASS"


def test_findings_correct_severity():
    """Test 12: brain output parsed into P0/P1/P2/P3 correctly."""
    findings = [
        {"severity": "P0", "category": "c", "description": "d", "file": "f", "line_hint": "l", "recommendation": "r"},
        {"severity": "P1", "category": "c", "description": "d", "file": "f", "line_hint": "l", "recommendation": "r"},
        {"severity": "P2", "category": "c", "description": "d", "file": "f", "line_hint": "l", "recommendation": "r"},
        {"severity": "P3", "category": "c", "description": "d", "file": "f", "line_hint": "l", "recommendation": "r"},
    ]
    assessment, p0, p1, p2, p3, _ = compute_assessment(findings)
    assert p0 == 1 and p1 == 1 and p2 == 1 and p3 == 1
    assert assessment == "FAIL"  # has P0


# ── run_analysis ──────────────────────────────────────────────────────────────

def test_brain_failure_retries_3x():
    """Test 6: mock API failure → 3 retries → returns None."""
    with patch("analyzer._call_brain", return_value=None) as mock_call:
        result = run_analysis(
            "fake_key", "https://api.deepseek.com/v1",
            "test-service", "1.0.0",
            [{"path": "main.py", "content": "print('hi')"}],
            "spec content", "context",
        )
        assert result is None
        assert mock_call.call_count == 3


def test_run_analysis_success():
    brain_resp = {
        "findings": [
            {"severity": "P1", "category": "Logic", "description": "Bug",
             "file": "main.py", "line_hint": "10", "recommendation": "Fix it"}
        ]
    }
    with patch("analyzer._call_brain", return_value=brain_resp):
        result = run_analysis(
            "fake_key", "https://api.deepseek.com/v1",
            "test-service", "1.0.0",
            [{"path": "main.py", "content": "pass"}],
            "spec", "context",
        )
        assert result is not None
        assert result["p1_count"] == 1
        assert result["overall_assessment"] == "PASS"
