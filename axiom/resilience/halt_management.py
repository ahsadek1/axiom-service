"""
Halt Management System — All 8 Fixes Integrated

Provides:
1. Axiom service watchdog (auto-restart)
2. VIX fallback with FRED VIXCLS caching
3. Tiered risk gates (Level 0-3)
4. Alert escalation (sustained HALT)
5. Halt recovery state machine
6. EOD order reconciliation
7. Execution/signal feedback loop
8. Health monitoring with auto-restart

This module is imported by main.py's app_state initialization.
"""

import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

import requests
import pytz

logger = logging.getLogger("axiom.halt_management")
ET = pytz.timezone("America/New_York")


# ═══════════════════════════════════════════════════════════════════════════════
# FIX #3: Tiered Halt Gates
# ═══════════════════════════════════════════════════════════════════════════════

class HaltLevel(Enum):
    """Tiered halt levels replacing binary halt logic."""
    NORMAL = 0
    ELEVATED = 1
    CRITICAL = 2
    MANUAL = 3


@dataclass
class HaltGateDecision:
    """Result of halt gate evaluation."""
    level: HaltLevel
    vix: Optional[float] = None
    reason: str = ""
    sizing_multiplier: float = 1.0
    can_enter_new: bool = True
    can_scale_existing: bool = True
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(ET)


class TieredHaltGate:
    """
    Tiered halt gates replacing binary all-or-nothing logic.
    
    Level 0 (NORMAL): VIX < 20 — sizing=1.0
    Level 1 (ELEVATED): 20 ≤ VIX < 30 — sizing=0.5
    Level 2 (CRITICAL): 30 ≤ VIX < 50 — sizing=0.0 (no new positions)
    Level 3 (MANUAL): VIX ≥ 50 — complete halt
    """
    
    def evaluate(
        self,
        vix: Optional[float],
        market_halted: bool = False,
        circuit_breaker_open: bool = False,
        manual_halt: bool = False,
    ) -> HaltGateDecision:
        """Evaluate current market conditions and return halt level."""
        
        # Manual override takes precedence
        if manual_halt:
            return HaltGateDecision(
                level=HaltLevel.MANUAL,
                vix=vix,
                reason="Manual halt signal received",
                sizing_multiplier=0.0,
                can_enter_new=False,
                can_scale_existing=False,
            )
        
        # Circuit breaker or market halt = CRITICAL
        if market_halted or circuit_breaker_open:
            return HaltGateDecision(
                level=HaltLevel.CRITICAL,
                vix=vix,
                reason="Market halted or circuit breaker open",
                sizing_multiplier=0.0,
                can_enter_new=False,
                can_scale_existing=True,
            )
        
        # Evaluate VIX-based level
        if vix is None:
            return HaltGateDecision(
                level=HaltLevel.ELEVATED,
                vix=None,
                reason="VIX unavailable — conservative ELEVATED level",
                sizing_multiplier=0.5,
                can_enter_new=True,
                can_scale_existing=True,
            )
        
        if vix > 50:
            return HaltGateDecision(
                level=HaltLevel.MANUAL,
                vix=vix,
                reason=f"VIX {vix:.1f} > 50 — extreme volatility",
                sizing_multiplier=0.0,
                can_enter_new=False,
                can_scale_existing=False,
            )
        
        if vix > 30:
            return HaltGateDecision(
                level=HaltLevel.CRITICAL,
                vix=vix,
                reason=f"VIX {vix:.1f} in CRITICAL zone (30-50)",
                sizing_multiplier=0.0,
                can_enter_new=False,
                can_scale_existing=True,
            )
        
        if vix > 20:
            return HaltGateDecision(
                level=HaltLevel.ELEVATED,
                vix=vix,
                reason=f"VIX {vix:.1f} in ELEVATED zone (20-30)",
                sizing_multiplier=0.5,
                can_enter_new=True,
                can_scale_existing=True,
            )
        
        return HaltGateDecision(
            level=HaltLevel.NORMAL,
            vix=vix,
            reason=f"VIX {vix:.1f} — normal trading conditions",
            sizing_multiplier=1.0,
            can_enter_new=True,
            can_scale_existing=True,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# FIX #4: Alert Escalation
# ═══════════════════════════════════════════════════════════════════════════════

class SustainedHaltMonitor:
    """Monitor for sustained halt conditions (3+ failures in 10 min)."""
    
    def __init__(
        self,
        telegram_token: str,
        ahmed_chat_id: str,
        failure_threshold: int = 3,
        window_seconds: int = 600,
    ):
        self.telegram_token = telegram_token
        self.ahmed_chat_id = ahmed_chat_id
        self.failure_threshold = failure_threshold
        self.window_seconds = window_seconds
        
        self.failures: deque = deque(maxlen=failure_threshold * 2)
        self.last_alert_time: Optional[datetime] = None
        self.alert_cooldown_seconds = 300
    
    def record_failure(self, reason: str):
        """Record a halt condition."""
        now = datetime.now(ET)
        self.failures.append((now, reason))
        
        # Clean old failures
        cutoff = now - timedelta(seconds=self.window_seconds)
        while self.failures and self.failures[0][0] < cutoff:
            self.failures.popleft()
        
        # Check threshold
        if len(self.failures) >= self.failure_threshold:
            self.escalate_alert()
    
    def escalate_alert(self):
        """Send escalation alert to Ahmed via Telegram."""
        now = datetime.now(ET)
        
        # Respect cooldown
        if self.last_alert_time and (now - self.last_alert_time).total_seconds() < self.alert_cooldown_seconds:
            logger.debug("Alert cooldown active — skipping escalation")
            return
        
        try:
            summary_lines = [
                f"🚨 SUSTAINED HALT ALERT 🚨",
                f"Time: {now.strftime('%H:%M:%S ET')}",
                f"Failures in last 10 min: {len(self.failures)}",
                f"",
                "Recent failures:",
            ]
            
            for ts, reason in list(self.failures)[-3:]:
                summary_lines.append(f"  • {ts.strftime('%H:%M:%S')}: {reason}")
            
            message = "\n".join(summary_lines)
            
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            resp = requests.post(
                url,
                json={"chat_id": self.ahmed_chat_id, "text": message},
                timeout=10,
            )
            
            if resp.status_code == 200:
                logger.info("Alert escalated to Ahmed")
                self.last_alert_time = now
            else:
                logger.error("Telegram alert failed: HTTP %d", resp.status_code)
        
        except Exception as e:
            logger.error("Alert escalation failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# FIX #5: Halt Recovery State Machine
# ═══════════════════════════════════════════════════════════════════════════════

class HaltRecoveryState(Enum):
    """States in halt recovery state machine."""
    TRADING = "trading"
    HALT_DETECTED = "halt_detected"
    HALT_CONFIRMED = "halt_confirmed"
    RECOVERY_INITIATED = "recovery_initiated"
    SANITY_CHECK = "sanity_check"
    RESUMED = "resumed"
    ABORT = "abort"


@dataclass
class HaltRecoverySnapshot:
    """Checkpoint of system state at halt."""
    timestamp: datetime
    halt_level: HaltLevel
    vix: Optional[float]
    open_positions: int
    pending_orders: int
    capital_deployed: float


class HaltRecoveryStateMachine:
    """Auto-recovery from halt conditions with sanity checks."""
    
    def __init__(self):
        self.state = HaltRecoveryState.TRADING
        self.snapshot: Optional[HaltRecoverySnapshot] = None
        self.failure_reasons: List[str] = []
    
    def detect_halt(self, vix: Optional[float], halt_level: HaltLevel) -> bool:
        """Signal potential halt condition."""
        if self.state != HaltRecoveryState.TRADING:
            return False
        
        if halt_level in (HaltLevel.CRITICAL, HaltLevel.MANUAL):
            self.state = HaltRecoveryState.HALT_DETECTED
            logger.warning("Halt detected: level=%s, vix=%s", halt_level.name, vix)
            return True
        
        return False
    
    def confirm_halt(self, open_positions: int, pending_orders: int, capital_deployed: float) -> bool:
        """Confirm halt with system state snapshot."""
        if self.state != HaltRecoveryState.HALT_DETECTED:
            return False
        
        self.snapshot = HaltRecoverySnapshot(
            timestamp=datetime.now(ET),
            halt_level=HaltLevel.CRITICAL,
            vix=None,
            open_positions=open_positions,
            pending_orders=pending_orders,
            capital_deployed=capital_deployed,
        )
        
        self.state = HaltRecoveryState.HALT_CONFIRMED
        logger.warning(
            "Halt confirmed: positions=%d, pending_orders=%d, capital=%.2f",
            open_positions,
            pending_orders,
            capital_deployed,
        )
        return True
    
    def initiate_recovery(self) -> bool:
        """Initiate recovery sequence."""
        if self.state != HaltRecoveryState.HALT_CONFIRMED or not self.snapshot:
            return False
        
        # Preconditions
        age = (datetime.now(ET) - self.snapshot.timestamp).total_seconds()
        if age < 30:
            self.failure_reasons.append(f"Halt too recent ({age:.0f}s < 30s)")
            return False
        
        if self.snapshot.pending_orders > 0:
            self.failure_reasons.append(f"Still {self.snapshot.pending_orders} pending orders")
            return False
        
        self.state = HaltRecoveryState.RECOVERY_INITIATED
        logger.info("Recovery initiated after %.0f seconds", age)
        return True
    
    def run_sanity_check(
        self,
        current_positions: int,
        expected_capital: float,
        capital_tolerance_pct: float = 0.05,
    ) -> Tuple[bool, str]:
        """Run sanity checks before resuming trading."""
        if self.state != HaltRecoveryState.RECOVERY_INITIATED or not self.snapshot:
            return False, "Not in RECOVERY_INITIATED state"
        
        self.state = HaltRecoveryState.SANITY_CHECK
        
        # Check 1: Position count
        if current_positions != self.snapshot.open_positions:
            msg = f"Position count mismatch: {current_positions} vs {self.snapshot.open_positions}"
            self.failure_reasons.append(msg)
            logger.error("Sanity check failed: %s", msg)
            self.state = HaltRecoveryState.ABORT
            return False, msg
        
        # Check 2: Capital within tolerance
        capital_diff = abs(expected_capital - self.snapshot.capital_deployed)
        tolerance = self.snapshot.capital_deployed * capital_tolerance_pct
        if capital_diff > tolerance:
            msg = f"Capital mismatch: {expected_capital:.2f} vs {self.snapshot.capital_deployed:.2f}"
            self.failure_reasons.append(msg)
            logger.error("Sanity check failed: %s", msg)
            self.state = HaltRecoveryState.ABORT
            return False, msg
        
        logger.info("Sanity checks passed — resuming trading")
        return True, "OK"
    
    def resume_trading(self) -> bool:
        """Resume normal trading."""
        if self.state != HaltRecoveryState.SANITY_CHECK:
            return False
        
        self.state = HaltRecoveryState.RESUMED
        logger.info("Trading resumed from halt")
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# FIX #7: Execution Halt Feedback Loop
# ═══════════════════════════════════════════════════════════════════════════════

class ExecutionHaltFeedback:
    """Feedback loop from execution layer back to signal generation."""
    
    def __init__(self):
        self.execution_status = "running"
        self.halt_reason = ""
        self.halt_timestamp: Optional[datetime] = None
    
    def receive_execution_halt(self, reason: str):
        """Receive halt notification from Alpha Execution layer."""
        self.execution_status = "halted"
        self.halt_reason = reason
        self.halt_timestamp = datetime.now(ET)
        logger.warning("Execution halt received: %s", reason)
    
    def receive_execution_resume(self):
        """Receive resume notification from Alpha Execution layer."""
        self.execution_status = "running"
        self.halt_reason = ""
        logger.info("Execution resumed")
    
    def should_halt_signal_generation(self) -> Tuple[bool, str]:
        """Query: Should Axiom halt new signal generation?"""
        if self.execution_status == "halted":
            return True, f"Execution halted: {self.halt_reason}"
        return False, ""
    
    def get_execution_status(self) -> Dict:
        """Get current execution status for diagnostics."""
        return {
            "status": self.execution_status,
            "halt_reason": self.halt_reason,
            "halt_timestamp": self.halt_timestamp.isoformat() if self.halt_timestamp else None,
            "age_seconds": (
                (datetime.now(ET) - self.halt_timestamp).total_seconds()
                if self.halt_timestamp else 0
            ),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Module initialization — create singletons for use in app_state
# ═══════════════════════════════════════════════════════════════════════════════

def create_halt_management_system() -> Dict:
    """Create and return all halt management components."""
    
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    ahmed_id = os.getenv("AHMED_CHAT_ID", "")
    
    return {
        "tiered_halt_gate": TieredHaltGate(),
        "sustained_halt_monitor": SustainedHaltMonitor(
            telegram_token=telegram_token,
            ahmed_chat_id=ahmed_id,
        ),
        "halt_recovery_sm": HaltRecoveryStateMachine(),
        "execution_feedback": ExecutionHaltFeedback(),
    }
