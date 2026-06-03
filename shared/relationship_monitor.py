"""
SOVEREIGN — Agent Relationship Dynamics Monitor.

Daily analysis of agent interaction patterns.  NOT real-time.

Runs post-market (16:30 ET) as a scheduled SOVEREIGN job.  Reads patterns
from CHRONICLE logs, agent message history, and last-seen timestamps to
identify systemic friction, confidence/evidence mismatches, unexpected
silence, and rapid consensus — the four social intelligence signals that
individual agent outputs do not make visible.

Pattern descriptions (spec-exact):
  1. Repeated Review Friction   — same agent pair, same issue class, ≥3× in 14 days
  2. Confidence vs Evidence     — high-confidence language without supporting evidence
  3. Silence as Signal          — agent exceeds expected communication cadence
  4. Rapid Consensus            — multiple agents agree too quickly on a complex question
  5. Ahmed State Awareness      — classify Ahmed's current communication state

Output: dict with friction_patterns, confidence_mismatches, silence_alerts,
        rapid_consensus, ahmed_state, timestamp, flagged_count.

Results are logged to /Users/ahmedsadek/nexus/shared/relationship_monitor_log.jsonl
and, when flagged_count > 0, sent to SOVEREIGN's notification channel.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (spec-exact)
# ---------------------------------------------------------------------------

FRICTION_THRESHOLD: int = 3  # same agent pair, same issue class, within 14 days
FRICTION_WINDOW_DAYS: int = 14

EXPECTED_CADENCE: Dict[str, Dict[str, object]] = {
    "GENESIS": {"max_silence_minutes": 30,  "context": "active_build"},
    "PRIMUS":  {"max_silence_minutes": 60,  "context": "market_hours"},
    "OMNI":    {"max_silence_minutes": 20,  "context": "scanning_active"},
    "VECTOR":  {"max_silence_minutes": 45,  "context": "investigation_active"},
    "CIPHER":  {"max_silence_minutes": 120, "context": "standard_operation"},
}

# Words that signal high confidence — require supporting evidence
HIGH_CONFIDENCE_FLAGS: List[str] = [
    "confirmed", "verified", "resolved", "deployed", "fixed", "complete"
]

# Minimum vocabulary similarity ratio to flag rapid consensus
CONSENSUS_SIMILARITY_THRESHOLD: float = 0.80

# Time window in minutes for rapid consensus detection
CONSENSUS_WINDOW_MINUTES: int = 10


# ---------------------------------------------------------------------------
# Core detector functions
# ---------------------------------------------------------------------------

def detect_review_friction(chronicle_records: List[dict]) -> List[dict]:
    """Detect agent pairs showing repeated review friction.

    A friction pattern is a builder/reviewer pair where the same class of
    issue recurs >= FRICTION_THRESHOLD times within FRICTION_WINDOW_DAYS.

    Args:
        chronicle_records: List of CHRONICLE record dicts.  Each must have
            keys: 'from_agent', 'to_agent', 'issue_class', 'timestamp'
            (ISO format string).  Missing keys are skipped.

    Returns:
        List of friction pattern dicts, each containing:
            builder, reviewer, repeated_issue_class, occurrence_count,
            recommendation, sovereign_action.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=FRICTION_WINDOW_DAYS)

    # Build: (builder, reviewer, issue_class) → list of timestamps
    pair_issues: Dict[tuple, List[datetime]] = {}

    for record in chronicle_records:
        builder = record.get("from_agent", "")
        reviewer = record.get("to_agent", "")
        issue_class = record.get("issue_class", "")
        ts_raw = record.get("timestamp", "")

        if not (builder and reviewer and issue_class and ts_raw):
            continue

        try:
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        if ts < cutoff:
            continue

        key = (builder, reviewer, issue_class)
        pair_issues.setdefault(key, []).append(ts)

    friction_patterns = []
    for (builder, reviewer, issue_class), timestamps in pair_issues.items():
        if len(timestamps) >= FRICTION_THRESHOLD:
            friction_patterns.append({
                "builder": builder,
                "reviewer": reviewer,
                "repeated_issue_class": issue_class,
                "occurrence_count": len(timestamps),
                "recommendation": "Explicit alignment session required before next major build",
                "sovereign_action": "FLAG_TO_AHMED",
            })

    return friction_patterns


def detect_confidence_mismatch(agent_messages: List[dict]) -> List[dict]:
    """Flag messages using high-confidence language without evidence.

    Confidence flags: 'confirmed', 'verified', 'resolved', 'deployed',
    'fixed', 'complete'.  An evidence field is considered present when the
    message dict has a non-empty 'evidence' key.

    Args:
        agent_messages: List of agent message dicts.  Each must have keys:
            'agent' (str), 'text' (str), 'evidence' (str or None),
            'timestamp' (str, ISO format).

    Returns:
        List of mismatch dicts, each containing:
            agent, claim (the matched flag word), missing_evidence, flagged_at.
    """
    mismatches = []

    for msg in agent_messages:
        agent = msg.get("agent", "UNKNOWN")
        text = msg.get("text", "")
        evidence = msg.get("evidence", "") or ""
        timestamp = msg.get("timestamp", "")

        if not text:
            continue

        text_lower = text.lower()
        matched_flags = [flag for flag in HIGH_CONFIDENCE_FLAGS if flag in text_lower]

        if matched_flags and not evidence.strip():
            mismatches.append({
                "agent": agent,
                "claim": matched_flags[0],
                "missing_evidence": (
                    f"Message contains '{matched_flags[0]}' but no evidence field populated"
                ),
                "flagged_at": timestamp,
            })

    return mismatches


def detect_agent_silence(
    agent_last_seen: Dict[str, datetime],
    reference_time: Optional[datetime] = None,
) -> List[dict]:
    """Detect agents that have exceeded their expected communication cadence.

    Args:
        agent_last_seen: Dict of agent_name (str) → last_seen (datetime).
            Timezone-naive datetimes are treated as UTC.
        reference_time: Time to measure silence from.  Defaults to now (UTC).

    Returns:
        List of silence alert dicts, each containing:
            alert, agent, silence_duration (minutes), expected_max,
            sovereign_action.
    """
    now = reference_time or datetime.now(timezone.utc)
    alerts = []

    for agent, last_seen in agent_last_seen.items():
        cadence = EXPECTED_CADENCE.get(agent)
        if not cadence:
            continue

        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)

        silence_minutes = int((now - last_seen).total_seconds() / 60)
        threshold = int(cadence["max_silence_minutes"])

        if silence_minutes > threshold:
            alerts.append({
                "alert": "UNEXPECTED_SILENCE",
                "agent": agent,
                "silence_duration": silence_minutes,
                "expected_max": threshold,
                "sovereign_action": "GENTLE_CHECK_IN",
            })

    return alerts


def classify_ahmed_state(recent_messages: List[dict]) -> str:
    """Classify Ahmed's current communication state from recent messages.

    Uses message length, keywords, and timing patterns to infer state.
    This heuristic allows SOVEREIGN to calibrate its communication style.

    Args:
        recent_messages: Last 5 (or fewer) messages from Ahmed.  Each must
            have keys: 'text' (str), 'timestamp' (str, ISO format).

    Returns:
        One of: 'time_pressure' | 'deep_focus' | 'exploratory' |
                'frustrated' | 'warm_light' | 'decision_required' | 'unknown'.
    """
    if not recent_messages:
        return "unknown"

    texts = [m.get("text", "") for m in recent_messages]
    combined_lower = " ".join(texts).lower()
    avg_length = sum(len(t) for t in texts) / max(len(texts), 1)

    # Decision required: explicit ask for direction
    if any(phrase in combined_lower for phrase in [
        "what should", "recommend", "go or no", "approve", "your call", "decide"
    ]):
        return "decision_required"

    # Frustrated: repeated questions, 'why isn't'
    if any(phrase in combined_lower for phrase in [
        "why isn't", "why is it", "still not", "not working", "again?", "what happened"
    ]):
        return "frustrated"

    # Warm/light: humor or personal tone
    if any(phrase in combined_lower for phrase in [
        "lol", "haha", "nice", "love it", "great job", "beautiful", "perfect 🔥"
    ]):
        return "warm_light"

    # Exploratory: philosophical or open-ended
    if any(phrase in combined_lower for phrase in [
        "what do you think", "thoughts on", "curious about", "what if", "philosophy"
    ]):
        return "exploratory"

    # Time pressure: short messages
    if avg_length < 40:
        return "time_pressure"

    # Deep focus: long, detailed messages
    if avg_length > 200:
        return "deep_focus"

    return "unknown"


def detect_rapid_consensus(
    agent_responses: List[dict],
    reference_time: Optional[datetime] = None,
) -> dict:
    """Detect performed consensus — agents agreeing too quickly on a complex question.

    Flags when multiple agents answer the same question within
    CONSENSUS_WINDOW_MINUTES with >CONSENSUS_SIMILARITY_THRESHOLD vocabulary
    overlap.

    Args:
        agent_responses: List of response dicts.  Each must have keys:
            'agent' (str), 'text' (str), 'timestamp' (str, ISO format).
        reference_time: Reference time for window calculation.  Defaults to
            the earliest response timestamp.

    Returns:
        Dict with keys: flagged (bool), reason (str), agents_involved (list),
        recommendation (str).
    """
    if len(agent_responses) < 2:
        return {
            "flagged": False,
            "reason": "Fewer than 2 responses — no consensus to evaluate",
            "agents_involved": [],
            "recommendation": "",
        }

    # Parse timestamps
    parsed = []
    for resp in agent_responses:
        ts_raw = resp.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            parsed.append((resp.get("agent", "?"), resp.get("text", ""), ts))
        except (ValueError, TypeError):
            parsed.append((resp.get("agent", "?"), resp.get("text", ""), None))

    # Check if all within time window
    valid_times = [ts for _, _, ts in parsed if ts is not None]
    if len(valid_times) >= 2:
        time_span = (max(valid_times) - min(valid_times)).total_seconds() / 60
        within_window = time_span <= CONSENSUS_WINDOW_MINUTES
    else:
        within_window = False

    if not within_window:
        return {
            "flagged": False,
            "reason": f"Responses spread over >{CONSENSUS_WINDOW_MINUTES} minutes — deliberation likely genuine",
            "agents_involved": [agent for agent, _, _ in parsed],
            "recommendation": "",
        }

    # Check vocabulary similarity across all pairs
    texts = [text for _, text, _ in parsed]
    high_similarity_pairs = []

    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            if not texts[i] or not texts[j]:
                continue
            ratio = SequenceMatcher(None, texts[i].lower(), texts[j].lower()).ratio()
            if ratio > CONSENSUS_SIMILARITY_THRESHOLD:
                high_similarity_pairs.append((parsed[i][0], parsed[j][0], ratio))

    if high_similarity_pairs:
        agents_involved = list({a for pair in high_similarity_pairs for a in pair[:2]})
        return {
            "flagged": True,
            "reason": (
                f"{len(high_similarity_pairs)} agent pair(s) show >{CONSENSUS_SIMILARITY_THRESHOLD:.0%} "
                f"vocabulary similarity within {CONSENSUS_WINDOW_MINUTES} minutes"
            ),
            "agents_involved": agents_involved,
            "recommendation": "request_independent_reasoning",
        }

    return {
        "flagged": False,
        "reason": "Responses arrived quickly but reasoning is independently expressed",
        "agents_involved": [agent for agent, _, _ in parsed],
        "recommendation": "",
    }


# ---------------------------------------------------------------------------
# Daily analysis runner
# ---------------------------------------------------------------------------

def run_daily_analysis(
    chronicle_records: List[dict],
    agent_messages: List[dict],
    agent_last_seen: Dict[str, datetime],
    ahmed_recent_messages: Optional[List[dict]] = None,
    agent_responses_for_consensus: Optional[List[dict]] = None,
) -> dict:
    """Run all relationship dynamics detectors and return a consolidated report.

    Intended to be called once per day (post-market, 16:30 ET).

    Args:
        chronicle_records: CHRONICLE records for friction detection.
        agent_messages: Agent message history for confidence mismatch detection.
        agent_last_seen: Dict of agent_name → last_seen datetime for silence detection.
        ahmed_recent_messages: Last 5 Ahmed messages for state classification.
            Defaults to [] if None.
        agent_responses_for_consensus: Agent responses for consensus check.
            Defaults to [] if None.

    Returns:
        Dict with: friction_patterns, confidence_mismatches, silence_alerts,
        rapid_consensus, ahmed_state, timestamp, flagged_count.
    """
    friction = detect_review_friction(chronicle_records)
    mismatches = detect_confidence_mismatch(agent_messages)
    silence = detect_agent_silence(agent_last_seen)
    ahmed_state = classify_ahmed_state(ahmed_recent_messages or [])
    consensus = detect_rapid_consensus(agent_responses_for_consensus or [])

    flagged_count = (
        len(friction)
        + len(mismatches)
        + len(silence)
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

    if flagged_count > 0:
        logger.warning(
            "RelationshipMonitor: %d flag(s) detected — friction=%d mismatches=%d "
            "silence=%d consensus_flagged=%s",
            flagged_count, len(friction), len(mismatches), len(silence),
            consensus.get("flagged", False),
        )
    else:
        logger.info("RelationshipMonitor: daily analysis complete — no flags")

    # Append to JSONL log
    log_path = "/Users/ahmedsadek/nexus/shared/relationship_monitor_log.jsonl"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(result) + "\n")
    except Exception as exc:
        logger.error("RelationshipMonitor: failed to write log: %s", exc)

    return result
