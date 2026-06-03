"""models.py — Outcome Monitor data models."""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ServiceSnapshot:
    """Snapshot of a single Nexus service health poll."""

    name: str
    status: str  # "UP" or "DOWN"
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class AlpacaSnapshot:
    """Snapshot of an Alpaca paper trading account."""

    account_id: str
    status: str  # "UP" or "DOWN"
    buying_power: float = 0.0
    portfolio_value: float = 0.0
    open_positions: int = 0
    error: Optional[str] = None


@dataclass
class DiagnosisResult:
    """Result of the diagnosis decision tree."""

    diagnosis: str
    severity: str  # "INFO", "WARN", "CRITICAL"
    escalate: bool
    details: str
    consecutive_dry_cycles: int = 0


@dataclass
class CycleResult:
    """Full result of one outcome monitor cycle."""

    cycle_ts: str
    market_open: bool
    trades_since_last_cycle: int
    diagnosis: DiagnosisResult
    services: Dict[str, ServiceSnapshot]
    alpaca: Dict[str, AlpacaSnapshot]
    escalated: bool = False
