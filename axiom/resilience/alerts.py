"""
alerts.py — Deduped Axiom alert router.
Spec: AXIOM RESILIENCE LAYER 30% v1.0 (Cipher, 2026-05-02)

Replaces scattered report("axiom", ...) + logger.warning() pairs
with a single deduped alerter that surfaces to Telegram via sovereign_comms.

Dedup window: 5 minutes per (category, ticker) pair.
This is tighter than the shared 30-min window — Axiom is high-frequency and
needs faster re-alert on the same ticker after a data outage clears.

Categories:
    CRITICAL_FLAG           — risk engine emitted a critical flag (SCORE_EXTREME, etc.)
    CONFIG_MISMATCH         — live config doesn't match expected state
    DATA_CONTRACT_VIOLATION — DataContractError caught at a data boundary
    HEALTH_FAIL             — source connectivity check failed
    STANDBY_ENTERED         — Axiom switched to standby mode
    STANDBY_EXITED          — Axiom returned to active mode
"""

from __future__ import annotations

import logging
import sys
import os
import threading
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

logger = logging.getLogger("axiom.resilience.alerts")

DEDUP_WINDOW = timedelta(seconds=300)  # 5 minutes

VALID_CATEGORIES = {
    "CRITICAL_FLAG",
    "CONFIG_MISMATCH",
    "DATA_CONTRACT_VIOLATION",
    "HEALTH_FAIL",
    "STANDBY_ENTERED",
    "STANDBY_EXITED",
}


class AxiomAlerter:
    """
    Deduped Telegram alert router for Axiom.

    Thread-safe. Safe to call from scheduler threads and HTTP handlers
    concurrently. All public methods return True if sent, False if deduped.

    Usage:
        alerter = AxiomAlerter()  # one instance, shared via app_state

        # Data contract violation
        alerter.send_contract_violation("orats", "rip", "missing", app_state)

        # Health failure
        alerter.send_health_alert("orats", ["HTTP 503", "timeout"], app_state)

        # Generic
        alerter.send("CRITICAL_FLAG", "AAPL", "SCORE_EXTREME: score=9.2", app_state)
    """

    def __init__(self) -> None:
        self._sent: dict[tuple[str, str], datetime] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(
        self,
        category: str,
        ticker: str,
        message: str,
        app_state: dict,
    ) -> bool:
        """
        Send an alert for (category, ticker) if not recently sent.

        Args:
            category  — one of VALID_CATEGORIES
            ticker    — ticker or logical key (e.g. "AAPL", "orats_health")
            message   — alert body
            app_state — Axiom app_state (used to reach sovereign_comms.report)

        Returns:
            True if sent, False if deduped.
        """
        if category not in VALID_CATEGORIES:
            raise ValueError(f"Unknown alert category: {category!r}")
        key = (category, ticker)
        with self._lock:
            last = self._sent.get(key)
            if last and (datetime.utcnow() - last) < DEDUP_WINDOW:
                logger.debug(
                    "Alert deduped (within 5-min window): category=%s ticker=%s",
                    category, ticker,
                )
                return False
            self._sent[key] = datetime.utcnow()

        self._deliver(category, ticker, message, app_state)
        return True

    def send_health_alert(
        self,
        check_name: str,
        errors: list[str],
        app_state: dict,
    ) -> bool:
        """
        Send a health check failure alert.
        Dedup key = ('HEALTH_FAIL', check_name).

        Args:
            check_name — source name (e.g. "orats", "polygon", "local_db")
            errors     — list of error strings from the health check
            app_state  — Axiom app_state
        """
        msg = f"Axiom health check FAILED: {check_name}\nErrors: {errors}"
        return self.send("HEALTH_FAIL", check_name, msg, app_state)

    def send_contract_violation(
        self,
        source: str,
        field: str,
        reason: str,
        app_state: dict,
    ) -> bool:
        """
        Send a data contract violation alert.
        Dedup key = ('DATA_CONTRACT_VIOLATION', source).

        Args:
            source    — data source name (e.g. "orats:AAPL", "polygon:TSLA")
            field     — field that failed (e.g. "rip", "open_interest")
            reason    — failure reason from DataContractError
            app_state — Axiom app_state
        """
        msg = f"DataContractError: {source}.{field} — {reason}"
        return self.send("DATA_CONTRACT_VIOLATION", source, msg, app_state)

    def reset(self, category: str, ticker: str) -> None:
        """
        Clear the dedup entry for (category, ticker).
        Useful after a problem resolves — next occurrence fires immediately.
        """
        with self._lock:
            self._sent.pop((category, ticker), None)

    def active_keys(self) -> list[tuple[str, str]]:
        """Return currently deduped (category, ticker) pairs."""
        now = datetime.utcnow()
        with self._lock:
            return [
                k for k, ts in self._sent.items()
                if (now - ts) < DEDUP_WINDOW
            ]

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    def _deliver(
        self,
        category: str,
        ticker: str,
        message: str,
        app_state: dict,
    ) -> None:
        """
        Route the alert via sovereign_comms.report().
        Never raises — delivery failure is logged and swallowed here
        so callers can always continue safely.
        """
        try:
            from shared.sovereign_comms import report
            level = "CRITICAL" if category in ("CRITICAL_FLAG", "DATA_CONTRACT_VIOLATION") else "INFO"
            report("axiom", level, {
                "alert_category": category,
                "ticker": ticker,
                "message": message,
            })
        except Exception as exc:
            logger.error(
                "AxiomAlerter delivery failed [%s/%s]: %s — original: %s",
                category, ticker, exc, message[:120],
            )
