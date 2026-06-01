"""
cascade_breaker.py — Circuit Breaker for Failure Cascades
===========================================================
Stops multi-layer failures from cascading through the system.

Rules:
  - Order placement fails 2x → stop screening for 5 min
  - Capital Router unreachable → stop execution
  - Reconciliation fails 3x → escalate and halt

Pattern: Circuit breaker (open/closed/half-open)
  - CLOSED: normal operation
  - OPEN: failures detected, block all operations
  - HALF_OPEN: testing if system recovered
"""
import logging
import time
from typing import Dict, Any
from enum import Enum
from datetime import datetime, timezone

log = logging.getLogger("thesis.cascade_breaker")


class CircuitState(Enum):
    """State of the circuit."""
    CLOSED = "CLOSED"        # Normal operation
    OPEN = "OPEN"            # Failures detected, blocking
    HALF_OPEN = "HALF_OPEN"  # Testing recovery


class CascadeBreaker:
    """
    Prevents failure cascades.
    
    When a component fails:
      1. Record failure
      2. Check if threshold exceeded
      3. If yes, open circuit (block future operations)
      4. After timeout, move to HALF_OPEN (test recovery)
      5. If test passes, close circuit (resume normal)
      6. If test fails, reopen circuit (extend timeout)
    """
    
    def __init__(
        self,
        failure_threshold: int = 2,
        timeout_seconds: int = 300,  # 5 minutes
        half_open_timeout: int = 30,  # 30 seconds to test recovery
    ):
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds
        self.half_open_timeout = half_open_timeout
        
        # State tracking per component
        self.states: Dict[str, Dict[str, Any]] = {
            # "component_name": {
            #     "state": CircuitState.CLOSED,
            #     "failure_count": 0,
            #     "last_failure_time": 0.0,
            #     "opened_at": 0.0,
            #     "reason": "",
            # }
        }
    
    def check_can_execute(self, component: str) -> bool:
        """
        Returns True if safe to execute this component.
        Returns False if circuit is open for this component.
        """
        if component not in self.states:
            # First time seeing this component
            self.states[component] = {
                "state": CircuitState.CLOSED,
                "failure_count": 0,
                "last_failure_time": 0.0,
                "opened_at": 0.0,
                "reason": "",
            }
        
        state_info = self.states[component]
        current_state = state_info["state"]
        now = time.time()
        
        if current_state == CircuitState.CLOSED:
            # Normal operation
            return True
        
        elif current_state == CircuitState.OPEN:
            # Circuit is open. Check if timeout expired.
            time_since_open = now - state_info["opened_at"]
            
            if time_since_open > self.timeout_seconds:
                # Timeout expired. Move to HALF_OPEN to test recovery.
                log.info(
                    "Circuit HALF_OPEN for %s (testing recovery after %.0fs)",
                    component, time_since_open
                )
                state_info["state"] = CircuitState.HALF_OPEN
                return True  # Allow one test request
            else:
                # Still in timeout
                remaining = self.timeout_seconds - time_since_open
                log.warning(
                    "Circuit OPEN for %s — blocking (%.0fs remaining). Reason: %s",
                    component, remaining, state_info["reason"]
                )
                return False
        
        elif current_state == CircuitState.HALF_OPEN:
            # Testing recovery. Allow execution but monitor closely.
            log.info("Circuit HALF_OPEN for %s — allowing test execution", component)
            return True
    
    def record_success(self, component: str) -> None:
        """
        Record successful execution. Used in HALF_OPEN state to test recovery.
        """
        if component not in self.states:
            return
        
        state_info = self.states[component]
        
        if state_info["state"] == CircuitState.HALF_OPEN:
            # Recovery successful! Close circuit.
            log.info("Circuit recovery successful for %s — CLOSING", component)
            state_info["state"] = CircuitState.CLOSED
            state_info["failure_count"] = 0
            state_info["reason"] = ""
        
        elif state_info["state"] == CircuitState.CLOSED:
            # Normal operation, just reset failure count
            state_info["failure_count"] = 0
    
    def record_failure(self, component: str, reason: str = "") -> None:
        """
        Record failed execution.
        If failures exceed threshold, open circuit.
        """
        if component not in self.states:
            self.states[component] = {
                "state": CircuitState.CLOSED,
                "failure_count": 0,
                "last_failure_time": 0.0,
                "opened_at": 0.0,
                "reason": "",
            }
        
        state_info = self.states[component]
        state_info["failure_count"] += 1
        state_info["last_failure_time"] = time.time()
        state_info["reason"] = reason
        
        log.warning(
            "Failure recorded for %s: count=%d threshold=%d — %s",
            component,
            state_info["failure_count"],
            self.failure_threshold,
            reason
        )
        
        if state_info["failure_count"] >= self.failure_threshold:
            # Open circuit
            state_info["state"] = CircuitState.OPEN
            state_info["opened_at"] = time.time()
            
            log.error(
                "⚠️ CIRCUIT OPEN for %s (threshold exceeded) — blocking for %d seconds. Reason: %s",
                component,
                self.timeout_seconds,
                reason
            )
    
    def get_status(self, component: str = None) -> Dict[str, Any]:
        """Get circuit status for a component or all components."""
        if component:
            if component in self.states:
                return {
                    "component": component,
                    **self.states[component]
                }
            else:
                return {"component": component, "state": "UNKNOWN"}
        else:
            # All components
            return {
                component: {
                    "state": info["state"].value,
                    "failure_count": info["failure_count"],
                    "reason": info.get("reason", "")
                }
                for component, info in self.states.items()
            }
    
    def reset_component(self, component: str) -> bool:
        """Manually reset a component's circuit."""
        if component not in self.states:
            return False
        
        self.states[component] = {
            "state": CircuitState.CLOSED,
            "failure_count": 0,
            "last_failure_time": 0.0,
            "opened_at": 0.0,
            "reason": "",
        }
        log.info("Circuit manually reset for %s", component)
        return True


# Global instance
_breaker: CascadeBreaker = None


def get_breaker() -> CascadeBreaker:
    """Get or create global cascade breaker."""
    global _breaker
    if _breaker is None:
        _breaker = CascadeBreaker(
            failure_threshold=2,
            timeout_seconds=300,  # 5 minutes
        )
    return _breaker


def can_execute_component(component: str) -> bool:
    """Check if component can execute (circuit not open)."""
    return get_breaker().check_can_execute(component)


def record_component_failure(component: str, reason: str = "") -> None:
    """Record failure for a component."""
    get_breaker().record_failure(component, reason)


def record_component_success(component: str) -> None:
    """Record success for a component (used in HALF_OPEN state)."""
    get_breaker().record_success(component)


def get_breaker_status() -> Dict[str, Any]:
    """Get status of all circuits."""
    return get_breaker().get_status()
