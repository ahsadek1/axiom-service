"""
omni_consult.py — Tier 3 AI Escalation

When GENESIS fails to fix a component within 3 minutes, this module:
1. Sends diagnostic payload to OMNI /synthesize for independent analysis
2. Sends same context directly to Claude API (Cipher-brain style adversarial review)
3. Returns combined recommendations
"""

import json
import logging
from typing import Optional

import requests

from config import NEXUS_SECRET, ANTHROPIC_API_KEY, OMNI_URL

log = logging.getLogger("sentinel.omni_consult")

CONSULT_TIMEOUT = 90   # seconds — OMNI can be slow
CLAUDE_TIMEOUT  = 60


def consult_omni(component: str, failure_detail: str, log_tail: str, actions_tried: list[str]) -> Optional[str]:
    """
    POST diagnostic context to OMNI /synthesize.
    Returns OMNI's recommended action string, or None on failure.
    """
    context = {
        "component":     component,
        "failure":       failure_detail,
        "actions_tried": actions_tried,
        "log_tail":      log_tail[-2000:],   # cap at 2KB
    }
    payload = {
        "ticker":      "SENTINEL",
        "direction":   "diagnostic",
        "pathway":     "P1",
        "window_id":   "SENTINEL-ESCALATION",
        "agent_count": 3,
        "weighted_score": 90.0,
        "agents_involved": ["Cipher", "Atlas", "Sage"],
        "scores": {"Cipher": 90, "Atlas": 90, "Sage": 90},
        "reasoning": (
            f"SENTINEL TIER-3 ESCALATION — GENESIS could not fix {component} in 3 minutes.\n\n"
            f"FAILURE: {failure_detail}\n\n"
            f"ACTIONS TRIED: {'; '.join(actions_tried)}\n\n"
            f"LOG TAIL:\n{log_tail[-1000:]}\n\n"
            "You are a senior trading systems engineer. Diagnose the root cause of this failure "
            "and provide the exact shell commands or code changes needed to fix it. Be specific. "
            "Format your response as: ROOT_CAUSE: ... | FIX: ... | COMMANDS: ..."
        ),
    }
    try:
        resp = requests.post(
            f"{OMNI_URL}/synthesize",
            json=payload,
            headers={"X-Nexus-Secret": NEXUS_SECRET, "Content-Type": "application/json"},
            timeout=CONSULT_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            verdict    = data.get("verdict", "?")
            reasoning  = data.get("reasoning", "")
            confidence = data.get("confidence", 0)
            log.info("OMNI consultation complete: verdict=%s confidence=%s", verdict, confidence)
            return f"[OMNI verdict={verdict} conf={confidence}%] {reasoning[:500]}"
        log.error("OMNI consultation failed: HTTP %d — %s", resp.status_code, resp.text[:200])
        return None
    except Exception as e:
        log.error("OMNI consultation error: %s", e)
        return None


def consult_cipher_brain(component: str, failure_detail: str, log_tail: str, actions_tried: list[str]) -> Optional[str]:
    """
    Call Claude API directly with adversarial diagnostic prompt.
    Returns Claude's recommendation string, or None on failure.
    """
    if not ANTHROPIC_API_KEY:
        log.warning("No ANTHROPIC_API_KEY — skipping Cipher brain consultation")
        return None

    prompt = (
        f"You are a senior engineer doing adversarial review of a live trading system failure.\n\n"
        f"FAILED COMPONENT: {component}\n"
        f"FAILURE DETAIL: {failure_detail}\n"
        f"ACTIONS ALREADY TRIED: {'; '.join(actions_tried)}\n\n"
        f"RECENT LOG TAIL:\n{log_tail[-1500:]}\n\n"
        f"The system is Nexus V2 — a Python microservices trading pipeline on a Mac mini. "
        f"Services run as launchd agents (launchctl). Ports: "
        f"axiom=8001, alpha-buffer=8002, prime-buffer=8003, omni=8004, "
        f"alpha-exec=8005, prime-exec=8006, oracle=8007, ails=8008, "
        f"cipher=9001, atlas=9002, sage=9003. "
        f"Code at /Users/ahmedsadek/nexus/. Logs at /Users/ahmedsadek/nexus/logs/.\n\n"
        f"Find every way this can fail beyond what was already tried. "
        f"Provide the exact root cause and fix. "
        f"Format: ROOT_CAUSE: ... | FIX: ... | COMMANDS: ..."
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 1024,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=CLAUDE_TIMEOUT,
        )
        if resp.status_code == 200:
            text = resp.json()["content"][0]["text"]
            log.info("Cipher brain consultation complete (%d chars)", len(text))
            return f"[Cipher-Brain] {text[:600]}"
        log.error("Cipher brain API error: HTTP %d", resp.status_code)
        return None
    except Exception as e:
        log.error("Cipher brain consultation error: %s", e)
        return None


def run_tier3_consult(
    component:    str,
    failure_detail: str,
    log_tail:     str,
    actions_tried: list[str],
) -> dict:
    """
    Run both OMNI and Cipher-brain consultations in sequence.

    Returns:
        {
            "omni_recommendation":   str or None,
            "cipher_recommendation": str or None,
            "combined_summary":      str,
        }
    """
    log.warning("TIER 3 — consulting OMNI and Cipher brain for %s", component)

    omni_rec   = consult_omni(component, failure_detail, log_tail, actions_tried)
    cipher_rec = consult_cipher_brain(component, failure_detail, log_tail, actions_tried)

    parts = []
    if omni_rec:
        parts.append(f"OMNI: {omni_rec}")
    if cipher_rec:
        parts.append(f"Cipher: {cipher_rec}")

    summary = "\n\n".join(parts) if parts else "No recommendations available from either AI brain."

    return {
        "omni_recommendation":   omni_rec,
        "cipher_recommendation": cipher_rec,
        "combined_summary":      summary,
    }
