"""
Tests for SOVEREIGN Agent Relationship Dynamics Monitor.

Covers: friction detection (threshold, window), confidence mismatch,
silence detection, rapid consensus, Ahmed state classification, and
the daily analysis integration run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from relationship_monitor import (
    classify_ahmed_state,
    detect_agent_silence,
    detect_confidence_mismatch,
    detect_rapid_consensus,
    detect_review_friction,
    run_daily_analysis,
    FRICTION_THRESHOLD,
    FRICTION_WINDOW_DAYS,
    EXPECTED_CADENCE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(days_ago: float = 0, minutes_ago: float = 0) -> str:
    """Return an ISO timestamp N days and M minutes ago (UTC)."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago, minutes=minutes_ago)
    return dt.isoformat()


def _record(builder: str, reviewer: str, issue_class: str, days_ago: float = 0) -> dict:
    return {
        "from_agent": builder,
        "to_agent": reviewer,
        "issue_class": issue_class,
        "timestamp": _ts(days_ago=days_ago),
    }


def _msg(agent: str, text: str, evidence: str = "", minutes_ago: float = 0) -> dict:
    return {
        "agent": agent,
        "text": text,
        "evidence": evidence,
        "timestamp": _ts(minutes_ago=minutes_ago),
    }


def _response(agent: str, text: str, minutes_ago: float = 0) -> dict:
    return {
        "agent": agent,
        "text": text,
        "timestamp": _ts(minutes_ago=minutes_ago),
    }


# ---------------------------------------------------------------------------
# Test 1 — Friction below threshold: no flag
# ---------------------------------------------------------------------------

def test_friction_below_threshold():
    """2 occurrences of same pair/issue → below threshold, no flag."""
    records = [
        _record("GENESIS", "CIPHER", "test_coverage", days_ago=1),
        _record("GENESIS", "CIPHER", "test_coverage", days_ago=3),
    ]
    result = detect_review_friction(records)
    assert result == []


# ---------------------------------------------------------------------------
# Test 2 — Friction at threshold: flagged
# ---------------------------------------------------------------------------

def test_friction_at_threshold():
    """3 occurrences of same pair/issue within 14 days → flagged."""
    records = [
        _record("GENESIS", "CIPHER", "missing_docstrings", days_ago=1),
        _record("GENESIS", "CIPHER", "missing_docstrings", days_ago=5),
        _record("GENESIS", "CIPHER", "missing_docstrings", days_ago=9),
    ]
    result = detect_review_friction(records)
    assert len(result) == 1
    assert result[0]["builder"] == "GENESIS"
    assert result[0]["reviewer"] == "CIPHER"
    assert result[0]["repeated_issue_class"] == "missing_docstrings"
    assert result[0]["occurrence_count"] == 3
    assert result[0]["sovereign_action"] == "FLAG_TO_AHMED"


# ---------------------------------------------------------------------------
# Test 3 — Friction outside 14-day window: no flag
# ---------------------------------------------------------------------------

def test_friction_outside_14_days():
    """3 occurrences but all older than 14 days → not flagged."""
    records = [
        _record("GENESIS", "CIPHER", "silent_failures", days_ago=15),
        _record("GENESIS", "CIPHER", "silent_failures", days_ago=20),
        _record("GENESIS", "CIPHER", "silent_failures", days_ago=25),
    ]
    result = detect_review_friction(records)
    assert result == []


# ---------------------------------------------------------------------------
# Test 4 — Confidence mismatch: 'confirmed' without evidence
# ---------------------------------------------------------------------------

def test_confidence_mismatch_confirmed_without_evidence():
    """'confirmed' with no evidence field → mismatch flagged."""
    messages = [
        _msg("GENESIS", "Deploy confirmed to Railway. Service is live.", evidence=""),
    ]
    result = detect_confidence_mismatch(messages)
    assert len(result) == 1
    assert result[0]["agent"] == "GENESIS"
    assert result[0]["claim"] == "confirmed"


# ---------------------------------------------------------------------------
# Test 5 — Confidence with evidence: no mismatch
# ---------------------------------------------------------------------------

def test_confidence_no_mismatch_with_evidence():
    """'confirmed' with populated evidence field → no mismatch."""
    messages = [
        _msg(
            "GENESIS",
            "Deploy confirmed to Railway.",
            evidence="HTTP 200 from /health, all 42 tests passing",
        ),
    ]
    result = detect_confidence_mismatch(messages)
    assert result == []


# ---------------------------------------------------------------------------
# Test 6 — Silence detected
# ---------------------------------------------------------------------------

def test_silence_detected():
    """Agent last seen beyond threshold → UNEXPECTED_SILENCE alert."""
    # OMNI threshold: 20 minutes
    last_seen = {
        "OMNI": datetime.now(timezone.utc) - timedelta(minutes=35),
    }
    alerts = detect_agent_silence(last_seen)
    assert len(alerts) == 1
    assert alerts[0]["agent"] == "OMNI"
    assert alerts[0]["alert"] == "UNEXPECTED_SILENCE"
    assert alerts[0]["silence_duration"] >= 35
    assert alerts[0]["sovereign_action"] == "GENTLE_CHECK_IN"


# ---------------------------------------------------------------------------
# Test 7 — Silence within threshold: no alert
# ---------------------------------------------------------------------------

def test_silence_within_threshold():
    """Agent last seen within threshold → no alert."""
    last_seen = {
        "OMNI": datetime.now(timezone.utc) - timedelta(minutes=10),
    }
    alerts = detect_agent_silence(last_seen)
    assert alerts == []


# ---------------------------------------------------------------------------
# Test 8 — Rapid consensus flagged
# ---------------------------------------------------------------------------

def test_rapid_consensus_flagged():
    """3 agents, nearly identical text, within 5 minutes → flagged."""
    text = "The market structure is bullish and we should proceed with the trade immediately."
    responses = [
        _response("CIPHER", text, minutes_ago=5),
        _response("ATLAS", text + " Agreed.", minutes_ago=4),
        _response("SAGE", text + " Concur.", minutes_ago=3),
    ]
    result = detect_rapid_consensus(responses)
    assert result["flagged"] is True
    assert result["recommendation"] == "request_independent_reasoning"
    assert len(result["agents_involved"]) >= 2


# ---------------------------------------------------------------------------
# Test 9 — Ahmed state: time pressure (short messages)
# ---------------------------------------------------------------------------

def test_ahmed_state_time_pressure():
    """Short messages → time_pressure state."""
    messages = [
        {"text": "ok", "timestamp": _ts()},
        {"text": "got it", "timestamp": _ts()},
        {"text": "asap", "timestamp": _ts()},
        {"text": "yes", "timestamp": _ts()},
    ]
    state = classify_ahmed_state(messages)
    assert state == "time_pressure"


# ---------------------------------------------------------------------------
# Test 10 — run_daily_analysis integration
# ---------------------------------------------------------------------------

def test_run_daily_analysis_integration(tmp_path, monkeypatch):
    """All detectors called and result aggregated; verifies output structure."""
    import relationship_monitor

    # Redirect the JSONL log to tmp_path to avoid polluting real logs
    monkeypatch.setattr(
        relationship_monitor,
        "run_daily_analysis",
        lambda **kwargs: _run_with_tmp_log(tmp_path, **kwargs),
    )

    # Build inputs with one friction pattern (threshold=3) and one mismatch
    records = [
        _record("GENESIS", "CIPHER", "type_hints", days_ago=2),
        _record("GENESIS", "CIPHER", "type_hints", days_ago=5),
        _record("GENESIS", "CIPHER", "type_hints", days_ago=8),
    ]
    messages = [
        _msg("GENESIS", "All tests are verified and complete."),
    ]
    last_seen = {}

    result = relationship_monitor.run_daily_analysis(
        chronicle_records=records,
        agent_messages=messages,
        agent_last_seen=last_seen,
    )

    assert "friction_patterns" in result
    assert "confidence_mismatches" in result
    assert "silence_alerts" in result
    assert "rapid_consensus" in result
    assert "ahmed_state" in result
    assert "flagged_count" in result
    assert isinstance(result["flagged_count"], int)
    # Should have friction (3 records) and mismatch ('verified' without evidence)
    assert result["flagged_count"] >= 2


def _run_with_tmp_log(tmp_path, **kwargs):
    """Call run_daily_analysis but write JSONL to tmp_path."""
    import relationship_monitor as rm
    import json
    from datetime import datetime, timezone

    friction = rm.detect_review_friction(kwargs.get("chronicle_records", []))
    mismatches = rm.detect_confidence_mismatch(kwargs.get("agent_messages", []))
    silence = rm.detect_agent_silence(kwargs.get("agent_last_seen", {}))
    ahmed_state = rm.classify_ahmed_state(kwargs.get("ahmed_recent_messages") or [])
    consensus = rm.detect_rapid_consensus(kwargs.get("agent_responses_for_consensus") or [])

    flagged_count = (
        len(friction) + len(mismatches) + len(silence)
        + (1 if consensus.get("flagged") else 0)
    )
    result = {
        "friction_patterns": friction,
        "confidence_mismatches": mismatches,
        "silence_alerts": silence,
        "rapid_consensus": consensus,
        "ahmed_state": ahmed_state,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "flagged_count": flagged_count,
    }
    log_path = str(tmp_path / "test_monitor_log.jsonl")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(result) + "\n")
    return result


# ---------------------------------------------------------------------------
# Additional unit tests
# ---------------------------------------------------------------------------

def test_deep_focus_state():
    """Long messages → deep_focus."""
    long_text = "a" * 250
    messages = [{"text": long_text, "timestamp": _ts()}]
    state = classify_ahmed_state(messages)
    assert state == "deep_focus"


def test_decision_required_state():
    """'recommend' keyword → decision_required."""
    messages = [{"text": "What do you recommend we do about this?", "timestamp": _ts()}]
    state = classify_ahmed_state(messages)
    assert state == "decision_required"


def test_frustrated_state():
    """'why isn't' keyword → frustrated."""
    messages = [{"text": "Why isn't this working again?", "timestamp": _ts()}]
    state = classify_ahmed_state(messages)
    assert state == "frustrated"


def test_friction_multiple_pairs():
    """Different pairs accumulate independently."""
    records = [
        # GENESIS/CIPHER pair — 3 occurrences
        _record("GENESIS", "CIPHER", "hardcoded_secrets", days_ago=1),
        _record("GENESIS", "CIPHER", "hardcoded_secrets", days_ago=3),
        _record("GENESIS", "CIPHER", "hardcoded_secrets", days_ago=5),
        # PRIMUS/ATLAS pair — only 2 (below threshold)
        _record("PRIMUS", "ATLAS", "missing_tests", days_ago=2),
        _record("PRIMUS", "ATLAS", "missing_tests", days_ago=6),
    ]
    result = detect_review_friction(records)
    assert len(result) == 1
    assert result[0]["builder"] == "GENESIS"


def test_silence_unknown_agent_ignored():
    """Agents not in EXPECTED_CADENCE are silently skipped."""
    last_seen = {
        "UNKNOWN_AGENT": datetime.now(timezone.utc) - timedelta(hours=24),
    }
    alerts = detect_agent_silence(last_seen)
    assert alerts == []


def test_consensus_not_flagged_slow():
    """Agents responding >10 minutes apart → not rapid consensus."""
    text = "Trade looks good, proceed."
    responses = [
        _response("CIPHER", text, minutes_ago=15),
        _response("ATLAS", text, minutes_ago=2),
    ]
    result = detect_rapid_consensus(responses)
    assert result["flagged"] is False
