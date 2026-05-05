"""
graceful_degradation.py — Approach 6: Graceful Degradation Runtime Survival Doctrine

Converts trading failures from binary halts into graduated quality reductions.
The system continues trading at reduced capacity rather than stopping entirely.

5 degradation levels:
  LEVEL 0: Full operation (target state)
  LEVEL 1: Minor — 1 source on fallback OR 1 brain down → EOD note only
  LEVEL 2: Moderate — multiple fallbacks OR 2 brains down → 50% size, alert Ahmed
  LEVEL 3: Severe — primary sources unavailable OR all brains down → no new entries
  LEVEL 4: Emergency Halt — execution bridge down OR VIX breach → full halt

ABSOLUTE NON-NEGOTIABLES (never degraded under any condition):
  - vix_brake_enforcement
  - position_limit_enforcement
  - invariant_enforcement
  - existing_position_monitoring
  - exit_execution
  - stop_loss_enforcement
  - circuit_breaker_enforcement
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Callable, Dict, List, Optional

import pytz

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


class DegradationLevel(IntEnum):
    """System degradation level — higher = worse."""
    FULL      = 0   # Full operation
    MINOR     = 1   # 1 fallback or 1 brain down
    MODERATE  = 2   # Multiple fallbacks or 2 brains down
    SEVERE    = 3   # Primary sources down or all brains down
    EMERGENCY = 4   # Execution bridge down or VIX breach


# Position size multiplier per level
LEVEL_SIZE_MULTIPLIER = {
    DegradationLevel.FULL:      1.00,
    DegradationLevel.MINOR:     1.00,   # No size change for minor
    DegradationLevel.MODERATE:  0.50,   # 50% size
    DegradationLevel.SEVERE:    0.00,   # No new entries
    DegradationLevel.EMERGENCY: 0.00,   # Full halt
}

# New entries allowed per level
LEVEL_NEW_ENTRIES_ALLOWED = {
    DegradationLevel.FULL:      True,
    DegradationLevel.MINOR:     True,
    DegradationLevel.MODERATE:  True,
    DegradationLevel.SEVERE:    False,
    DegradationLevel.EMERGENCY: False,
}

# Alert Ahmed for these levels
LEVELS_REQUIRING_ALERT = {DegradationLevel.MODERATE, DegradationLevel.SEVERE, DegradationLevel.EMERGENCY}

# These are NEVER degraded under any condition
NON_NEGOTIABLES = {
    "vix_brake_enforcement",
    "position_limit_enforcement",
    "invariant_enforcement",
    "existing_position_monitoring",
    "exit_execution",
    "stop_loss_enforcement",
    "circuit_breaker_enforcement",
}


@dataclass
class DegradationEvent:
    """A single degradation level change."""
    timestamp: str
    from_level: int
    to_level: int
    trigger: str
    size_multiplier: float
    new_entries_allowed: bool
    note: str


@dataclass
class DegradationState:
    """Current degradation state snapshot."""
    level: DegradationLevel
    active_degradations: List[str]
    size_multiplier: float
    new_entries_allowed: bool
    existing_positions_monitored: bool  # NEVER False
    alert_ahmed: bool
    alert_message: Optional[str]


class GracefulDegradationManager:
    """
    Manages system degradation levels during trading hours.

    Core principle: A degraded system that continues trading
    is better than a halted system that misses alpha.
    Exception: Safety gates are NEVER degraded.
    VIX brakes, position limits, and invariants are absolute.

    Degradation affects: position sizing, trade frequency,
    data source quality, AI validation depth.

    Degradation NEVER affects: safety gates, invariant enforcement,
    position monitoring, exit execution.

    Usage:
        mgr = GracefulDegradationManager("NEXUS_ALPHA", 2000.0, telegram_fn)
        state = mgr.apply_degradation("QI_TWO_BRAINS_TIMEOUT")
        size = mgr.get_effective_size()
        if mgr.is_new_entry_allowed():
            ...
    """

    # Full degradation rules per condition key — exact spec implementation
    DEGRADATION_RULES: Dict[str, Dict] = {
        "ORATS_PRIMARY_FAILED": {
            "level": DegradationLevel.MINOR,
            "size_multiplier_delta": 0.0,
            "new_entries_allowed": True,
            "action": "switch_to_polygon_iv",
            "message": None,
        },
        "ALL_IV_SOURCES_FAILED": {
            "level": DegradationLevel.SEVERE,
            "size_multiplier_delta": -1.0,
            "new_entries_allowed": False,
            "action": "halt_new_entries_manage_existing",
            "message": "All IV sources unavailable. No new entries. Managing existing positions.",
        },
        "QI_ONE_BRAIN_TIMEOUT": {
            "level": DegradationLevel.MINOR,
            "size_multiplier_delta": 0.0,
            "new_entries_allowed": True,
            "action": "operate_as_2_brain_system",
            "message": None,
        },
        "QI_TWO_BRAINS_TIMEOUT": {
            "level": DegradationLevel.MODERATE,
            "size_multiplier_delta": -0.75,
            "new_entries_allowed": True,
            "action": "operate_as_degraded_1_brain_0.25x",
            "message": "QI severely degraded. 2/3 brains unavailable. Reducing to 25% size.",
        },
        "QI_ALL_BRAINS_TIMEOUT": {
            "level": DegradationLevel.SEVERE,
            "size_multiplier_delta": -1.0,
            "new_entries_allowed": False,
            "action": "suspend_new_entries",
            "message": "QI fully unavailable. No new entries until brains recover.",
        },
        "CAPITAL_ROUTER_SLOW": {
            "level": DegradationLevel.MINOR,
            "size_multiplier_delta": 0.0,
            "new_entries_allowed": True,
            "action": "increase_cr_timeout",
            "message": None,
        },
        "CAPITAL_ROUTER_UNREACHABLE": {
            "level": DegradationLevel.MODERATE,
            "size_multiplier_delta": -0.5,
            "new_entries_allowed": True,
            "action": "use_service_level_limits_only",
            "message": "Capital Router unreachable. Using service-level position limits only.",
        },
        "PRICE_DATA_DELAYED": {
            "level": DegradationLevel.MINOR,
            "size_multiplier_delta": -0.25,
            "new_entries_allowed": True,
            "action": "continue_with_delayed_data",
            "message": None,
        },
        "EXIT_MONITOR_SLOWED": {
            "level": DegradationLevel.MODERATE,
            "size_multiplier_delta": 0.0,
            "new_entries_allowed": False,
            "action": "halt_new_entries_prioritize_monitoring",
            "message": "Exit monitor degraded. Halting new entries to focus on existing positions.",
        },
        "ALPACA_RATE_LIMITED": {
            "level": DegradationLevel.MODERATE,
            "size_multiplier_delta": -0.5,
            "new_entries_allowed": True,
            "action": "exponential_backoff_and_reduce_frequency",
            "message": "Alpaca rate limiting detected. Reducing trade frequency.",
        },
        "VIX_BRAKE_DEBIT": {
            "level": DegradationLevel.SEVERE,
            "size_multiplier_delta": -1.0,
            "new_entries_allowed": False,
            "action": "halt_debit_positions",
            "message": "VIX > 30. All debit positions halted. Credit positions may continue.",
        },
        "VIX_BRAKE_FULL": {
            "level": DegradationLevel.EMERGENCY,
            "size_multiplier_delta": -1.0,
            "new_entries_allowed": False,
            "action": "full_halt",
            "message": "VIX > 40. FULL HALT. No new positions of any kind.",
        },
        "CIRCUIT_BREAKER_DAILY": {
            "level": DegradationLevel.EMERGENCY,
            "size_multiplier_delta": -1.0,
            "new_entries_allowed": False,
            "action": "halt_service",
            "message": "Daily loss limit reached. Service halted.",
        },
    }

    def __init__(
        self,
        service_name: str = "NEXUS",
        base_size_usd: float = 2000.0,
        telegram_alert_fn: Optional[Callable] = None,
    ):
        """
        Args:
            service_name: Service identifier for logging/alerts.
            base_size_usd: Base position size before degradation multiplier.
            telegram_alert_fn: Callable(message: str) for Ahmed alerts.
        """
        self.service_name = service_name
        self.base_size_usd = base_size_usd
        self.telegram_alert = telegram_alert_fn
        self._active_degradations: List[str] = []
        self._current_level = DegradationLevel.FULL
        self._current_size_multiplier = 1.0
        self._events: List[DegradationEvent] = []

    def apply_degradation(self, degradation_key: str) -> DegradationState:
        """
        Apply a degradation condition. Call when failure is detected.

        Args:
            degradation_key: Key from DEGRADATION_RULES (e.g. "QI_TWO_BRAINS_TIMEOUT").

        Returns:
            Current DegradationState after applying the condition.
        """
        rule = self.DEGRADATION_RULES.get(degradation_key)
        if not rule:
            logger.warning("Unknown degradation key: %s", degradation_key)
            return self._get_current_state()

        if degradation_key not in self._active_degradations:
            self._active_degradations.append(degradation_key)

        new_level = rule["level"]
        if new_level.value > self._current_level.value:
            old_level = self._current_level
            self._current_level = new_level
            self._events.append(DegradationEvent(
                timestamp=datetime.now(ET).isoformat(),
                from_level=old_level.value,
                to_level=new_level.value,
                trigger=degradation_key,
                size_multiplier=self._current_size_multiplier,
                new_entries_allowed=rule["new_entries_allowed"],
                note=rule.get("action", ""),
            ))

        delta = rule.get("size_multiplier_delta", 0.0)
        if delta < 0:
            self._current_size_multiplier = max(0.0, self._current_size_multiplier + delta)

        alert_levels = {DegradationLevel.MODERATE, DegradationLevel.SEVERE, DegradationLevel.EMERGENCY}
        if new_level in alert_levels and rule.get("message"):
            logger.warning("DEGRADATION [%s] %s: %s", self.service_name, degradation_key, rule["message"])

        state = self._get_current_state()
        if state.alert_ahmed and state.alert_message and self.telegram_alert:
            try:
                self.telegram_alert(state.alert_message)
            except Exception as e:
                logger.error("Failed to send degradation alert: %s", e)
        return state

    def clear_degradation(self, degradation_key: str) -> DegradationState:
        """
        Clear a degradation when the condition is resolved.

        Args:
            degradation_key: The condition key to clear.

        Returns:
            Current DegradationState after recalculating.
        """
        if degradation_key in self._active_degradations:
            self._active_degradations.remove(degradation_key)
            logger.info("Degradation cleared: %s. Recalculating state.", degradation_key)
            self._recalculate_state()
        return self._get_current_state()

    def get_effective_size(self, base_size: Optional[float] = None) -> float:
        """
        Get current effective position size after degradation multiplier.

        Args:
            base_size: Override base size. Uses self.base_size_usd if None.

        Returns:
            Effective size in USD.
        """
        b = base_size if base_size is not None else self.base_size_usd
        return b * self._current_size_multiplier

    def get_size_multiplier(self) -> float:
        """Return current size multiplier (0.0–1.0)."""
        return self._current_size_multiplier

    def is_new_entry_allowed(self) -> bool:
        """Return True if new positions can be opened in current state."""
        for key in self._active_degradations:
            rule = self.DEGRADATION_RULES.get(key, {})
            if not rule.get("new_entries_allowed", True):
                return False
        return True

    def is_non_negotiable(self, action: str) -> bool:
        """Return True if action is a non-negotiable safety gate."""
        return action in NON_NEGOTIABLES

    def on_event(self, event_type: str, detail: Optional[str] = None) -> DegradationLevel:
        """Alias for apply_degradation — returns the new DegradationLevel."""
        self.apply_degradation(event_type)
        return self.current_level

    @property
    def current_level(self) -> DegradationLevel:
        return self._current_level

    def get_status_summary(self) -> Dict:
        """Return current degradation status summary dict."""
        return {
            "level": self._current_level.value,
            "level_name": self._current_level.name,
            "size_multiplier": self._current_size_multiplier,
            "new_entries_allowed": self.is_new_entry_allowed(),
            "active_conditions": list(self._active_degradations),
            "total_events": len(self._events),
        }

    def get_eod_degradation_report(self) -> Dict:
        """Returns degradation event log for EOD report."""
        return {
            "final_level": self._current_level.name,
            "total_degradation_events": len(self._events),
            "events": [
                {
                    "timestamp": e.timestamp,
                    "from": DegradationLevel(e.from_level).name,
                    "to": DegradationLevel(e.to_level).name,
                    "trigger": e.trigger,
                    "size_multiplier": e.size_multiplier,
                }
                for e in self._events[-20:]
            ],
        }

    def _recalculate_state(self) -> None:
        """Recalculate level and multiplier from scratch after clearing a condition."""
        self._current_level = DegradationLevel.FULL
        self._current_size_multiplier = 1.0
        for key in self._active_degradations:
            rule = self.DEGRADATION_RULES.get(key, {})
            level = rule.get("level", DegradationLevel.MINOR)
            if level.value > self._current_level.value:
                self._current_level = level
            delta = rule.get("size_multiplier_delta", 0.0)
            if delta < 0:
                self._current_size_multiplier = max(0.0, self._current_size_multiplier + delta)

    def _get_current_state(self) -> DegradationState:
        """Build current DegradationState snapshot."""
        alert_levels = {DegradationLevel.MODERATE, DegradationLevel.SEVERE, DegradationLevel.EMERGENCY}
        alert_ahmed = self._current_level in alert_levels

        alert_message: Optional[str] = None
        if alert_ahmed:
            messages = [
                self.DEGRADATION_RULES.get(k, {}).get("message", "")
                for k in self._active_degradations
                if self.DEGRADATION_RULES.get(k, {}).get("message")
            ]
            if messages:
                icon = "🚨" if self._current_level == DegradationLevel.EMERGENCY else "⚠️"
                alert_message = (
                    f"{icon} SYSTEM DEGRADED: {self.service_name}\n"
                    f"Level: {self._current_level.name}\n"
                    f"Size multiplier: {self._current_size_multiplier:.2f}\n"
                    f"Active conditions:\n"
                    + "\n".join(f"  → {m}" for m in messages)
                )

        return DegradationState(
            level=self._current_level,
            active_degradations=list(self._active_degradations),
            size_multiplier=self._current_size_multiplier,
            new_entries_allowed=self.is_new_entry_allowed(),
            existing_positions_monitored=True,   # NEVER False
            alert_ahmed=alert_ahmed,
            alert_message=alert_message,
        )
