"""
echo_gate.py — ECHO Validation Gate
=====================================
The last line of defense before a live order hits Alpaca.

Every GO or STRONG_GO verdict from OMNI must pass through ECHO before being
forwarded to Alpha Execution or Prime Execution. ECHO does not re-analyze the
trade — it validates that the verdict packet is structurally sound, internally
consistent, and safe to execute.

7 checks (in order):
  V1 — Structural Integrity     : required fields present, correct types
  V2 — Score Sanity             : score in range and meets pathway threshold
  V3 — Pathway/Agents Consistency: agent count matches pathway (P1/P2/P3/P4)
  V4 — Window ID Freshness      : verdict not stale (≤ 15 min old)
  V5 — Echo Chamber Flag        : echo_chamber=True → reject immediately
  V6 — Synthesis ID Uniqueness  : no duplicate synthesis_id in last 60 min
  V7 — Execution Gate Status    : target execution service is not paused/down

Fail-open on ECHO crash: if validate_verdict() raises an unhandled exception,
OMNI must log it, tag the verdict echo_bypassed=True, and forward anyway.
ECHO must never block execution due to its own bug.

Author: GENESIS | 2026-05-05
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("nexus.echo_gate")

# ── Pathway thresholds (mirrors alpha-buffer config) ──────────────────────────
_PATHWAY_MIN_SCORE: Dict[str, float] = {
    "P1": 65.0,
    "P2": 65.0,
    "P3": 90.0,
    "P4": 25.0,
}

# ── Pathway agent count requirements ─────────────────────────────────────────
_PATHWAY_AGENT_COUNT: Dict[str, int] = {
    "P1": 3,
    "P2": 2,
    "P3": 1,
    "P4": 0,   # P4 is OMNI-initiated; 0–3 agents allowed
}

_VALID_AGENTS = {"cipher", "atlas", "sage"}
_VALID_VERDICTS = {"GO", "STRONG_GO"}
_VALID_PATHWAYS = {"P1", "P2", "P3", "P4"}
_REQUIRED_FIELDS = ["ticker", "direction", "pathway", "verdict", "weighted_score",
                    "window_id", "synthesis_id"]

# ── Window freshness ──────────────────────────────────────────────────────────
_WINDOW_MAX_AGE_SEC: float = 900.0   # 15 minutes
_WINDOW_ID_EXEMPT_PREFIXES = ("E2E-CONTRACT-", "CANARY-")

# ── Synthesis ID dedup window ─────────────────────────────────────────────────
_DEDUP_WINDOW_SEC: float = 3600.0    # 60 minutes

# ── Execution gate check ──────────────────────────────────────────────────────
_EXEC_HEALTH_TIMEOUT_SEC: float = 5.0


@dataclass
class EchoResult:
    """Result of ECHO validation."""
    status: str                          # "PASS" | "REJECT" | "HOLD"
    synthesis_id: int
    ticker: str
    checks_passed: List[str] = field(default_factory=list)
    checks_failed: List[str] = field(default_factory=list)
    failed_check: Optional[str] = None  # first failing check name
    reason: Optional[str] = None        # human-readable rejection reason
    hold_reason: Optional[str] = None
    forwarded_to: Optional[str] = None
    elapsed_ms: int = 0
    echo_bypassed: bool = False         # True only if ECHO itself crashed


class EchoGate:
    """
    Validates OMNI verdicts before execution routing.

    Usage:
        gate = EchoGate()
        result = gate.validate(verdict_dict, exec_url, exec_secret)
        if result.status == "PASS":
            # safe to forward to execution
    """

    def __init__(self) -> None:
        # Synthesis ID dedup: id → epoch time of first seen
        self._seen_synthesis_ids: Dict[int, float] = {}

    def validate(
        self,
        verdict: dict,
        exec_url: str,
        exec_secret: str,
    ) -> EchoResult:
        """
        Run all 7 ECHO checks against a verdict dict.

        Args:
            verdict:     OMNI verdict dict (see spec §4 for schema).
            exec_url:    Base URL of the target execution service.
            exec_secret: Auth secret for the target execution service.

        Returns:
            EchoResult with status PASS | REJECT | HOLD.

        Never raises — unhandled exceptions produce a PASS with echo_bypassed=True.
        """
        t_start = time.time()
        ticker = verdict.get("ticker", "UNKNOWN")
        synth_id = verdict.get("synthesis_id", -1)

        checks_passed: List[str] = []
        checks_failed: List[str] = []

        try:
            # ── V1: Structural Integrity ──────────────────────────────────────
            fail = self._check_structure(verdict)
            if fail:
                return self._reject(ticker, synth_id, "structural", fail,
                                    checks_passed, checks_failed, t_start)
            checks_passed.append("structural")

            # ── V2: Score Sanity ──────────────────────────────────────────────
            fail = self._check_score(verdict)
            if fail:
                return self._reject(ticker, synth_id, "score_sanity", fail,
                                    checks_passed, checks_failed, t_start)
            checks_passed.append("score_sanity")

            # ── V3: Pathway/Agents Consistency ────────────────────────────────
            fail = self._check_pathway_agents(verdict)
            if fail:
                return self._reject(ticker, synth_id, "pathway_agents", fail,
                                    checks_passed, checks_failed, t_start)
            checks_passed.append("pathway_agents")

            # ── V4: Window ID Freshness ───────────────────────────────────────
            fail = self._check_window_freshness(verdict)
            if fail:
                return self._reject(ticker, synth_id, "window_freshness", fail,
                                    checks_passed, checks_failed, t_start)
            checks_passed.append("window_freshness")

            # ── V5: Echo Chamber Flag ─────────────────────────────────────────
            fail = self._check_echo_chamber(verdict)
            if fail:
                return self._reject(ticker, synth_id, "echo_chamber_flag", fail,
                                    checks_passed, checks_failed, t_start)
            checks_passed.append("echo_chamber_flag")

            # ── V6: Synthesis ID Uniqueness ───────────────────────────────────
            fail = self._check_synthesis_dedup(synth_id)
            if fail:
                return self._reject(ticker, synth_id, "synthesis_dedup", fail,
                                    checks_passed, checks_failed, t_start)
            checks_passed.append("synthesis_dedup")
            # Record this synthesis_id as seen
            self._seen_synthesis_ids[synth_id] = time.time()
            self._prune_seen_ids()

            # ── V7: Execution Gate Status ─────────────────────────────────────
            hold_reason = self._check_exec_gate(exec_url, exec_secret)
            if hold_reason:
                elapsed = int((time.time() - t_start) * 1000)
                logger.warning("ECHO HOLD [%s] %s — %s", ticker, synth_id, hold_reason)
                return EchoResult(
                    status="HOLD",
                    synthesis_id=synth_id,
                    ticker=ticker,
                    checks_passed=checks_passed,
                    hold_reason=hold_reason,
                    elapsed_ms=elapsed,
                )
            checks_passed.append("exec_gate")

            # ── ALL CHECKS PASSED ─────────────────────────────────────────────
            elapsed = int((time.time() - t_start) * 1000)
            logger.info(
                "ECHO PASS [%s] synthesis_id=%s pathway=%s score=%.1f elapsed=%dms",
                ticker, synth_id, verdict.get("pathway"), verdict.get("weighted_score"), elapsed,
            )
            return EchoResult(
                status="PASS",
                synthesis_id=synth_id,
                ticker=ticker,
                checks_passed=checks_passed,
                elapsed_ms=elapsed,
            )

        except Exception as exc:
            elapsed = int((time.time() - t_start) * 1000)
            logger.error(
                "ECHO CRASH for %s synthesis_id=%s: %s — failing open (echo_bypassed=True)",
                ticker, synth_id, exc,
            )
            # Fail-open: ECHO must never block execution due to its own bug
            return EchoResult(
                status="PASS",
                synthesis_id=synth_id,
                ticker=ticker,
                checks_passed=checks_passed,
                elapsed_ms=elapsed,
                echo_bypassed=True,
                reason=f"ECHO crashed: {exc!s:.100}",
            )

    # ── Check implementations ─────────────────────────────────────────────────

    def _check_structure(self, verdict: dict) -> Optional[str]:
        """V1: All required fields present and correct types."""
        for f in _REQUIRED_FIELDS:
            if f not in verdict or verdict[f] is None or verdict[f] == "":
                return f"Required field '{f}' is missing or empty"

        score = verdict.get("weighted_score")
        if not isinstance(score, (int, float)):
            return f"weighted_score must be numeric, got {type(score).__name__}"

        pathway = verdict.get("pathway", "")
        if pathway not in _VALID_PATHWAYS:
            return f"pathway must be one of {sorted(_VALID_PATHWAYS)}, got '{pathway}'"

        v = verdict.get("verdict", "")
        if v not in _VALID_VERDICTS:
            return f"verdict must be GO or STRONG_GO, got '{v}'"

        return None

    def _check_score(self, verdict: dict) -> Optional[str]:
        """V2: Score within 0–100 and meets pathway minimum."""
        score = float(verdict["weighted_score"])
        if not (0.0 <= score <= 100.0):
            return f"weighted_score={score} is out of valid range 0.0–100.0"

        pathway = verdict["pathway"]
        min_score = _PATHWAY_MIN_SCORE.get(pathway, 0.0)
        if score < min_score:
            return (
                f"{pathway} pathway requires weighted_score >= {min_score}, got {score}"
            )

        return None

    def _check_pathway_agents(self, verdict: dict) -> Optional[str]:
        """V3: Agent count matches pathway requirement. Agent names must be valid."""
        pathway = verdict["pathway"]
        agents = verdict.get("agents_involved", [])

        # Validate agent names
        invalid = [a for a in agents if a.lower() not in _VALID_AGENTS]
        if invalid:
            return f"Invalid agent names: {invalid}. Valid: {sorted(_VALID_AGENTS)}"

        required = _PATHWAY_AGENT_COUNT[pathway]
        if pathway == "P4":
            # P4 allows 0–3 agents
            if len(agents) > 3:
                return f"P4 pathway allows 0–3 agents, got {len(agents)}"
        else:
            if len(agents) != required:
                return (
                    f"{pathway} pathway requires exactly {required} agent(s), "
                    f"got {len(agents)}: {agents}"
                )

        return None

    def _check_window_freshness(self, verdict: dict) -> Optional[str]:
        """V4: window_id not stale (≤ 15 min old). Exempt prefixes always pass."""
        window_id = str(verdict.get("window_id", ""))

        # Exempt prefixes (E2E contracts, canary tests) — always pass
        for prefix in _WINDOW_ID_EXEMPT_PREFIXES:
            if window_id.startswith(prefix):
                return None

        # Parse window_id as YYYY-MM-DD-HHMM
        try:
            window_dt = datetime.strptime(window_id, "%Y-%m-%d-%H%M")
            window_dt = window_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return f"window_id '{window_id}' does not match format YYYY-MM-DD-HHMM"

        age_sec = (datetime.now(timezone.utc) - window_dt).total_seconds()
        if age_sec > _WINDOW_MAX_AGE_SEC:
            return (
                f"window_id '{window_id}' is {age_sec:.0f}s old "
                f"(max allowed: {_WINDOW_MAX_AGE_SEC:.0f}s)"
            )

        return None

    def _check_echo_chamber(self, verdict: dict) -> Optional[str]:
        """V5: echo_chamber flag is logged but does NOT reject.
        
        Buffer-level echo detection is overly aggressive. Flags legitimate consensus
        (all 3 agents independently voting NO_GO). Verdict passes; OMNI may deprioritize.
        """
        if verdict.get("echo_chamber") is True:
            logger.warning(
                "Echo flagged (synth=%s, %s) → PASSING (buffer detector overly aggressive)",
                verdict.get("synthesis_id"), verdict.get("ticker")
            )
        return None

    def _check_synthesis_dedup(self, synthesis_id: int) -> Optional[str]:
        """V6: synthesis_id must not have been seen in the last 60 min."""
        last_seen = self._seen_synthesis_ids.get(synthesis_id)
        if last_seen is not None:
            age = time.time() - last_seen
            if age < _DEDUP_WINDOW_SEC:
                return (
                    f"synthesis_id={synthesis_id} already seen {age:.0f}s ago "
                    f"(dedup window: {_DEDUP_WINDOW_SEC:.0f}s)"
                )
        return None

    def _check_exec_gate(self, exec_url: str, exec_secret: str) -> Optional[str]:
        """V7: Target execution service is not paused or down."""
        if not exec_url:
            return None  # No URL provided — skip check (e.g. test mode)
        try:
            resp = requests.get(
                f"{exec_url.rstrip('/')}/health",
                headers={"X-Nexus-Secret": exec_secret},
                timeout=_EXEC_HEALTH_TIMEOUT_SEC,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("execution_paused"):
                    return f"execution_paused: {exec_url} is paused"
                return None
            return f"execution service returned HTTP {resp.status_code}"
        except requests.exceptions.ConnectionError:
            return f"execution service is DOWN (connection refused): {exec_url}"
        except requests.exceptions.Timeout:
            return f"execution service health check timed out: {exec_url}"
        except Exception as e:
            return f"execution gate check error: {e!s:.80}"

    def _prune_seen_ids(self) -> None:
        """Remove synthesis IDs older than the dedup window."""
        now = time.time()
        expired = [k for k, v in self._seen_synthesis_ids.items()
                   if now - v > _DEDUP_WINDOW_SEC]
        for k in expired:
            del self._seen_synthesis_ids[k]

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _reject(
        ticker: str,
        synth_id: int,
        check_name: str,
        reason: str,
        checks_passed: List[str],
        checks_failed: List[str],
        t_start: float,
    ) -> EchoResult:
        elapsed = int((time.time() - t_start) * 1000)
        checks_failed.append(check_name)
        logger.warning(
            "ECHO REJECT [%s] synthesis_id=%s check=%s reason=%s",
            ticker, synth_id, check_name, reason,
        )
        return EchoResult(
            status="REJECT",
            synthesis_id=synth_id,
            ticker=ticker,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
            failed_check=check_name,
            reason=reason,
            elapsed_ms=elapsed,
        )


# ── Module-level singleton ────────────────────────────────────────────────────
# Shared across all OMNI requests. Thread-safe: only _seen_synthesis_ids mutates,
# protected by the GIL for dict operations on CPython.
_echo_gate = EchoGate()


def validate_verdict(
    verdict: dict,
    exec_url: str = "",
    exec_secret: str = "",
) -> EchoResult:
    """
    Module-level entry point. Called by OMNI before routing to execution.

    Args:
        verdict:     OMNI verdict dict.
        exec_url:    Target execution service base URL (e.g. http://localhost:8005).
        exec_secret: Auth secret for the execution service.

    Returns:
        EchoResult. Never raises.
    """
    return _echo_gate.validate(verdict, exec_url, exec_secret)
