"""
diagnoser.py — Diagnosis decision tree for zero-trade cycles.

Priority order (first match wins):
  1. auto_execute_disabled  — deliberate config, non-escalating
  2. scanner_dry            — no scans; escalates at WARN after 3+ consecutive cycles
  3. concordance_failing    — submissions flow but zero GO verdicts (CRITICAL)
  4. capital_locked         — buying power below floor, non-escalating
  5. omni_not_synthesizing  — submissions flow but OMNI silent (CRITICAL)
  6. unknown                — all checks pass but still zero trades (CRITICAL)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from config import CAPITAL_FLOOR, CONSECUTIVE_DRY_ESCALATE, SCANNER_STALE_MINUTES
from models import AlpacaSnapshot, DiagnosisResult, ServiceSnapshot

logger = logging.getLogger(__name__)


def _parse_scan_ts(ts_str: Optional[str]) -> Optional[datetime]:
    """
    Parse an ISO-8601 timestamp string into a timezone-aware datetime.

    Args:
        ts_str: ISO-8601 string (with or without timezone suffix).

    Returns:
        UTC-aware datetime, or None if missing or unparseable.
    """
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        logger.debug("Could not parse scan timestamp: %r", ts_str)
        return None


def _is_scanner_stale(snap: ServiceSnapshot) -> bool:
    """
    Return True if the scanner is DOWN or its last_scan_at is older than
    SCANNER_STALE_MINUTES minutes.

    Args:
        snap: ServiceSnapshot for one of the agent scanning services.

    Returns:
        True if stale or unreachable, False if recently scanned.
    """
    if snap.status == "DOWN":
        return True
    ts = _parse_scan_ts(snap.data.get("last_scan_at"))
    if ts is None:
        return True
    return (datetime.now(timezone.utc) - ts) > timedelta(minutes=SCANNER_STALE_MINUTES)


def diagnose(
    services: Dict[str, ServiceSnapshot],
    alpaca: Dict[str, AlpacaSnapshot],
    trades_since_last_cycle: int,
    consecutive_dry_cycles: int,
) -> DiagnosisResult:
    """
    Run the outcome diagnosis decision tree.

    Args:
        services:               Dict of service name → ServiceSnapshot.
        alpaca:                 Dict of "v1"/"v2" → AlpacaSnapshot.
        trades_since_last_cycle: Number of new trades since previous cycle.
        consecutive_dry_cycles: How many consecutive cycles have had zero trades.

    Returns:
        DiagnosisResult with diagnosis label, severity, escalation flag, and details.
    """
    # ── Happy path ────────────────────────────────────────────────────────────
    if trades_since_last_cycle > 0:
        return DiagnosisResult(
            diagnosis="pipeline_healthy",
            severity="INFO",
            escalate=False,
            details=f"{trades_since_last_cycle} trade(s) executed since last cycle.",
            consecutive_dry_cycles=0,
        )

    # ── Check 1: AUTO_EXECUTE disabled ────────────────────────────────────────
    alpha_exec = services.get("alpha_execution")
    prime_exec = services.get("prime_execution")
    alpha_auto = (
        alpha_exec.data.get("auto_execute", True)
        if alpha_exec and alpha_exec.status == "UP"
        else True
    )
    prime_auto = (
        prime_exec.data.get("auto_execute", True)
        if prime_exec and prime_exec.status == "UP"
        else True
    )
    if not alpha_auto or not prime_auto:
        disabled = []
        if not alpha_auto:
            disabled.append("alpha_execution")
        if not prime_auto:
            disabled.append("prime_execution")
        return DiagnosisResult(
            diagnosis="auto_execute_disabled",
            severity="INFO",
            escalate=False,
            details=(
                f"auto_execute=False on: {', '.join(disabled)}. "
                "Deliberate configuration — no pipeline fault."
            ),
            consecutive_dry_cycles=consecutive_dry_cycles,
        )

    # ── Check 2: Scanner dry ──────────────────────────────────────────────────
    cipher = services.get("cipher", ServiceSnapshot("cipher", "DOWN"))
    atlas = services.get("atlas", ServiceSnapshot("atlas", "DOWN"))
    sage = services.get("sage", ServiceSnapshot("sage", "DOWN"))
    all_stale = (
        _is_scanner_stale(cipher)
        and _is_scanner_stale(atlas)
        and _is_scanner_stale(sage)
    )
    if all_stale:
        escalate = consecutive_dry_cycles >= CONSECUTIVE_DRY_ESCALATE
        return DiagnosisResult(
            diagnosis="scanner_dry",
            severity="WARN" if escalate else "INFO",
            escalate=escalate,
            details=(
                f"All scanners stale or DOWN. "
                f"Consecutive dry cycles: {consecutive_dry_cycles}. "
                + (
                    f"Stall threshold ({CONSECUTIVE_DRY_ESCALATE} cycles) reached — escalating."
                    if escalate
                    else "Within normal market-slow tolerance."
                )
            ),
            consecutive_dry_cycles=consecutive_dry_cycles,
        )

    # ── Check 3: Concordance failing ──────────────────────────────────────────
    alpha_buf = services.get("alpha_buffer")
    prime_buf = services.get("prime_buffer")
    alpha_subs = (
        alpha_buf.data.get("submissions_today", 0)
        if alpha_buf and alpha_buf.status == "UP"
        else 0
    )
    prime_subs = (
        prime_buf.data.get("submissions_today", 0)
        if prime_buf and prime_buf.status == "UP"
        else 0
    )
    alpha_gos = (
        alpha_buf.data.get("go_verdicts_today", 0)
        if alpha_buf and alpha_buf.status == "UP"
        else 0
    )
    prime_gos = (
        prime_buf.data.get("go_verdicts_today", 0)
        if prime_buf and prime_buf.status == "UP"
        else 0
    )
    if (alpha_subs > 0 or prime_subs > 0) and (alpha_gos == 0 and prime_gos == 0):
        return DiagnosisResult(
            diagnosis="concordance_failing",
            severity="CRITICAL",
            escalate=True,
            details=(
                f"Submissions={alpha_subs + prime_subs} but GO verdicts=0. "
                "Concordance pipeline is broken — picks are entering but no approvals are exiting."
            ),
            consecutive_dry_cycles=consecutive_dry_cycles,
        )

    # ── Check 4: Capital locked ───────────────────────────────────────────────
    v1 = alpaca.get("v1")
    v2 = alpaca.get("v2")
    # If an account is DOWN, treat its buying_power as above floor (avoid false positive)
    v1_power = v1.buying_power if v1 and v1.status == "UP" else CAPITAL_FLOOR + 1
    v2_power = v2.buying_power if v2 and v2.status == "UP" else CAPITAL_FLOOR + 1
    if v1_power < CAPITAL_FLOOR and v2_power < CAPITAL_FLOOR:
        return DiagnosisResult(
            diagnosis="capital_locked",
            severity="WARN",
            escalate=False,
            details=(
                f"Both Alpaca accounts below capital floor ${CAPITAL_FLOOR:,.0f}. "
                f"V1: ${v1_power:,.0f} | V2: ${v2_power:,.0f}. "
                "Cannot open positions at minimum base size."
            ),
            consecutive_dry_cycles=consecutive_dry_cycles,
        )

    # ── Check 5: OMNI not synthesizing ───────────────────────────────────────
    omni = services.get("omni")
    omni_pulls = (
        omni.data.get("arm_pulls_today", 0) if omni and omni.status == "UP" else 0
    )
    if (alpha_subs > 0 or prime_subs > 0) and omni_pulls == 0:
        return DiagnosisResult(
            diagnosis="omni_not_synthesizing",
            severity="CRITICAL",
            escalate=True,
            details=(
                f"Submissions={alpha_subs + prime_subs} but OMNI arm_pulls=0. "
                "OMNI is not synthesizing picks — Quad Intelligence stalled or unreachable."
            ),
            consecutive_dry_cycles=consecutive_dry_cycles,
        )

    # ── Check 6: Unknown ─────────────────────────────────────────────────────
    return DiagnosisResult(
        diagnosis="unknown",
        severity="CRITICAL",
        escalate=True,
        details=(
            "All diagnostic checks passed but zero trades executed. "
            "Pipeline state is internally inconsistent — manual investigation required."
        ),
        consecutive_dry_cycles=consecutive_dry_cycles,
    )
