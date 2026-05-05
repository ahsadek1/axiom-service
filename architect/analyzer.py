"""
analyzer.py — ARCHITECT Brain (Claude Sonnet 4.6) Analysis Engine

Calls Anthropic Claude Sonnet with ARCHITECT's structural coherence specialization.
Retries up to 3x with exponential backoff on failure.
"""

import json
import logging
import time
from typing import Optional

logger = logging.getLogger("architect.analyzer")

AGENT_NAME = "ARCHITECT"
BRAIN_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = (
    "You are ARCHITECT, an elite structural and architectural coherence specialist. "
    "You enforce CODE_MANDATE articles III-V and PRECISION MANDATE v1. "
    "You find single responsibility violations, duplicate logic across files/services, "
    "config in the wrong layer (hardcoded values that should be env vars), "
    "cross-service architectural inconsistencies, bare except clauses, missing type annotations, "
    "and missing docstrings. "
    "You ALWAYS return a valid JSON object with a 'findings' array. "
    "Each finding has: {severity: 'P0'|'P1'|'P2'|'P3', category: str, description: str, "
    "file: str, line_hint: str, recommendation: str}. "
    "P0 = architectural corruption, P1 = mandate violation, "
    "P2 = drift from pattern, P3 = improvement opportunity."
)

RETRY_DELAYS = [1.0, 2.0, 4.0]


def _build_user_message(
    service_name: str,
    service_version: str,
    code_files: list,
    spec_content: str,
    context: str,
) -> str:
    """Build the user prompt for the brain API."""
    files_block = "\n\n".join(
        f"### FILE: {f['path']}\n```\n{f['content']}\n```"
        for f in code_files
    )
    return (
        f"## Service: {service_name} v{service_version}\n\n"
        f"## Spec\n{spec_content}\n\n"
        f"## Context\n{context or 'No additional context provided.'}\n\n"
        f"## Code Files\n{files_block}\n\n"
        "Review against CODE_MANDATE Articles III-V: (1) Does every function do exactly one thing? "
        "(2) Are all config values in the right layer? (3) Is there any duplicate logic? "
        "(4) Are all error paths handled explicitly (no bare except)? "
        "(5) Are all functions typed and documented? "
        "Return ONLY a valid JSON object with a 'findings' array."
    )


def _call_brain(api_key: str, user_message: str) -> Optional[dict]:
    """
    Call Anthropic Claude Sonnet via anthropic SDK.
    Returns parsed JSON dict or None on failure.
    """
    if not api_key or api_key == "PLACEHOLDER_SET_BY_AHMED":
        logger.error("ARCHITECT: ANTHROPIC_API_KEY not configured — brain unavailable")
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=BRAIN_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        content = msg.content[0].text
        # Claude may wrap JSON in markdown — strip it
        text = content.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
        return json.loads(text)
    except Exception as e:
        logger.warning("ARCHITECT brain API call failed: %s", e)
        return None


def parse_findings(brain_output: dict) -> list:
    """Parse brain JSON output into structured findings list."""
    raw_findings = brain_output.get("findings", [])
    if not isinstance(raw_findings, list):
        logger.warning("ARCHITECT: brain output 'findings' is not a list")
        return []

    valid = []
    for f in raw_findings:
        if not isinstance(f, dict):
            continue
        severity = str(f.get("severity", "P3")).upper().strip()
        if severity not in ("P0", "P1", "P2", "P3"):
            severity = "P3"
        valid.append({
            "severity": severity,
            "category": str(f.get("category", "unknown"))[:200],
            "description": str(f.get("description", ""))[:1000],
            "file": str(f.get("file", "unknown"))[:300],
            "line_hint": str(f.get("line_hint", ""))[:200],
            "recommendation": str(f.get("recommendation", ""))[:1000],
        })
    return valid


def compute_assessment(findings: list) -> tuple:
    """Compute overall_assessment, p-counts, confidence from findings list."""
    p0 = sum(1 for f in findings if f["severity"] == "P0")
    p1 = sum(1 for f in findings if f["severity"] == "P1")
    p2 = sum(1 for f in findings if f["severity"] == "P2")
    p3 = sum(1 for f in findings if f["severity"] == "P3")

    if p0 > 0:
        assessment = "FAIL"
    elif p1 > 3:
        assessment = "CONDITIONAL"
    else:
        assessment = "PASS"

    total = len(findings)
    if total == 0:
        confidence = "LOW"
    elif total > 5:
        confidence = "HIGH"
    else:
        confidence = "MEDIUM"

    return assessment, p0, p1, p2, p3, confidence


def run_analysis(
    api_key: str,
    service_name: str,
    service_version: str,
    code_files: list,
    spec_content: str,
    context: str,
) -> Optional[dict]:
    """
    Run ARCHITECT analysis with 3-retry exponential backoff.
    Returns structured result dict or None if all retries fail.
    """
    user_message = _build_user_message(
        service_name, service_version, code_files, spec_content, context
    )

    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        logger.info("ARCHITECT: brain call attempt %d/3 for %s v%s", attempt, service_name, service_version)
        result = _call_brain(api_key, user_message)
        if result is not None:
            findings = parse_findings(result)
            assessment, p0, p1, p2, p3, confidence = compute_assessment(findings)
            logger.info(
                "ARCHITECT: analysis complete — %d findings (P0=%d P1=%d P2=%d P3=%d) assessment=%s",
                len(findings), p0, p1, p2, p3, assessment,
            )
            return {
                "findings": findings,
                "overall_assessment": assessment,
                "p0_count": p0,
                "p1_count": p1,
                "p2_count": p2,
                "p3_count": p3,
                "confidence_level": confidence,
            }

        if attempt < len(RETRY_DELAYS):
            logger.warning("ARCHITECT: brain failure attempt %d — retrying in %.1fs", attempt, delay)
            time.sleep(delay)
        else:
            logger.error("ARCHITECT: all 3 brain API attempts failed for %s v%s", service_name, service_version)

    return None
