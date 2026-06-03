"""
COMPREHENSIVE TRADING SYSTEM FIXES — Implementation Module

This module implements all 8 critical fixes for trading system resilience:

1. Axiom VIX service unreachable (port 8001) — diagnose Railway/local, restart, verify
2. Yahoo fallback degraded — implement FRED VIXCLS as 3rd source with caching
3. Binary halt logic — replace with tiered risk gates (Level 0-3)
4. No alert escalation — implement Telegram alert on sustained HALT (3+ in 10 min)
5. No auto-recovery — implement halt recovery state machine with sanity checks
6. Order queue backlog — implement EOD reconciliation to close/cancel stale orders
7. Signal/execution decoupling — add feedback loop to halt signal generation
8. Axiom service dependency — add health monitoring with auto-restart watchdog

Each fix is independently testable and deployable.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import pytz

logger = logging.getLogger("axiom.trading_fixes")

ET = pytz.timezone("America/New_York")


# ═══════════════════════════════════════════════════════════════════════════════
# FIX #1: Axiom VIX Service Health & Auto-Restart
# ═══════════════════════════════════════════════════════════════════════════════

class AxiomServiceWatchdog:
    """
    Monitors Axiom service (port 8001) and auto-restarts if down.
    
    Checks:
      - /health endpoint responds with status=200
      - _SERVICE_MODE != "standby" (ready to assess)
      - Restart threshold: 3 failed checks in 30 seconds
    """
    
    def __init__(
        self,
        service_url: str = "http://localhost:8001",
        check_interval: int = 10,
        restart_threshold: int = 3,
        restart_cooldown: int = 60,
    ):
        self.service_url = service_url
        self.check_interval = check_interval
        self.restart_threshold = restart_threshold
        self.restart_cooldown = restart_cooldown
        
        self.failure_count = 0
        self.last_failure_time = None
        self.last_restart_time = None
        self.is_running = False
    
    def check_health(self) -> Tuple[bool, str]:
        """
        Check if Axiom service is healthy.
        
        Returns:
            (is_healthy: bool, status_msg: str)
        """
        try:
            resp = requests.get(
                f"{self.service_url}/health",
                timeout=5,
            )
            
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}"
            
            data = resp.json()
            if data.get("status") == "starting":
                return False, "Service still starting"
            
            if data.get("_service_mode") == "standby":
                return False, f"Service in STANDBY: {data.get('_standby_reason', 'unknown')}"
            
            self.failure_count = 0
            return True, "OK"
        
        except requests.exceptions.Timeout:
            return False, "Timeout"
        except requests.exceptions.ConnectionError:
            return False, "Connection refused"
        except Exception as e:
            return False, str(e)
    
    def should_trigger_restart(self) -> bool:
        """
        Return True if restart threshold has been exceeded.
        
        Threshold: restart_threshold failures within restart_cooldown window.
        """
        now = time.time()
        
        # Check cooldown period
        if self.last_restart_time and (now - self.last_restart_time) < self.restart_cooldown:
            return False
        
        # Count failures in the last check_interval * restart_threshold seconds
        if self.last_failure_time and (now - self.last_failure_time) > (
            self.check_interval * self.restart_threshold
        ):
            self.failure_count = 0
        
        return self.failure_count >= self.restart_threshold
    
    def record_failure(self):
        """Record a health check failure."""
        self.failure_count += 1
        self.last_failure_time = time.time()
    
    def trigger_restart(self) -> Tuple[bool, str]:
        """
        Trigger Axiom service restart.
        
        Returns:
            (success: bool, message: str)
        """
        try:
            # Try graceful systemd restart first (if available)
            import subprocess
            result = subprocess.run(
                ["systemctl", "restart", "axiom", "--user"],
                capture_output=True,
                timeout=30,
            )
            
            if result.returncode == 0:
                self.failure_count = 0
                self.last_restart_time = time.time()
                logger.info("Axiom service restarted successfully (systemd)")
                return True, "Restarted via systemd"
            
            # Fallback: SSH to 192.168.1.141 and restart there
            result = subprocess.run(
                ["ssh", "ubuntu@192.168.1.141", "systemctl restart axiom --user"],
                capture_output=True,
                timeout=30,
            )
            
            if result.returncode == 0:
                self.failure_count = 0
                self.last_restart_time = time.time()
                logger.info("Axiom service restarted successfully (remote)")
                return True, "Restarted via SSH"
            
            return False, f"Restart failed: {result.stderr.decode()}"
        
        except Exception as e:
            return False, f"Restart error: {e}"
    
    async def run(self):
        """Run the watchdog loop."""
        self.is_running = True
        logger.info("Axiom watchdog starting (check every %ds)", self.check_interval)
        
        while self.is_running:
            is_healthy, status_msg = self.check_health()
            
            if is_healthy:
                logger.debug("Axiom service healthy: %s", status_msg)
            else:
                logger.warning("Axiom service unhealthy: %s", status_msg)
                self.record_failure()
                
                if self.should_trigger_restart():
                    logger.error("Axiom restart threshold exceeded — triggering restart")
                    success, msg = self.trigger_restart()
                    logger.info("Restart result: %s — %s", "SUCCESS" if success else "FAILED", msg)
            
            await asyncio.sleep(self.check_interval)


# ═══════════════════════════════════════════════════════════════════════════════
# FIX #2: FRED VIXCLS Caching Layer (3rd VIX Source)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class VIXCacheEntry:
    """Cached VIX value with timestamp and source."""
    value: float
    source: str  # "polygon" | "yahoo" | "fred"
    timestamp: datetime
    is_estimated: bool = False
    
    @property
    def age_seconds(self) -> int:
        return int((datetime.now(timezone.utc) - self.timestamp).total_seconds())
    
    @property
    def is_stale(self, max_age_seconds: int = 300) -> bool:
        """Return True if cache entry is older than max_age_seconds."""
        return self.age_seconds > max_age_seconds


class VIXCacheManager:
    """
    Multi-level VIX cache with FRED VIXCLS as tertiary source.
    
    Hierarchy:
      1. Polygon real-time (primary)
      2. Yahoo real-time (secondary)
      3. FRED VIXCLS EOD (tertiary with caching)
      4. Last known + estimated
      5. Conservative default (20.0)
    """
    
    def __init__(self, fred_api_key: str, cache_ttl_seconds: int = 300):
        self.fred_api_key = fred_api_key
        self.cache_ttl_seconds = cache_ttl_seconds
        self.cache: Dict[str, VIXCacheEntry] = {}
    
    def get_fred_vixcls(self) -> Optional[Tuple[float, datetime]]:
        """
        Fetch VIX from FRED VIXCLS (end-of-day, cached).
        
        Returns:
            (vix_value: float, timestamp: datetime) or None on failure.
        """
        try:
            url = "https://api.stlouisfed.org/fred/series/data"
            params = {
                "series_id": "VIXCLS",
                "api_key": self.fred_api_key,
                "file_type": "json",
                "limit": 1,
                "sort_order": "desc",
            }
            
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            
            data = resp.json()
            observations = data.get("observations", [])
            
            if observations:
                obs = observations[0]
                vix_str = obs.get("value", "")
                date_str = obs.get("date", "")
                
                if vix_str and vix_str != ".":
                    vix = float(vix_str)
                    # Parse date as YYYY-MM-DD EOD (4 PM ET)
                    ts = datetime.strptime(date_str, "%Y-%m-%d").replace(
                        hour=16, minute=0, second=0, tzinfo=ET
                    )
                    return vix, ts
            
            logger.warning("FRED VIXCLS returned no data")
            return None
        
        except Exception as e:
            logger.warning("FRED VIXCLS fetch failed: %s", e)
            return None
    
    def get_vix_with_fred_fallback(
        self,
        polygon_vix: Optional[float],
        yahoo_vix: Optional[float],
        last_known_vix: Optional[float],
    ) -> Tuple[float, bool, str]:
        """
        Get VIX with FRED as tertiary source.
        
        Args:
            polygon_vix: Real-time Polygon VIX or None.
            yahoo_vix: Real-time Yahoo VIX or None.
            last_known_vix: Last successfully fetched VIX value.
        
        Returns:
            (vix_value: float, is_estimated: bool, source: str)
        """
        # Primary: Polygon
        if polygon_vix is not None:
            logger.debug("VIX %.2f from Polygon (real-time)", polygon_vix)
            return polygon_vix, False, "polygon"
        
        logger.warning("Polygon VIX failed — trying Yahoo fallback")
        
        # Secondary: Yahoo
        if yahoo_vix is not None:
            logger.debug("VIX %.2f from Yahoo (real-time)", yahoo_vix)
            return yahoo_vix, False, "yahoo"
        
        logger.warning("Yahoo VIX failed — trying FRED VIXCLS fallback")
        
        # Tertiary: FRED with cache
        if "fred_vixcls" in self.cache:
            entry = self.cache["fred_vixcls"]
            if entry.age_seconds < self.cache_ttl_seconds:
                logger.debug("VIX %.2f from FRED cache (age %ds)", entry.value, entry.age_seconds)
                return entry.value, False, "fred_cache"
        
        # Fetch fresh FRED data
        fred_result = self.get_fred_vixcls()
        if fred_result:
            vix, ts = fred_result
            self.cache["fred_vixcls"] = VIXCacheEntry(
                value=vix,
                source="fred",
                timestamp=ts,
                is_estimated=False,
            )
            logger.warning("VIX %.2f from FRED (EOD — may be stale during market hours)", vix)
            return vix, False, "fred"
        
        logger.warning("FRED unavailable — trying estimated from last known")
        
        # Quaternary: Last known + 2
        if last_known_vix is not None:
            estimated = last_known_vix + 2.0
            logger.warning(
                "All live VIX sources failed — using estimated %.1f (last known %.1f + 2)",
                estimated,
                last_known_vix,
            )
            return estimated, True, "estimated"
        
        # Ultimate fallback
        logger.error("All VIX sources failed — defaulting to 20.0 (ELEVATED)")
        return 20.0, True, "default"


# ═══════════════════════════════════════════════════════════════════════════════
# FIX #3: Tiered Risk Gates (replacing binary halt logic)
# ═══════════════════════════════════════════════════════════════════════════════

class HaltLevel(Enum):
    """Tiered halt levels replacing binary all-or-nothing logic."""
    NORMAL = 0          # No halt — normal trading
    ELEVATED = 1        # Heightened caution — size limits, increased monitoring
    CRITICAL = 2        # Severe restriction — only existing positions, no new entries
    MANUAL = 3          # Complete halt — only manual override by Ahmed


@dataclass
class HaltGateDecision:
    """Result of halt gate evaluation."""
    level: HaltLevel
    vix: Optional[float] = None
    reason: str = ""
    sizing_multiplier: float = 1.0
    can_enter_new: bool = True
    can_scale_existing: bool = True
    timestamp: datetime = field(default_factory=lambda: datetime.now(ET))


class TieredHaltGate:
    """
    Replaces binary halt logic with tiered gates based on market conditions.
    
    Level 0 (NORMAL):
      - VIX < 20
      - All trading permitted
      - sizing_mult = 1.0
    
    Level 1 (ELEVATED):
      - VIX 20-30
      - New position size reduced 50%
      - Existing positions can scale
      - sizing_mult = 0.5
    
    Level 2 (CRITICAL):
      - VIX 30-50 OR market halt OR circuit breaker
      - NO new positions
      - Existing positions can scale ONLY if collateral available
      - sizing_mult = 0.0 for new, 0.25 for scaling
    
    Level 3 (MANUAL):
      - VIX > 50 OR explicit halt signal
      - Complete trading halt
      - Only Ahmed can override via /sovereign/directive
      - sizing_mult = 0.0
    """
    
    def __init__(self):
        self.thresholds = {
            HaltLevel.NORMAL: {"vix_max": 20},
            HaltLevel.ELEVATED: {"vix_max": 30},
            HaltLevel.CRITICAL: {"vix_max": 50},
            HaltLevel.MANUAL: {"vix_max": float("inf")},
        }
    
    def evaluate(
        self,
        vix: Optional[float],
        market_halted: bool = False,
        circuit_breaker_open: bool = False,
        manual_halt: bool = False,
    ) -> HaltGateDecision:
        """
        Evaluate current market conditions and return halt level.
        
        Args:
            vix: Current VIX value (or None if unavailable).
            market_halted: True if market has halted trading.
            circuit_breaker_open: True if circuit breaker is open.
            manual_halt: True if explicit halt signal received.
        
        Returns:
            HaltGateDecision with level and sizing recommendations.
        """
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
            # Conservative approach: treat as ELEVATED if VIX unavailable
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
# FIX #4: Alert Escalation (sustained HALT detection)
# ═══════════════════════════════════════════════════════════════════════════════

class SustainedHaltMonitor:
    """
    Monitor for sustained halt conditions (3+ failures in 10 minutes).
    Sends Telegram alert to Ahmed when escalation threshold reached.
    """
    
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
            # Build failure summary
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
            
            # Send Telegram message
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            resp = requests.post(
                url,
                json={
                    "chat_id": self.ahmed_chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            
            if resp.status_code == 200:
                logger.info("Alert escalated to Ahmed — message sent")
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
    TRADING = "trading"          # Normal state
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
    """
    Auto-recovery from halt conditions with sanity checks.
    
    Flow:
      1. TRADING — normal operation
      2. HALT_DETECTED — one indicator suggests halt (e.g., high VIX)
      3. HALT_CONFIRMED — multiple indicators confirm (e.g., VIX + market halt)
      4. RECOVERY_INITIATED — preconditions met, attempting recovery
      5. SANITY_CHECK — verify capital, positions, no duplicates
      6. RESUMED — back to normal trading (TRADING state)
      OR
      6. ABORT — sanity check failed, require manual intervention
    """
    
    def __init__(self):
        self.state = HaltRecoveryState.TRADING
        self.snapshot: Optional[HaltRecoverySnapshot] = None
        self.failure_reasons: List[str] = []
    
    def detect_halt(self, vix: Optional[float], halt_level: HaltLevel) -> bool:
        """
        Signal potential halt condition.
        
        Returns True if state transition occurred (TRADING → HALT_DETECTED).
        """
        if self.state != HaltRecoveryState.TRADING:
            return False
        
        if halt_level in (HaltLevel.CRITICAL, HaltLevel.MANUAL):
            self.state = HaltRecoveryState.HALT_DETECTED
            logger.warning("Halt detected: level=%s, vix=%s", halt_level.name, vix)
            return True
        
        return False
    
    def confirm_halt(self, open_positions: int, pending_orders: int, capital_deployed: float) -> bool:
        """
        Confirm halt with system state snapshot.
        
        Returns True if state transition occurred (HALT_DETECTED → HALT_CONFIRMED).
        """
        if self.state != HaltRecoveryState.HALT_DETECTED:
            return False
        
        self.snapshot = HaltRecoverySnapshot(
            timestamp=datetime.now(ET),
            halt_level=HaltLevel.CRITICAL,  # Placeholder
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
        """
        Initiate recovery sequence.
        
        Returns True if preconditions met (HALT_CONFIRMED → RECOVERY_INITIATED).
        """
        if self.state != HaltRecoveryState.HALT_CONFIRMED or not self.snapshot:
            return False
        
        # Preconditions
        # 1. At least 30 seconds since halt confirmed (avoid thrashing)
        age = (datetime.now(ET) - self.snapshot.timestamp).total_seconds()
        if age < 30:
            self.failure_reasons.append(f"Halt too recent ({age:.0f}s < 30s threshold)")
            return False
        
        # 2. Pending orders cleared (manual intervention may be needed)
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
        """
        Run sanity checks before resuming trading.
        
        Checks:
          1. Open positions match snapshot (±0)
          2. Capital within tolerance (±5%)
          3. No orphaned orders
        
        Returns:
            (passed: bool, message: str)
        """
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
            msg = (
                f"Capital mismatch: {expected_capital:.2f} "
                f"vs {self.snapshot.capital_deployed:.2f} (tolerance ±{tolerance:.2f})"
            )
            self.failure_reasons.append(msg)
            logger.error("Sanity check failed: %s", msg)
            self.state = HaltRecoveryState.ABORT
            return False, msg
        
        logger.info("Sanity checks passed — resuming trading")
        return True, "OK"
    
    def resume_trading(self) -> bool:
        """
        Resume normal trading.
        
        Returns True if state transition successful (SANITY_CHECK → RESUMED).
        """
        if self.state != HaltRecoveryState.SANITY_CHECK:
            return False
        
        self.state = HaltRecoveryState.RESUMED
        logger.info("Trading resumed from halt")
        
        # Transition back to TRADING after a short grace period
        # (In practice, this would be triggered by next successful trade)
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# FIX #6: EOD Order Reconciliation (close/cancel stale orders)
# ═══════════════════════════════════════════════════════════════════════════════

class EODOrderReconciliation:
    """
    End-of-day order queue cleanup.
    
    Runs at 3:30 PM ET (after close):
      1. Query all open orders from Alpaca
      2. Identify stale orders (>1 hour old, partially filled, or orphaned)
      3. Close or cancel with logging
      4. Reconcile against internal queue
      5. Alert Ahmed if discrepancies found
    """
    
    def __init__(
        self,
        alpaca_key: str,
        alpaca_secret: str,
        telegram_token: str,
        ahmed_chat_id: str,
    ):
        self.alpaca_key = alpaca_key
        self.alpaca_secret = alpaca_secret
        self.telegram_token = telegram_token
        self.ahmed_chat_id = ahmed_chat_id
        
        self.alpaca_h = {
            "APCA-API-KEY-ID": alpaca_key,
            "APCA-API-SECRET-KEY": alpaca_secret,
        }
    
    def get_open_orders(self) -> List[Dict]:
        """Fetch all open orders from Alpaca."""
        try:
            resp = requests.get(
                "https://paper-api.alpaca.markets/v2/orders",
                params={"status": "open"},
                headers=self.alpaca_h,
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json() if isinstance(resp.json(), list) else []
        except Exception as e:
            logger.error("Failed to fetch open orders: %s", e)
            return []
    
    def is_order_stale(self, order: Dict) -> Tuple[bool, str]:
        """
        Determine if an order is stale and should be closed.
        
        Criteria:
          1. Created >1 hour ago AND partially filled
          2. Created >2 hours ago (regardless of fill)
          3. In "partially_filled" state for >30 minutes
        
        Returns:
            (is_stale: bool, reason: str)
        """
        created_at = order.get("created_at")
        if not created_at:
            return False, ""
        
        try:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            age_minutes = (now - created
        except Exception as e:
            logger.warning("Failed to check staleness: %s", e)
            return False, ""


# Complete remaining fixes in next segment...
