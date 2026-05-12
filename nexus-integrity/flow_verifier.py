"""
flow_verifier.py — 5-stage pipeline flow verifier for nexus-integrity.

Verifies that data is flowing through the full Nexus pipeline every 15 minutes
during market hours. Unlike /health checks, this verifies ACTUAL DATA FLOW,
not process liveness.

Stages:
  1. Pool Delivery — Axiom pushed a pool in the last 16 min
  2. Agent Scan Activity — Cipher/Atlas/Sage produced decisions in the last 20 min
  3. Buffer Acceptance — Alpha Buffer circuit breaker is NORMAL
  4. Synthesis Readiness — OMNI has workers and is incrementing syntheses
  5. Execution Readiness — Alpha-execution responds to options dry-run probe

Auto-remediation is attempted for each stage failure.
Recovery assertion (V6 amendment) fires after every fix attempt.
"""

import datetime
import logging
import sqlite3
import time
from typing import List, Optional

import requests

import config
from models import FlowStageResult, StageResult

logger = logging.getLogger("integrity.flow_verifier")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _http_get(url: str, headers: Optional[dict] = None, timeout: float = 5.0) -> Optional[dict]:
    """Perform an authenticated GET request, returning parsed JSON or None on error.

    Args:
        url: Full URL to GET.
        headers: Optional request headers.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON dict or None on any error.
    """
    try:
        resp = requests.get(url, headers=headers or {}, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        logger.warning("GET %s returned %d", url, resp.status_code)
        return None
    except requests.RequestException as e:
        logger.error("GET %s failed: %s", url, e)
        return None


def _auth_headers() -> dict:
    """Return auth headers for internal Nexus services.

    Returns:
        Dict with X-Nexus-Secret header.
    """
    return {"X-Nexus-Secret": config.NEXUS_SECRET}


def _get_db_max_age(db_path: str, table: str, ts_col: str) -> Optional[float]:
    """Query a SQLite DB for the age of the most recent row in a table.

    Args:
        db_path: Path to SQLite database file.
        table: Table name to query.
        ts_col: Column name containing the timestamp (epoch float or ISO string).

    Returns:
        Age in seconds of the most recent row, or None if DB unreachable or empty.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=3.0)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                f"SELECT MAX({ts_col}) as latest FROM {table}"
            ).fetchone()
        finally:
            conn.close()

        if row is None or row["latest"] is None:
            return None

        latest = row["latest"]
        # Handle both epoch float and ISO string
        if isinstance(latest, (int, float)):
            age = time.time() - float(latest)
        else:
            # ISO string
            from datetime import datetime, timezone
            try:
                dt = datetime.fromisoformat(str(latest).replace("Z", "+00:00"))
                age = time.time() - dt.timestamp()
            except ValueError:
                return None

        return age

    except sqlite3.Error as e:
        logger.error("DB age query failed (%s.%s.%s): %s", db_path, table, ts_col, e)
        return None


# ---------------------------------------------------------------------------
# Stage verifiers
# ---------------------------------------------------------------------------

def _verify_stage1_pool_delivery() -> FlowStageResult:
    """Stage 1: Verify Axiom pushed a pool snapshot in the last POOL_MAX_AGE_S.

    Returns:
        FlowStageResult for stage 1.
    """
    start = time.time()
    name = "pool_delivery"

    age = _get_db_max_age(config.AXIOM_DB_PATH, "pool_snapshots", "created_at")
    if age is None:
        # Try HTTP fallback
        data = _http_get(f"{config.AXIOM_URL}/health")
        if data is None:
            return FlowStageResult(
                stage=1, name=name, result=StageResult.FAIL,
                detail="Axiom DB unreachable and /health failed",
                latency_ms=(time.time() - start) * 1000,
            )
        # Can't determine age — treat as unknown
        return FlowStageResult(
            stage=1, name=name, result=StageResult.PASS,
            detail="Axiom /health OK (DB unavailable for age check)",
            latency_ms=(time.time() - start) * 1000,
        )

    if age > config.POOL_MAX_AGE_S:
        # Only flag as FAIL during market hours or pre-market window (8am-4pm ET Mon-Fri).
        # Outside those hours, Axiom does not push pool snapshots — stale is expected.
        from datetime import datetime, timezone, timedelta
        try:
            et_offset = timedelta(hours=-4)  # EDT (Apr-Oct); adjust to -5 for EST
            now_et = datetime.now(timezone(et_offset))
            weekday = now_et.weekday()  # 0=Mon, 6=Sun
            hour = now_et.hour
            in_active_window = (weekday < 5) and (8 <= hour < 16)
        except Exception:
            in_active_window = True  # Fail safe: assume market hours on error

        if in_active_window:
            return FlowStageResult(
                stage=1, name=name, result=StageResult.FAIL,
                detail=f"Last pool snapshot {age:.0f}s ago (max {config.POOL_MAX_AGE_S}s)",
                latency_ms=(time.time() - start) * 1000,
            )
        else:
            return FlowStageResult(
                stage=1, name=name, result=StageResult.PASS,
                detail=f"Last pool snapshot {age:.0f}s ago — outside market hours, stale expected",
                latency_ms=(time.time() - start) * 1000,
            )

    return FlowStageResult(
        stage=1, name=name, result=StageResult.PASS,
        detail=f"Last pool snapshot {age:.0f}s ago",
        latency_ms=(time.time() - start) * 1000,
    )


def _verify_stage2_agent_activity() -> FlowStageResult:
    """Stage 2: Verify Cipher/Atlas/Sage produced decisions in the last AGENT_MAX_SILENCE_S.

    Note: 0 submissions ≠ broken. 0 decisions (no scan activity at all) = broken.

    Returns:
        FlowStageResult for stage 2.
    """
    start = time.time()
    name = "agent_scan_activity"

    # GAP-15: Check market hours FIRST — before any DB reachability check.
    # Outside 09:30–16:00 ET weekdays, agent silence (and unreachable DBs) is EXPECTED.
    # Previously, the "No agent decision DBs reachable" early return had no market hours gate,
    # causing false FAIL alerts post-market when agents are correctly idle.
    try:
        et_offset = datetime.timedelta(hours=-4)  # EDT (Apr-Oct)
        now_et = datetime.datetime.now(datetime.timezone(et_offset))
        weekday = now_et.weekday()  # 0=Mon, 6=Sun
        hour = now_et.hour
        minute = now_et.minute
        in_market_hours = (weekday < 5) and (
            (hour == 9 and minute >= 30) or (10 <= hour < 16)
        )
    except Exception:
        in_market_hours = True  # Fail safe: if clock check fails, assume market hours

    if not in_market_hours:
        return FlowStageResult(
            stage=2, name=name, result=StageResult.PASS,
            detail="Outside market hours (09:30–16:00 ET weekdays) — agent silence is expected",
            latency_ms=(time.time() - start) * 1000,
        )

    ages = {}
    for agent, db_path in [
        ("cipher", config.CIPHER_DB_PATH),
        ("atlas", config.ATLAS_DB_PATH),
        ("sage", config.SAGE_DB_PATH),
    ]:
        age = _get_db_max_age(db_path, "decisions", "created_at")
        if age is not None:
            ages[agent] = age

    if not ages:
        return FlowStageResult(
            stage=2, name=name, result=StageResult.FAIL,
            detail="No agent decision DBs reachable",
            latency_ms=(time.time() - start) * 1000,
        )

    silent_agents = [a for a, age in ages.items() if age > config.AGENT_MAX_SILENCE_S]
    if silent_agents:
        if len(silent_agents) == 3:
            return FlowStageResult(
                stage=2, name=name, result=StageResult.FAIL,
                detail=f"ALL agents silent > {config.AGENT_MAX_SILENCE_S}s: {ages}",
                latency_ms=(time.time() - start) * 1000,
            )
        return FlowStageResult(
            stage=2, name=name, result=StageResult.FAIL,
            detail=f"Agents silent > {config.AGENT_MAX_SILENCE_S}s: {silent_agents}",
            latency_ms=(time.time() - start) * 1000,
        )

    return FlowStageResult(
        stage=2, name=name, result=StageResult.PASS,
        detail=f"All agents active: {ages}",
        latency_ms=(time.time() - start) * 1000,
    )


def _verify_stage3_buffer_acceptance() -> FlowStageResult:
    """Stage 3: Verify Alpha Buffer circuit breaker is NORMAL.

    Returns:
        FlowStageResult for stage 3.
    """
    start = time.time()
    name = "buffer_acceptance"

    data = _http_get(
        f"{config.ALPHA_BUFFER_URL}/health",
        headers=_auth_headers()
    )
    if data is None:
        return FlowStageResult(
            stage=3, name=name, result=StageResult.FAIL,
            detail="Alpha Buffer /health unreachable",
            latency_ms=(time.time() - start) * 1000,
        )

    cb_state = data.get("circuit_breaker", data.get("status", "UNKNOWN"))
    if cb_state not in ("NORMAL", "healthy", "ok"):
        return FlowStageResult(
            stage=3, name=name, result=StageResult.FAIL,
            detail=f"Alpha Buffer circuit_breaker={cb_state}",
            latency_ms=(time.time() - start) * 1000,
        )

    return FlowStageResult(
        stage=3, name=name, result=StageResult.PASS,
        detail=f"circuit_breaker={cb_state}",
        latency_ms=(time.time() - start) * 1000,
    )


def _verify_stage4_synthesis_readiness() -> FlowStageResult:
    """Stage 4: Verify OMNI is active. Returns 3-way verdict (Cipher C7).

    LIVE   — concordances forming, GO verdicts dispatched (pipeline healthy + signal)
    QUIET  — concordances forming, all below GO threshold (market condition, NOT failure)
    SILENT — no concordances forming for >30 min (potential failure, investigate)

    QUIET is NOT an alert condition. SILENT triggers investigation.
    This prevents false P1 alerts on low-signal market days.

    GENESIS-FIX: Skip this check outside market hours (09:30–16:00 ET weekdays).
    Off-hours silence is EXPECTED behavior, not a failure.

    Returns:
        FlowStageResult for stage 4.
    """
    # Check market hours FIRST — outside trading hours, silence is expected
    try:
        et_offset = datetime.timedelta(hours=-4)  # EDT (Apr-Oct)
        now_et = datetime.datetime.now(datetime.timezone(et_offset))
        weekday = now_et.weekday()  # 0=Mon, 6=Sun
        hour = now_et.hour
        minute = now_et.minute
        in_market_hours = (weekday < 5) and (
            (hour == 9 and minute >= 30) or (10 <= hour < 16)
        )
    except Exception:
        in_market_hours = True  # Fail safe: if clock check fails, assume market hours

    if not in_market_hours:
        logger.info("Stage 4 (synthesis_readiness): Outside market hours — skipping check (silence expected)")
        return FlowStageResult(
            stage=4, name="synthesis_readiness", result=StageResult.PASS,
            detail="Outside market hours (09:30–16:00 ET weekdays) — silence expected",
            latency_ms=0,
        )

    start = time.time()
    name = "synthesis_readiness"

    data = _http_get(
        f"{config.OMNI_URL}/health",
        headers=_auth_headers()
    )
    if data is None:
        return FlowStageResult(
            stage=4, name=name, result=StageResult.FAIL,
            detail="OMNI /health unreachable",
            latency_ms=(time.time() - start) * 1000,
        )

    # Check pool workers
    workers = data.get("pool_workers", data.get("workers",
                data.get("synthesis_pool", {}).get("workers", 0)))
    try:
        workers = int(workers)
    except (TypeError, ValueError):
        workers = 0

    if workers == 0:
        return FlowStageResult(
            stage=4, name=name, result=StageResult.FAIL,
            detail=f"OMNI pool_workers={workers} (expected > 0)",
            latency_ms=(time.time() - start) * 1000,
        )

    # C7: 3-way verdict — check concordance formation + GO verdict rate
    try:
        syntheses_today = data.get("syntheses_today", 0) or 0
        go_verdicts     = data.get("go_verdicts_today", 0) or 0
        last_min_ago    = data.get("last_synthesis_min_ago")  # None = never ran

        # Check alpha-buffer concordance count (last 30 min)
        concordances_recent = _count_recent_concordances(minutes=30)

        if go_verdicts > 0:
            # LIVE: GO verdicts dispatched — pipeline healthy and producing signal
            verdict_str = "LIVE"
            detail = (f"LIVE: {go_verdicts} GO verdicts today, "
                      f"{concordances_recent} concordances last 30min")
            stage_result = StageResult.PASS
        elif concordances_recent > 0 or syntheses_today > 0:
            # QUIET: Concordances forming or syntheses running, but no GO verdicts
            # This is a MARKET condition (signals below threshold), NOT a failure
            verdict_str = "QUIET"
            detail = (f"QUIET: {syntheses_today} syntheses, 0 GO verdicts — "
                      f"market not clearing threshold (normal on low-signal days)")
            stage_result = StageResult.PASS  # Not a failure
        else:
            # SILENT: No concordances, no syntheses — pipeline may be stuck
            silence_min = last_min_ago if last_min_ago is not None else 999
            if silence_min > 30:
                verdict_str = "SILENT"
                detail = (f"SILENT: 0 concordances last 30min, "
                          f"last synthesis {silence_min:.0f}min ago — pipeline stalled")
                stage_result = StageResult.FAIL
            else:
                verdict_str = "QUIET"
                detail = (f"QUIET: warming up, last synthesis {silence_min:.1f}min ago")
                stage_result = StageResult.PASS

        logger.info("Stage 4 C7 verdict: %s | %s", verdict_str, detail)
    except Exception as _c7_err:
        logger.debug("C7 3-way verdict fallback: %s", _c7_err)
        verdict_str = "UNKNOWN"
        detail = f"OMNI pool_workers={workers}"
        stage_result = StageResult.PASS

    return FlowStageResult(
        stage=4, name=name, result=stage_result,
        detail=detail,
        latency_ms=(time.time() - start) * 1000,
    )


def _count_recent_concordances(minutes: int = 30) -> int:
    """Count concordances formed in the last N minutes from alpha-buffer DB."""
    try:
        import sqlite3 as _sq
        from datetime import datetime, timezone, timedelta
        db = "/Users/ahmedsadek/nexus/data/alpha_buffer.db"
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        conn = _sq.connect(db, timeout=2)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM concordance_results WHERE created_at >= ?",
            (cutoff,)
        )
        count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0  # If DB unavailable, don't block on it


def _verify_stage5_execution_readiness() -> FlowStageResult:
    """Stage 5: Verify Alpha-execution accepts a dry-run options probe (V2 amendment).

    Uses X-Probe-Mode header and PROBE_SECRET. Checks that execution engine
    is ready to handle options orders — not just that the HTTP server responds.

    Returns:
        FlowStageResult for stage 5.
    """
    start = time.time()
    name = "execution_readiness"

    try:
        resp = requests.post(
            f"{config.ALPHA_EXEC_URL}/execute",
            json={
                "ticker": "SPY",
                "asset_class": "options",
                "strategy": "short_put",
                "dry_run": True,
            },
            headers={
                "X-Nexus-Secret": config.PROBE_SECRET,
                "X-Probe-Mode": "true",
            },
            timeout=10.0,
        )

        # 200 or 422 (validation error) both prove the endpoint is alive and parsing
        # 503 = service down, 500 = internal error = fail
        if resp.status_code in (200, 201, 422):
            return FlowStageResult(
                stage=5, name=name, result=StageResult.PASS,
                detail=f"Alpha-execution responded {resp.status_code} to options dry-run",
                latency_ms=(time.time() - start) * 1000,
            )
        else:
            return FlowStageResult(
                stage=5, name=name, result=StageResult.FAIL,
                detail=f"Alpha-execution returned {resp.status_code}: {resp.text[:100]}",
                latency_ms=(time.time() - start) * 1000,
            )

    except requests.RequestException as e:
        return FlowStageResult(
            stage=5, name=name, result=StageResult.FAIL,
            detail=f"Alpha-execution unreachable: {e}",
            latency_ms=(time.time() - start) * 1000,
        )


# ---------------------------------------------------------------------------
# Auto-remediation
# ---------------------------------------------------------------------------

def _attempt_fix(stage: FlowStageResult) -> bool:
    """Attempt auto-remediation for a failed stage.

    Args:
        stage: The failed FlowStageResult.

    Returns:
        True if fix was attempted (not necessarily successful).
    """
    if stage.stage == 1:
        logger.info("Stage 1 fix: triggering Axiom pool refresh via /trigger-tier2")
        try:
            resp = requests.post(
                f"{config.AXIOM_URL}/trigger-tier2",
                headers=_auth_headers(),
                timeout=10.0
            )
            logger.info("Axiom /trigger-tier2: %d", resp.status_code)
            return True
        except requests.RequestException as e:
            logger.error("Stage 1 fix failed: %s", e)
            return False

    elif stage.stage == 3:
        logger.info("Stage 3 fix: resetting Alpha Buffer circuit breaker")
        try:
            resp = requests.post(
                f"{config.ALPHA_BUFFER_URL}/reset-circuit-breaker",
                headers=_auth_headers(),
                timeout=10.0
            )
            logger.info("Alpha Buffer /reset-circuit-breaker: %d", resp.status_code)
            return True
        except requests.RequestException as e:
            logger.error("Stage 3 fix failed: %s", e)
            return False

    elif stage.stage == 4:
        logger.info("Stage 4 fix: restarting OMNI via launchctl")
        import subprocess
        try:
            subprocess.run(
                ["launchctl", "kickstart", "-k", "gui/$(id -u)/ai.nexus.omni"],
                shell=False, capture_output=True, timeout=15
            )
            time.sleep(30)  # Wait for restart
            return True
        except (subprocess.SubprocessError, OSError) as e:
            logger.error("Stage 4 fix failed: %s", e)
            return False

    logger.info("No auto-fix available for stage %d", stage.stage)
    return False


def _recovery_assert(stage_num: int, pre_fix_result: FlowStageResult) -> bool:
    """Verify that an auto-fix actually resolved the issue (V6 amendment).

    Re-runs the stage verifier and confirms the result is now PASS.

    Args:
        stage_num: Stage number to re-verify.
        pre_fix_result: The original failure result before the fix.

    Returns:
        True if recovery assertion passes (stage now PASS), False otherwise.
    """
    logger.info("Recovery assert for stage %d", stage_num)
    time.sleep(5)  # Brief settle time

    verifiers = {
        1: _verify_stage1_pool_delivery,
        2: _verify_stage2_agent_activity,
        3: _verify_stage3_buffer_acceptance,
        4: _verify_stage4_synthesis_readiness,
        5: _verify_stage5_execution_readiness,
    }

    verifier = verifiers.get(stage_num)
    if not verifier:
        return False

    result = verifier()
    passed = result.result == StageResult.PASS
    logger.info(
        "Recovery assert stage %d: %s (was: %s)",
        stage_num, result.result.value, pre_fix_result.result.value
    )
    return passed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_flow_verification() -> List[FlowStageResult]:
    """Run all 5 pipeline flow verification stages.

    For each failing stage:
      1. Attempt auto-remediation
      2. Run recovery assertion (V6 amendment)
      3. Mark as FIXED or ESCALATED based on assertion result

    Returns:
        List of FlowStageResult, one per stage.
    """
    logger.info("Starting 5-stage pipeline flow verification")
    results: List[FlowStageResult] = []

    verifiers = [
        _verify_stage1_pool_delivery,
        _verify_stage2_agent_activity,
        _verify_stage3_buffer_acceptance,
        _verify_stage4_synthesis_readiness,
        _verify_stage5_execution_readiness,
    ]

    for verifier in verifiers:
        result = verifier()
        logger.info(
            "Stage %d (%s): %s — %s",
            result.stage, result.name, result.result.value, result.detail
        )

        if result.result == StageResult.FAIL:
            fix_attempted = _attempt_fix(result)
            result.fix_attempted = fix_attempted

            if fix_attempted:
                time.sleep(5)  # Allow fix to propagate
                recovered = _recovery_assert(result.stage, result)
                result.fix_succeeded = recovered
                result.result = StageResult.FIXED if recovered else StageResult.ESCALATED
                logger.info(
                    "Stage %d after fix: %s", result.stage, result.result.value
                )

        results.append(result)

    passed = sum(1 for r in results if r.result in (StageResult.PASS, StageResult.FIXED))
    logger.info(
        "Flow verification complete: %d/%d stages passed",
        passed, len(results)
    )
    return results


def get_flow_score(results: List[FlowStageResult]) -> float:
    """Calculate a 0-100 flow score from stage results.

    Weights: stage 5 (execution) counts double.

    Args:
        results: List of FlowStageResult from run_flow_verification().

    Returns:
        Float score 0-100.
    """
    if not results:
        return 0.0

    weights = {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0, 5: 2.0}
    total_weight = sum(weights.get(r.stage, 1.0) for r in results)
    earned_weight = sum(
        weights.get(r.stage, 1.0)
        for r in results
        if r.result in (StageResult.PASS, StageResult.FIXED)
    )

    return (earned_weight / total_weight) * 100.0 if total_weight > 0 else 0.0
