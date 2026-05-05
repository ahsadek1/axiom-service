"""
analyzer.py — CONTRACT Brain (Gemini 2.0 Flash) Analysis Engine

Calls Google Gemini 2.0 Flash with CONTRACT's integration contract specialization.
Retries up to 3x with exponential backoff on failure.
"""

import json
import logging
import time
from typing import Optional

logger = logging.getLogger("contract.analyzer")

AGENT_NAME = "CONTRACT"
BRAIN_MODEL = "gemini-2.0-flash"

SYSTEM_PROMPT = (
    "You are CONTRACT, an elite integration contract enforcement specialist for live trading systems. "
    "You find HTTP contract mismatches between caller and callee, response schema drift, auth header "
    "inconsistencies across services, timeout contract mismatches, and retry behavior inconsistencies. "
    "You ALWAYS return a valid JSON object with a 'findings' array. "
    "Each finding has: {severity: 'P0'|'P1'|'P2'|'P3', category: str, description: str, "
    "file: str, line_hint: str, recommendation: str}. "
    "P0 = contract broken (service will fail), P1 = contract drift, "
    "P2 = undocumented assumption, P3 = missing contract test."
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
        "For each external HTTP call: list the auth header used, payload fields sent, timeout, "
        "and retry behavior. Cross-reference with the target service's expected contract. "
        "Flag every mismatch. Return ONLY a valid JSON object with a 'findings' array."
    )


def _call_brain(api_key: str, user_message: str) -> Optional[dict]:
    """
    Call Google Gemini 2.0 Flash via google-generativeai SDK.
    Returns parsed JSON dict or None on failure.
    """
    if not api_key or api_key == "PLACEHOLDER_SET_BY_AHMED":
        logger.error("CONTRACT: GOOGLE_API_KEY not configured — brain unavailable")
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            BRAIN_MODEL,
            system_instruction=SYSTEM_PROMPT,
        )
        response = model.generate_content(
            user_message,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                max_output_tokens=4096,
                temperature=0.2,
            ),
        )
        return json.loads(response.text)
    except Exception as e:
        logger.warning("CONTRACT brain API call failed: %s", e)
        return None


def parse_findings(brain_output: dict) -> list:
    """Parse brain JSON output into structured findings list."""
    raw_findings = brain_output.get("findings", [])
    if not isinstance(raw_findings, list):
        logger.warning("CONTRACT: brain output 'findings' is not a list")
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
    Run CONTRACT analysis with 3-retry exponential backoff.
    Returns structured result dict or None if all retries fail.
    """
    user_message = _build_user_message(
        service_name, service_version, code_files, spec_content, context
    )

    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        logger.info("CONTRACT: brain call attempt %d/3 for %s v%s", attempt, service_name, service_version)
        result = _call_brain(api_key, user_message)
        if result is not None:
            findings = parse_findings(result)
            assessment, p0, p1, p2, p3, confidence = compute_assessment(findings)
            logger.info(
                "CONTRACT: analysis complete — %d findings (P0=%d P1=%d P2=%d P3=%d) assessment=%s",
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
            logger.warning("CONTRACT: brain failure attempt %d — retrying in %.1fs", attempt, delay)
            time.sleep(delay)
        else:
            logger.error("CONTRACT: all 3 brain API attempts failed for %s v%s", service_name, service_version)

    return None
