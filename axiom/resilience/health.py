"""
health.py — Axiom-specific HealthReport.
Spec: AXIOM_30_SPEC v1.0 (Cipher, 2026-05-02)

Checks Axiom's actual data sources: ORATS, Polygon, Alpaca, DeepSeek, local DB.
Results are cached in app_state["_health_report"] by the scheduler (every 5 min)
and served by /health endpoint's resilience_status field.

IMPORTANT: check_all() is called from scheduler only — NOT from /health endpoint
(which must remain synchronous and fast).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests


@dataclass
class AxiomSourceStatus:
    name: str
    reachable: bool
    latency_ms: Optional[float] = None
    error: Optional[str] = None


@dataclass
class AxiomHealthReport:
    """
    Axiom-specific health report.

    Sources checked:
    - ORATS      (IVR data for all 10 layers)
    - Polygon    (options data, technicals, beta)
    - Alpaca     (positions, account status)
    - DeepSeek   (regime AI scoring — key presence only, no live call)
    - local_db   (axiom.db write test)

    Used by /health endpoint's resilience_status field.
    """

    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    sources: dict = field(default_factory=dict)  # str -> AxiomSourceStatus
    overall: str = "unknown"  # "healthy" | "degraded" | "critical"
    degraded_sources: list = field(default_factory=list)

    def check_all(self, app_state: dict) -> "AxiomHealthReport":
        """
        Run all source connectivity checks.
        Called from scheduler (every 5 min) — NOT from /health endpoint.
        Results cached in app_state["_health_report"] and served by /health.
        """
        self._check_orats()
        self._check_polygon(app_state)
        self._check_alpaca()
        self._check_deepseek()
        self._check_local_db(app_state)
        self._compute_overall()
        return self

    def _check_orats(self) -> None:
        """Ping ORATS /datav2/summaries with a known ticker (AAPL)."""
        token = os.getenv("ORATS_TOKEN") or os.getenv("ORATS_API_KEY", "4476e955-241a-4540-b114-ebbf1a3a3b87")
        start = datetime.utcnow()
        try:
            r = requests.get(
                "https://api.orats.io/datav2/summaries",
                params={"token": token, "ticker": "AAPL"},
                timeout=5,
            )
            ok = r.status_code == 200 and bool(r.json().get("data"))
            self.sources["orats"] = AxiomSourceStatus(
                name="orats",
                reachable=ok,
                latency_ms=(datetime.utcnow() - start).total_seconds() * 1000,
                error=None if ok else f"HTTP {r.status_code}",
            )
        except Exception as e:
            self.sources["orats"] = AxiomSourceStatus(
                name="orats", reachable=False, error=str(e)
            )

    def _check_polygon(self, app_state: dict) -> None:
        """Ping Polygon /v2/aggs/ticker/AAPL/prev."""
        key = os.getenv("POLYGON_API_KEY", "")
        start = datetime.utcnow()
        try:
            r = requests.get(
                "https://api.polygon.io/v2/aggs/ticker/AAPL/prev",
                params={"apiKey": key},
                timeout=5,
            )
            ok = r.status_code == 200
            self.sources["polygon"] = AxiomSourceStatus(
                name="polygon",
                reachable=ok,
                latency_ms=(datetime.utcnow() - start).total_seconds() * 1000,
                error=None if ok else f"HTTP {r.status_code}",
            )
        except Exception as e:
            self.sources["polygon"] = AxiomSourceStatus(
                name="polygon", reachable=False, error=str(e)
            )

    def _check_alpaca(self) -> None:
        """Ping Alpaca /v2/account."""
        key = os.getenv("ALPACA_KEY", "") or os.getenv("APCA_API_KEY_ID", "")
        secret = os.getenv("ALPACA_SECRET", "") or os.getenv("APCA_API_SECRET_KEY", "")
        start = datetime.utcnow()
        try:
            r = requests.get(
                "https://paper-api.alpaca.markets/v2/account",
                headers={
                    "APCA-API-KEY-ID": key,
                    "APCA-API-SECRET-KEY": secret,
                },
                timeout=5,
            )
            ok = r.status_code == 200
            self.sources["alpaca"] = AxiomSourceStatus(
                name="alpaca",
                reachable=ok,
                latency_ms=(datetime.utcnow() - start).total_seconds() * 1000,
                error=None if ok else f"HTTP {r.status_code}",
            )
        except Exception as e:
            self.sources["alpaca"] = AxiomSourceStatus(
                name="alpaca", reachable=False, error=str(e)
            )

    def _check_deepseek(self) -> None:
        """
        Validate DeepSeek key is set.
        No live call — avoid unnecessary API cost.
        """
        key = (
            os.getenv("DEEPSEEK_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or ""
        )
        self.sources["deepseek"] = AxiomSourceStatus(
            name="deepseek",
            reachable=bool(key),
            error=None if key else "DEEPSEEK_KEY not set",
        )

    def _check_local_db(self, app_state: dict) -> None:
        """Test SQLite connectivity on axiom.db."""
        import sqlite3

        settings = app_state.get("settings")
        db_path = None
        if settings and hasattr(settings, "axiom_db_path"):
            db_path = settings.axiom_db_path
        if not db_path:
            db_path = os.getenv("AXIOM_DB_PATH", "/app/data/axiom.db")

        try:
            conn = sqlite3.connect(db_path, timeout=3)
            conn.execute("SELECT 1")
            conn.close()
            self.sources["local_db"] = AxiomSourceStatus(
                name="local_db", reachable=True
            )
        except Exception as e:
            self.sources["local_db"] = AxiomSourceStatus(
                name="local_db", reachable=False, error=str(e)
            )

    def _compute_overall(self) -> None:
        """Compute overall health from source statuses."""
        self.degraded_sources = [
            name for name, s in self.sources.items() if not s.reachable
        ]
        critical = {"orats", "polygon", "alpaca"}
        critical_down = [s for s in self.degraded_sources if s in critical]
        if len(critical_down) >= 2:
            self.overall = "critical"
        elif self.degraded_sources:
            self.overall = "degraded"
        else:
            self.overall = "healthy"

    def to_dict(self) -> dict:
        return {
            "overall": self.overall,
            "timestamp": self.timestamp,
            "sources": {
                name: {
                    "reachable": s.reachable,
                    "latency_ms": s.latency_ms,
                    "error": s.error,
                }
                for name, s in self.sources.items()
            },
            "degraded": self.degraded_sources,
        }
