"""
quad_intelligence.py — OMNI Quad Intelligence Engine

Runs 4 AI brains in parallel. Each brain receives identical complete context.
Each brain leads on one dimension. All 4 must respond to achieve STRONG_GO.

Brain assignments:
  Claude    — PRIMARY synthesis. Evaluates overall trade coherence.
  o3-mini   — ADVERSARIAL. Attacks the thesis. Finds edge cases and failure modes.
  Gemini    — PATTERN. Pattern recognition across price action, sector, macro context.
  DeepSeek  — MOMENTUM. Fast market structure and near-term momentum analysis.

If a brain fails (timeout/API error), it contributes an error result — not a NO_GO vote.
With ≥ 3 brains responding, synthesis proceeds. Fewer → CONDITIONAL automatically.
"""

import json
import logging
import time
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from datetime import datetime, timezone
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    ALL_BRAINS,
    BRAIN_CLAUDE,
    BRAIN_DEEPSEEK,
    BRAIN_GEMINI,
    BRAIN_O3MINI,
    BRAIN_DIMENSIONS,
    BRAIN_TIMEOUT_SECONDS,
    BRAIN_RETRY_COUNT,
)

# GENESIS FIX 2026-06-02: Socket-level timeout adapter to prevent hung connections
# Prevents brain API calls from blocking indefinitely on stalled TCP connections
class TimeoutHTTPAdapter(HTTPAdapter):
    """HTTP adapter with socket-level timeout."""
    def __init__(self, timeout, *args, **kwargs):
        self.timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        kwargs['timeout'] = self.timeout
        return super().send(request, **kwargs)


def _create_timeout_session(timeout_sec: int = BRAIN_TIMEOUT_SECONDS):
    """Create a requests.Session with socket timeout on both connect and read."""
    sess = requests.Session()
    adapter = TimeoutHTTPAdapter(timeout=(5, timeout_sec))  # connect_timeout, read_timeout
    sess.mount('http://', adapter)
    sess.mount('https://', adapter)
    return sess

# ── Brain Health Cache ────────────────────────────────────────────────────────
# Cached health status for each brain. Reset on new synthesis cycle.
# Key: brain_name, Value: dict with 'healthy': bool, 'checked_at': float
_brain_health_cache: dict[str, dict] = {}
_BRAIN_HEALTH_CACHE_TTL = 120  # seconds — recheck every 2 min during market hours

_BRAIN_PING_TIMEOUT = 15.0  # seconds for health ping. Gemini 2.5-flash under load: ~10s. Increased 2026-06-02 post-PRIMUS alert.

# GPT-4o is the fallback when Gemini is unresponsive.
# It takes the Gemini PATTERN role when Gemini fails the health check.
_BRAIN_GEMINI_FALLBACK = "gpt-4o-fallback"

logger = logging.getLogger("omni.quad_intelligence")

# ── Brain System Prompts ──────────────────────────────────────────────────────

_BASE_PROMPT = """You are analyzing a multi-agent concordance signal for a live trading system.
The system targets: Win rate ≥75%, avg win ≥40%, avg loss ≤20%.
Respond ONLY with valid JSON. No markdown, no preamble.

CONCORDANCE CONTEXT:
{context_json}

YOUR ROLE: {role_description}

Respond in this EXACT JSON format:
{{
  "vote": "GO" or "NO_GO",
  "confidence": <integer 0-100>,
  "concern_1": "<primary concern ≤100 chars, or 'None'>",
  "concern_2": "<secondary concern ≤100 chars, or 'None'>",
  "echo_chamber": <true or false>,
  "reasoning": "<2-3 sentences explaining your vote>"
}}"""

BRAIN_ROLES = {
    BRAIN_CLAUDE:   "You are the PRIMARY synthesis brain. Evaluate the overall quality, "
                    "coherence and conviction of this concordance signal. Does the collective "
                    "agent agreement represent a genuine high-probability opportunity? Flag if "
                    "agents appear to be citing the same source (echo chamber).",
    BRAIN_O3MINI:   "You are the ADVERSARIAL brain. Your job is rigorous stress-testing — "
                    "not reflexive rejection. Find real edge cases, hidden risks, timing problems, "
                    "and failure modes specific to THIS setup. Be skeptical of weak signals. "
                    "BUT: if the risk/reward is genuinely favorable and risks are manageable, "
                    "vote GO — adversarial does not mean always NO_GO. "
                    "CRITICAL: Do NOT base your vote on the system's historical win rate or "
                    "aggregate past performance — those stats reflect a nascent system with "
                    "fewer than 30 trades and are not valid signals for individual trade quality. "
                    "Judge only: (1) this ticker's setup quality, (2) specific risks for this "
                    "exact entry, (3) whether the concordance reasoning is sound or echo-chamber. "
                    "Also check if agent reasoning shows signs of echo chamber (shared source).",
    BRAIN_GEMINI:   "You are the PATTERN recognition brain. Analyze this signal in the context "
                    "of market regime, sector dynamics, and whether this setup matches historically "
                    "high-probability patterns. Does the technical and macro context support this "
                    "entry at this moment?",
    BRAIN_DEEPSEEK: "You are the MOMENTUM analysis brain. Focus on near-term market structure: "
                    "is momentum aligned with this direction? Is this an early entry or is the "
                    "move already extended? What does the VIX regime and current session context "
                    "say about timing?",
}


def _ping_gemini(api_key: str) -> bool:
    """
    Pre-flight health check for Gemini before committing to full synthesis call.
    Sends a minimal 1-token prompt with a 3-second hard timeout.
    Returns True if Gemini is responsive, False otherwise.

    FIX-GEMINI-HEALTH: Added 2026-05-04 after Gemini went silent all afternoon
    without any alert. This detects the failure before synthesis starts so we can
    route to GPT-4o immediately instead of waiting for a 15s timeout per call.
    """
    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": "Reply OK"}]}],
                "generationConfig": {
                    "maxOutputTokens": 4,
                    "temperature": 0.0,
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            },
            timeout=_BRAIN_PING_TIMEOUT,
        )
        return resp.status_code == 200
    except Exception as e:
        logger.warning("Gemini health ping failed: %s", e)
        return False


def check_brain_health(
    brain_name: str,
    api_key: str,
    bot_token: str = "",
    ahmed_chat_id: str = "",
) -> bool:
    """
    Check if a brain is healthy, using a short-lived cache to avoid pre-pinging
    on every single synthesis call.

    Currently only implemented for Gemini (the brain that failed silently on May 4).
    Other brains (Claude, DeepSeek, o3-mini) are assumed healthy and rely on the
    existing per-call retry + error result mechanism.

    If Gemini fails the ping, sends a Telegram alert immediately (within 2 min of failure).

    Returns True if the brain is (or is assumed) healthy.
    """
    import threading as _threading
    now = time.time()

    # Only health-check Gemini for now
    if brain_name != BRAIN_GEMINI:
        return True

    cached = _brain_health_cache.get(brain_name)
    if cached and (now - cached["checked_at"]) < _BRAIN_HEALTH_CACHE_TTL:
        return cached["healthy"]

    healthy = _ping_gemini(api_key)
    _brain_health_cache[brain_name] = {"healthy": healthy, "checked_at": now}

    if not healthy:
        logger.critical(
            "BRAIN HEALTH FAIL: %s is unresponsive (ping timeout/error). "
            "Routing to GPT-4o fallback for PATTERN analysis.",
            brain_name,
        )
        # Alert Ahmed immediately — fire-and-forget in background thread
        if bot_token and ahmed_chat_id:
            def _alert():
                try:
                    import requests as _r
                    _r.post(
                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                        json={
                            "chat_id": ahmed_chat_id,
                            "text": (
                                "\u26a0\ufe0f OMNI BRAIN ALERT\n"
                                f"Gemini is unresponsive (health ping failed).\n"
                                "Routing to GPT-4o for PATTERN analysis.\n"
                                "Synthesis will continue with 4 brains (Claude + o3-mini + GPT-4o + DeepSeek)."
                            ),
                        },
                        timeout=5,
                    )
                except Exception:
                    pass
            _threading.Thread(target=_alert, daemon=True).start()

    return healthy


def run_all_brains(
    context: dict,
    concordance: Optional[dict] = None,      # FIX-MEMORY-PHASE2: For brain-specific contexts
    axiom_result: Optional[dict] = None,     # FIX-MEMORY-PHASE2
    regime: Optional[dict] = None,           # FIX-MEMORY-PHASE2
    oracle_ctx: Optional[dict] = None,       # FIX-MEMORY-PHASE2
    anthropic_api_key: str = "",
    openai_api_key:    str = "",
    gemini_api_key:    str = "",
    deepseek_api_key:  str = "",
    bot_token:         str = "",
    ahmed_chat_id:     str = "",
) -> dict[str, dict]:
    """
    Run all 4 brains in parallel and collect their votes.

    FIX-GEMINI-HEALTH (2026-05-04): Pre-flight Gemini health check before spawning
    brain threads. If Gemini is unresponsive, GPT-4o takes the PATTERN role.
    This prevents a 15s timeout cascade per synthesis call when Gemini is down.

    Args:
        context:           Full concordance + regime + Axiom context dict.
        anthropic_api_key: Anthropic API key for Claude.
        openai_api_key:    OpenAI API key for o3-mini.
        gemini_api_key:    Google Gemini API key.
        deepseek_api_key:  DeepSeek API key.
        bot_token:         Telegram bot token for brain-down alerts.
        ahmed_chat_id:     Ahmed's Telegram chat ID for alerts.

    Returns:
        Dict mapping brain name → brain result dict.
        Every brain has an entry — errors are recorded as error results, not omitted.
    """
    # FIX-GEMINI-HEALTH: Pre-flight check — if Gemini is down, swap in GPT-4o
    # This prevents 15s timeout per synthesis call when Gemini is unresponsive.
    gemini_healthy = check_brain_health(
        BRAIN_GEMINI, gemini_api_key,
        bot_token=bot_token, ahmed_chat_id=ahmed_chat_id,
    )
    # Use GPT-4o as the PATTERN brain if Gemini is down.
    # GPT-4o uses the same OpenAI endpoint/key as o3-mini.
    if not gemini_healthy:
        logger.warning(
            "Gemini unhealthy — routing PATTERN role to GPT-4o for this synthesis cycle."
        )

    api_keys = {
        BRAIN_CLAUDE:   anthropic_api_key,
        BRAIN_O3MINI:   openai_api_key,
        BRAIN_GEMINI:   gemini_api_key if gemini_healthy else openai_api_key,  # GPT-4o fallback
        BRAIN_DEEPSEEK: deepseek_api_key,
    }

    # FIX-MEMORY-PHASE2: Build brain-specific contexts to reduce oracle field duplication
    # Each non-pattern brain excludes oracle (saves 2.5KB × 3 brains = 7.5KB per verdict)
    from synthesis import build_context_for_brain  # Import new Phase 2 function
    
    brain_contexts_json = {}
    if concordance is not None and axiom_result is not None and regime is not None:
        # Use new brain-specific contexts (Phase 2)
        for brain_name in ALL_BRAINS:
            brain_ctx = build_context_for_brain(
                brain_name=brain_name,
                concordance=concordance,
                axiom_result=axiom_result,
                regime=regime,
                oracle_ctx=oracle_ctx,
            )
            brain_contexts_json[brain_name] = json.dumps(brain_ctx, default=str)
    else:
        # Fallback: use original context for all brains (if components not provided)
        context_json = json.dumps(context, default=str)  # FIX-MEMORY-PHASE1: minified
        brain_contexts_json = {brain: context_json for brain in ALL_BRAINS}

    results: dict[str, dict] = {}
    # GAP-BRAIN-HANG: Hard deadline for all brain calls combined.
    # BRAIN_TIMEOUT_SECONDS covers per-request network timeout, but a hung TCP
    # connection (established, server never responds) can block indefinitely
    # without raising — holding the synthesis pool worker and semaphore.
    # as_completed(timeout=HARD_DEADLINE) raises concurrent.futures.TimeoutError
    # if not all futures complete in time, so unfinished brains get error results
    # and synthesis continues immediately.
    #
    # CRITICAL: Do NOT use ThreadPoolExecutor as a context manager here.
    # The context manager calls shutdown(wait=True) on exit, which blocks waiting
    # for all threads — defeating the deadline entirely if brains are stuck in
    # requests.post(). Instead, call shutdown(wait=False): hung threads eventually
    # exit on their own via the per-request timeout (BRAIN_TIMEOUT_SECONDS).
    # FIX-CLAUDE-TIMEOUT (2026-06-01): Updated grace period from 30s to 15s (tighter deadline).
    # With 45s per-brain timeout, 4 brains in parallel complete in ~45s max.
    # Hard deadline of 60s allows for overhead + thread pool scheduling.
    _HARD_DEADLINE = min(BRAIN_TIMEOUT_SECONDS * 4 // 3, 60)  # 60s hard stop (was 150s)

    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="omni-brain")
    futures = {
        executor.submit(
            _call_brain,
            brain_name,
            brain_contexts_json[brain_name],  # FIX-MEMORY-PHASE2: Use brain-specific context
            api_keys[brain_name],
        ): brain_name
        for brain_name in ALL_BRAINS
    }

    completed_brains: set = set()
    try:
        for future in as_completed(futures, timeout=_HARD_DEADLINE):
            brain_name = futures[future]
            try:
                result = future.result()  # future already done — no extra timeout needed
            except Exception as e:
                logger.warning("Brain %s raised exception: %s", brain_name, e)
                result = _error_result(brain_name, str(e)[:200])

            results[brain_name] = result
            completed_brains.add(brain_name)
            if "error" not in result:
                logger.info(
                    "Brain %s voted %s (confidence=%s)",
                    brain_name.upper(),
                    result.get("vote", "UNKNOWN"),
                    result.get("confidence", "?"),
                )
    except TimeoutError:
        # Some brains did not complete within the hard deadline.
        # Assign error results to any that didn't finish — synthesis continues
        # with whatever brains responded.
        hung_brains = [name for name in ALL_BRAINS if name not in completed_brains]
        logger.critical(
            "run_all_brains HARD DEADLINE hit (%ds) — brains still pending: %s",
            _HARD_DEADLINE, hung_brains,
        )
        for brain_name in hung_brains:
            results[brain_name] = _error_result(
                brain_name, f"hard deadline exceeded after {_HARD_DEADLINE}s"
            )
    finally:
        # Don't wait for hung threads — they will exit on their own when the
        # per-request timeout fires. Blocking here re-introduces the hang.
        executor.shutdown(wait=False)

    return results


def _call_brain(
    brain_name:  str,
    context_json: str,
    api_key:     str,
) -> dict:
    """
    Call a single brain's API and parse the response.

    Args:
        brain_name:   Brain identifier (BRAIN_CLAUDE, etc.).
        context_json: JSON string of full context.
        api_key:      API key for this brain's service.

    Returns:
        Brain result dict with vote, confidence, concerns, reasoning.
        Returns error result dict on any failure.
    """
    prompt = _BASE_PROMPT.format(
        context_json     = context_json,
        role_description = BRAIN_ROLES[brain_name],
    )

    start_ms  = int(time.time() * 1000)
    last_error: Exception | None = None

    for attempt in range(1 + BRAIN_RETRY_COUNT):
        if attempt > 0:
            logger.info("Brain %s retry %d/%d", brain_name, attempt, BRAIN_RETRY_COUNT)
        try:
            raw_text = _dispatch_api(brain_name, prompt, api_key)
            elapsed  = int(time.time() * 1000) - start_ms

            # Guard: empty response means the brain returned nothing (e.g. Gemini
            # thinking model exhausted tokens and returned empty body, or Claude
            # connection reset). Retry once before treating as error.
            if not raw_text or not raw_text.strip():
                logger.warning("Brain %s returned empty response (%dms) — attempt %d",
                               brain_name, elapsed, attempt + 1)
                last_error = Exception(f"empty response from API after {elapsed}ms")
                continue  # retry

            result = _parse_brain_response(raw_text, brain_name)
            result["elapsed_ms"]  = elapsed
            result["brain"]       = brain_name
            result["dimension"]   = BRAIN_DIMENSIONS[brain_name]
            return result

        except Exception as e:
            elapsed = int(time.time() * 1000) - start_ms
            logger.warning("Brain %s call failed (%dms) attempt %d: %s",
                           brain_name, elapsed, attempt + 1, e)
            last_error = e
            # Don't retry on non-timeout errors (4xx, 5xx auth/quota) — only on
            # connection-level failures where a retry has a real chance of success.
            retryable = any(kw in str(e).lower() for kw in (
                "timeout", "connection reset", "connection aborted", "read timed out",
                "service unavailable", "502", "503", "504",
            ))
            if not retryable:
                break

    elapsed = int(time.time() * 1000) - start_ms
    err = _error_result(brain_name, str(last_error)[:200])
    err["elapsed_ms"] = elapsed
    return err


def _dispatch_api(brain_name: str, prompt: str, api_key: str) -> str:
    """
    Send prompt to the appropriate API and return raw text response.

    Args:
        brain_name: Brain identifier.
        prompt:     Complete prompt string.
        api_key:    API key.

    Returns:
        Raw response text from the API.

    Raises:
        Exception: On API error, timeout, or non-2xx response.
    """
    if brain_name == BRAIN_CLAUDE:
        return _call_anthropic(prompt, api_key)
    elif brain_name == BRAIN_O3MINI:
        return _call_openai(prompt, api_key)
    elif brain_name == BRAIN_GEMINI:
        # FIX-GEMINI-HEALTH: If Gemini is down, run_all_brains passes the OpenAI key
        # for the Gemini slot. Detect this and route to GPT-4o instead.
        if api_key and api_key.startswith("sk-"):
            logger.info(
                "BRAIN GEMINI: routing to GPT-4o (OpenAI key detected — Gemini fallback active)"
            )
            return _call_gpt4o_pattern(prompt, api_key)
        return _call_gemini(prompt, api_key)
    elif brain_name == BRAIN_DEEPSEEK:
        return _call_deepseek(prompt, api_key)
    else:
        raise ValueError(f"Unknown brain: {brain_name}")


def _call_anthropic(prompt: str, api_key: str) -> str:
    """Call Claude claude-sonnet-4-6 via Anthropic Messages API.
    
    GENESIS FIX 2026-06-02: Use timeout session with socket-level timeout
    to prevent hung connections from blocking indefinitely.
    """
    sess = _create_timeout_session(BRAIN_TIMEOUT_SECONDS)
    try:
        resp = sess.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 1000,
                "messages":   [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]
    finally:
        sess.close()


def _call_openai(prompt: str, api_key: str) -> str:
    """Call o3-mini via OpenAI Chat Completions API.
    
    GENESIS FIX 2026-06-02: Use timeout session with socket-level timeout.
    """
    sess = _create_timeout_session(BRAIN_TIMEOUT_SECONDS)
    try:
        resp = sess.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "model":                 "o3-mini",
                "messages":              [{"role": "user", "content": prompt}],
                "max_completion_tokens": 2000,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    finally:
        sess.close()


def _call_gemini(prompt: str, api_key: str) -> str:
    """Call Gemini 2.5 Flash (thinking disabled) via Google Generative AI REST API.

    Switched from 2.5 Pro to 2.5 Flash (thinkingBudget=0) to eliminate the
    25-90s thinking phase that was causing consistent brain timeouts and
    degrading quad-intelligence to 2/4 brains. Flash with thinking disabled
    produces sub-5s responses and is fully sufficient for the structured
    JSON vote this brain delivers (500-char output, deterministic format).
    Cipher Finding 9 (Pro required for reasoning depth) does not apply to
    this structured voting use case — it applies to open-ended synthesis.
    
    GENESIS FIX 2026-06-02: Use timeout session with socket-level timeout.
    """
    sess = _create_timeout_session(BRAIN_TIMEOUT_SECONDS)
    try:
        resp = sess.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "maxOutputTokens": 4096,
                    "temperature": 0.2,
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            },
        )
        resp.raise_for_status()
        # Parse response defensively — Gemini 2.5 Flash (thinking model) may include
        # thought blocks before the text response. Filter to actual text parts only.
        # Handle missing/empty/malformed content gracefully.
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            logger.warning("Gemini returned no candidates | Response: %s", resp.text[:200])
            return ""
        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts:
            logger.warning("Gemini returned no parts in content | Candidate: %s", candidates[0])
            return ""
        # Skip thought-only parts; prefer non-thought text
        text_parts = [
            p.get("text", "")
            for p in parts
            if isinstance(p, dict) and "text" in p and not p.get("thought", False)
        ]
        if not text_parts:
            # Fall back: accept any text part including thought blocks
            text_parts = [p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p]
        return text_parts[0] if text_parts else ""
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        logger.warning("Gemini response parsing error: %s | Response: %s", e, resp.text[:300])
        return ""
    except Exception as e:
        logger.warning("Gemini API error: %s", e)
        raise
    finally:
        sess.close()


def _call_gpt4o_pattern(prompt: str, api_key: str) -> str:
    """Call GPT-4o as a drop-in replacement for Gemini's PATTERN brain role.

    FIX-GEMINI-HEALTH (2026-05-04): Used when Gemini is unresponsive.
    GPT-4o is fully capable of the structured JSON vote format.
    Uses gpt-4o (not o3-mini) to keep the adversarial brain (o3-mini) distinct.
    
    GENESIS FIX 2026-06-02: Use timeout session with socket-level timeout.
    """
    sess = _create_timeout_session(BRAIN_TIMEOUT_SECONDS)
    try:
        resp = sess.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "model":      "gpt-4o",
                "messages":   [{"role": "user", "content": prompt}],
                "max_tokens": 1000,
                "temperature": 0.2,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    finally:
        sess.close()


def _call_deepseek(prompt: str, api_key: str) -> str:
    """Call DeepSeek V3 via DeepSeek Chat Completions API (OpenAI-compatible).
    
    GENESIS FIX 2026-06-02: Use timeout session with socket-level timeout.
    """
    sess = _create_timeout_session(BRAIN_TIMEOUT_SECONDS)
    try:
        resp = sess.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       "deepseek-chat",
                "messages":    [{"role": "user", "content": prompt}],
                "max_tokens":  1000,
                "temperature": 0.2,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    finally:
        sess.close()


def _parse_brain_response(raw_text: str, brain_name: str) -> dict:
    """
    Parse a brain's raw text response into a structured dict.

    Strips markdown fences if present. Returns error result on parse failure.

    Args:
        raw_text:   Raw text response from the brain API.
        brain_name: Brain name for error logging.

    Returns:
        Parsed brain result dict.
    """
    # Strip markdown code fences
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text  = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    text = text.strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("Brain %s returned unparseable JSON: %s | Raw: %s", brain_name, e, text[:200])
        return _error_result(brain_name, f"JSON parse error: {e}")

    # Validate and normalize vote
    vote = str(parsed.get("vote", "")).upper().strip()
    if vote not in ("GO", "NO_GO"):
        # CONDITIONAL or any invalid vote is treated as NO_GO (Ahmed directive 2026-05-07: no CONDITIONAL)
        logger.warning("Brain %s returned invalid/CONDITIONAL vote '%s' — treating as NO_GO", brain_name, vote)
        vote = "NO_GO"

    return {
        "vote":          vote,
        "confidence":    min(100, max(0, int(parsed.get("confidence", 50)))),
        "concern_1":     str(parsed.get("concern_1", "None"))[:120],
        "concern_2":     str(parsed.get("concern_2", "None"))[:120],
        "echo_chamber":  bool(parsed.get("echo_chamber", False)),
        "reasoning":     str(parsed.get("reasoning", ""))[:500],
        "brain":         brain_name,
        "dimension":     BRAIN_DIMENSIONS[brain_name],
    }


def _error_result(brain_name: str, error_msg: str) -> dict:
    """
    Create a standardized error result for a brain that failed.

    Error results do NOT count as votes — they count toward brains_responded = False.

    Args:
        brain_name: Brain name.
        error_msg:  Error description.

    Returns:
        Error result dict. 'error' key present signals failure to caller.
    """
    return {
        "vote":        None,
        "confidence":  None,
        "concern_1":   None,
        "concern_2":   None,
        "echo_chamber": False,
        "reasoning":   None,
        "brain":       brain_name,
        "dimension":   BRAIN_DIMENSIONS.get(brain_name, "unknown"),
        "error":       error_msg,
    }
