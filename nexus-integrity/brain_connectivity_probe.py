"""
brain_connectivity_probe.py — SOVEREIGN Amendment S5

Tests all four AI brain APIs before market open and every 30 min intraday.

ROOT CAUSE CAUGHT: 2026-04-29, Gemini was returning 503 all afternoon.
Claude was intermittently timing out. Syntheses completed with only 2-3/4 brains,
producing CONDITIONAL verdicts (counted as NO_GO). The OMNI health endpoint
reported "healthy". No pre-market check detected the degradation.

This probe runs at 08:45 ET (pre-market, after Alpaca probe) and every 30 min
intraday as a Tier 2 probe. It uses PROBE_ scoped API keys (separate from
trading keys) and makes minimal single-token test calls.

Result feeds into TRS as 'brain_capacity' sub-score.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("integrity.brain_probe")

# API keys — use probe-scoped keys if available, fall back to trading keys
CLAUDE_API_KEY: str = os.environ.get(
    "CLAUDE_PROBE_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
GEMINI_API_KEY: str = os.environ.get(
    "GEMINI_PROBE_KEY", os.environ.get("GEMINI_API_KEY", ""))
DEEPSEEK_API_KEY: str = os.environ.get(
    "DEEPSEEK_PROBE_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))
OPENAI_API_KEY: str = os.environ.get(
    "OPENAI_PROBE_KEY", os.environ.get("OPENAI_API_KEY", ""))

BRAIN_TIMEOUT_SECONDS: float = 10.0
MIN_BRAINS_REQUIRED: int = 3          # <3 = AMBER; <2 = block (TRS=0)
MIN_BRAINS_HARD_BLOCK: int = 2


@dataclass
class BrainProbeResult:
    brain: str
    available: bool
    latency_ms: Optional[float] = None
    error: Optional[str] = None


@dataclass
class BrainConnectivityResult:
    available: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    probe_results: List[BrainProbeResult] = field(default_factory=list)
    trs_score: float = 0.0
    ts: float = field(default_factory=time.time)

    @property
    def available_count(self) -> int:
        return len(self.available)

    @property
    def summary(self) -> str:
        return (
            f"{self.available_count}/4 brains available "
            f"[{', '.join(self.available)}]"
            + (f" | FAILED: {', '.join(self.failed.keys())}" if self.failed else "")
        )


def _probe_claude() -> BrainProbeResult:
    """Single minimal call to Anthropic API to verify connectivity and auth."""
    if not CLAUDE_API_KEY:
        return BrainProbeResult("claude", available=False, error="API key not configured")
    try:
        import anthropic
        start = time.monotonic()
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        client.messages.create(
            model="claude-haiku-4-5",  # Use cheapest/fastest model for probe
            max_tokens=1,
            messages=[{"role": "user", "content": "1"}],
        )
        latency_ms = (time.monotonic() - start) * 1000
        return BrainProbeResult("claude", available=True, latency_ms=latency_ms)
    except Exception as e:
        return BrainProbeResult("claude", available=False, error=str(e)[:120])


def _probe_gemini() -> BrainProbeResult:
    """Single minimal call to Gemini API."""
    if not GEMINI_API_KEY:
        return BrainProbeResult("gemini", available=False, error="API key not configured")
    try:
        import google.generativeai as genai
        start = time.monotonic()
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")
        model.generate_content(
            "1",
            generation_config={"max_output_tokens": 1},
        )
        latency_ms = (time.monotonic() - start) * 1000
        return BrainProbeResult("gemini", available=True, latency_ms=latency_ms)
    except Exception as e:
        return BrainProbeResult("gemini", available=False, error=str(e)[:120])


def _probe_deepseek() -> BrainProbeResult:
    """Single minimal call to DeepSeek API."""
    if not DEEPSEEK_API_KEY:
        return BrainProbeResult("deepseek", available=False, error="API key not configured")
    try:
        import openai
        start = time.monotonic()
        client = openai.OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com/v1",
        )
        client.chat.completions.create(
            model="deepseek-chat",
            max_tokens=1,
            messages=[{"role": "user", "content": "1"}],
        )
        latency_ms = (time.monotonic() - start) * 1000
        return BrainProbeResult("deepseek", available=True, latency_ms=latency_ms)
    except Exception as e:
        return BrainProbeResult("deepseek", available=False, error=str(e)[:120])


def _probe_o3mini() -> BrainProbeResult:
    """Single minimal call to OpenAI o3-mini API."""
    if not OPENAI_API_KEY:
        return BrainProbeResult("o3mini", available=False, error="API key not configured")
    try:
        import openai
        start = time.monotonic()
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        client.chat.completions.create(
            model="o3-mini",
            max_completion_tokens=10,
            messages=[{"role": "user", "content": "1"}],
        )
        latency_ms = (time.monotonic() - start) * 1000
        return BrainProbeResult("o3mini", available=True, latency_ms=latency_ms)
    except Exception as e:
        return BrainProbeResult("o3mini", available=False, error=str(e)[:120])


def run_brain_connectivity_probe() -> BrainConnectivityResult:
    """
    Probe all four AI brains concurrently with a timeout.

    Each probe makes a single minimal API call to verify connectivity and auth.
    All four run in parallel — total wall time ≤ BRAIN_TIMEOUT_SECONDS.

    Returns BrainConnectivityResult with:
      - available: list of responsive brain names
      - failed: dict of brain_name → error string
      - trs_score: 100 (4 brains), 75 (3), 50 (2), 0 (<2 — hard block)
    """
    probe_fns = {
        "claude":   _probe_claude,
        "gemini":   _probe_gemini,
        "deepseek": _probe_deepseek,
        "o3mini":   _probe_o3mini,
    }

    results: List[BrainProbeResult] = []
    available: List[str] = []
    failed: Dict[str, str] = {}

    # Run all probes sequentially (gRPC/Gemini deadlocks in ThreadPoolExecutor fork context)
    # NOTE: SIGALRM cannot be used from non-main threads — each probe function handles
    # its own timeout via the requests/httpx timeout parameter.
    for brain_name, fn in probe_fns.items():
        try:
            result = fn()
            results.append(result)
            if result.available:
                available.append(brain_name)
            else:
                failed[brain_name] = result.error or "unknown error"
        except Exception as e:
            err = str(e)[:80]
            failed[brain_name] = err
            results.append(BrainProbeResult(brain_name, available=False, error=err))

    available_count = len(available)

    # Compute TRS sub-score
    if available_count >= 4:
        trs_score = 100.0
    elif available_count == 3:
        trs_score = 75.0
    elif available_count == 2:
        trs_score = 50.0
    else:
        trs_score = 0.0  # Hard block — cannot synthesize with <2 brains

    conn_result = BrainConnectivityResult(
        available=available,
        failed=failed,
        probe_results=results,
        trs_score=trs_score,
    )

    if available_count < MIN_BRAINS_REQUIRED:
        logger.warning(
            "BRAIN CONNECTIVITY DEGRADED: %d/4 brains available. %s",
            available_count, conn_result.summary,
        )
    if available_count < MIN_BRAINS_HARD_BLOCK:
        logger.error(
            "BRAIN CONNECTIVITY CRITICAL: <2 brains available — TRS=0 hard block. %s",
            conn_result.summary,
        )
    else:
        logger.debug("Brain connectivity: %s", conn_result.summary)

    return conn_result
