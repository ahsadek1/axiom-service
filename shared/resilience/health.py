"""
health.py — Standard health report schema for all Nexus agents.
Spec: NEXUS_RESILIENCE_BASE v1.1 (Cipher, 2026-05-02)
Status: Zero conflicts — additive.

Every agent's /health endpoint returns HealthReport.to_dict().
This gives VECTOR, SOVEREIGN, and monitoring tools a single consistent
surface to query across all 9+ services.

Usage:
    health = HealthReport(agent="axiom", version="4.2.0")

    # Report a data source
    health.add_source("orats", ok=True)
    health.add_source("alpaca", ok=False, fallback=True,
                      fallback_source="cache", error="timeout after 5s")

    # Suspend scanning (e.g. VIX halt)
    health.suspend("VIX >= 30 — hard halt active")

    # Resume after halt lifts
    health.resume()

    # In the Flask/FastAPI route:
    @app.get("/health")
    def get_health():
        return health.to_dict()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class HealthStatus(str, Enum):
    HEALTHY   = "HEALTHY"    # all sources OK, scanning active
    DEGRADED  = "DEGRADED"   # fallback active, still producing verdicts
    SUSPENDED = "SUSPENDED"  # scanning intentionally paused (VIX halt etc.)
    UNHEALTHY = "UNHEALTHY"  # critical source down, not producing verdicts


@dataclass
class SourceHealth:
    """Health status of one data source or dependency."""
    name: str
    status: str                        # "OK" | "DEGRADED" | "DOWN"
    fallback_active: bool = False
    fallback_source: Optional[str] = None
    error: Optional[str] = None


@dataclass
class HealthReport:
    """
    Agent-level health report. Aggregates source statuses and surface state.

    Args:
        agent    — agent name (e.g. "axiom", "omni", "cipher")
        version  — service version string

    Auto-fields:
        started_at — set to utcnow() on construction
        status     — starts HEALTHY; degrades as add_source() is called
    """

    agent: str
    version: str
    started_at: datetime = field(default_factory=datetime.utcnow)
    status: HealthStatus = HealthStatus.HEALTHY
    sources: list = field(default_factory=list)
    suspended_reason: Optional[str] = None
    last_cycle: Optional[datetime] = None
    last_skip_rate: float = 0.0
    extra: dict = field(default_factory=dict)  # agent-specific fields (VIX, regime, etc.)

    @property
    def uptime_seconds(self) -> int:
        return int((datetime.utcnow() - self.started_at).total_seconds())

    def add_source(
        self,
        name: str,
        ok: bool,
        fallback: bool = False,
        fallback_source: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """
        Register a data source health status.

        Status derivation:
            ok=True                → "OK"
            ok=False, fallback=True → "DEGRADED" (agent-level → DEGRADED)
            ok=False, fallback=False → "DOWN"    (agent-level → UNHEALTHY)

        Args:
            name            — source name (e.g. "orats", "alpaca", "axiom_api")
            ok              — True if source is responding correctly
            fallback        — True if we're using a fallback (cache, stale data)
            fallback_source — name of the fallback being used
            error           — error description if not ok
        """
        derived_status = "OK" if ok else ("DEGRADED" if fallback else "DOWN")
        self.sources.append(
            SourceHealth(
                name=name,
                status=derived_status,
                fallback_active=fallback,
                fallback_source=fallback_source,
                error=error,
            )
        )
        # Escalate agent status — never downgrade (UNHEALTHY stays UNHEALTHY)
        if not ok and not fallback:
            self.status = HealthStatus.UNHEALTHY
        elif fallback and self.status == HealthStatus.HEALTHY:
            self.status = HealthStatus.DEGRADED

    def suspend(self, reason: str) -> None:
        """
        Mark scanning as intentionally suspended (e.g. VIX hard halt).
        Does not affect source statuses — just the top-level state.
        """
        self.status = HealthStatus.SUSPENDED
        self.suspended_reason = reason

    def resume(self) -> None:
        """
        Clear suspension and return to HEALTHY.
        Source-level issues will re-escalate on the next add_source() call.
        """
        self.status = HealthStatus.HEALTHY
        self.suspended_reason = None

    def set_extra(self, key: str, value) -> None:
        """
        Add an agent-specific field to the health dict.
        Example: health.set_extra("vix", 18.4)
                 health.set_extra("regime", "NEUTRAL")
        """
        self.extra[key] = value

    def to_dict(self) -> dict:
        """
        Serialize to JSON-safe dict for /health endpoint responses.
        Includes all standard fields plus any agent-specific extra fields.
        """
        return {
            "agent": self.agent,
            "version": self.version,
            "status": self.status.value,
            "uptime_seconds": self.uptime_seconds,
            "suspended_reason": self.suspended_reason,
            "last_cycle": self.last_cycle.isoformat() if self.last_cycle else None,
            "last_skip_rate": round(self.last_skip_rate, 3),
            "sources": [
                {
                    "name": s.name,
                    "status": s.status,
                    "fallback_active": s.fallback_active,
                    "fallback_source": s.fallback_source,
                    "error": s.error,
                }
                for s in self.sources
            ],
            **self.extra,
        }
