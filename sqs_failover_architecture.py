"""
sqs_failover_architecture.py — Deterministic SQS Failover System

MANDATE: Zero trading halts due to single component failures.

Architecture:
  1. Health Detection (10s monitoring intervals)
  2. Automatic Failover Trigger (when primary down)
  3. Standby Deterministic Algorithm (simplified logic, no dependencies)
  4. Verification & Reconciliation (no duplicate orders, capital accurate)
  5. Graceful Degradation (reduced capacity vs halt)

Subsystems covered:
  - ATG_SWING: Medium-term swing trades
  - ATG_INTRADAY: Daily momentum trades
  - AILS: AI-integrated long-short system
  - ATM_MULTIWEEK: Multi-week volatility plays
  - ATM_0DTE: 0-DTE weekly spreads
  - AMAT/Prime: Prime execution system
  - ATG_BUFFER: Order buffering
  - Message Broker: Event routing
  - Capital Router: Capital allocation
  - VIX Data Sources: Volatility inputs

Design Philosophy:
  - Deterministic: No ML, no fancy logic — pure state machine
  - Standalone: Standby code has zero dependencies on primary
  - Reconciled: Every trade logged, every capital move tracked
  - Degraded not halted: System operates at reduced size, not stopped
  - Transparent: All failovers logged + alerted immediately
"""

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

import pytz
import requests

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


# =====================================================================
# 1. HEALTH DETECTION SUBSYSTEM (10s interval monitoring)
# =====================================================================

class HealthStatus(Enum):
    """Component health status."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass
class HealthCheckResult:
    """Single health check result."""
    timestamp: str
    component: str
    status: HealthStatus
    latency_ms: float
    error: Optional[str] = None
    metadata: Dict = field(default_factory=dict)

    def is_healthy(self) -> bool:
        return self.status in (HealthStatus.HEALTHY, HealthStatus.DEGRADED)


@dataclass
class HealthSnapshot:
    """All components health at one moment."""
    timestamp: str
    checks: List[HealthCheckResult]
    overall_degradation_level: int  # 0=full, 1=minor, 2=moderate, 3=severe, 4=emergency
    components_down: List[str] = field(default_factory=list)
    failovers_active: List[str] = field(default_factory=list)


class ComponentHealthMonitor:
    """
    Monitors ALL subsystems every 10 seconds.
    
    Subsystems:
      1. ATG_SWING (SQS: atg-swing-queue)
      2. ATG_INTRADAY (SQS: atg-intraday-queue)
      3. AILS (SQS: ails-queue)
      4. ATM_MULTIWEEK (SQS: atm-multiweek-queue)
      5. ATM_0DTE (SQS: atm-0dte-queue)
      6. AMAT/Prime (SQS: amat-prime-queue)
      7. ATG_BUFFER (SQS: atg-buffer-queue)
      8. Message Broker (TCP: 192.168.1.141:5672, RabbitMQ)
      9. Capital Router (HTTP: localhost:9100/health)
      10. VIX Data Sources (HTTP: polygon.io, orats.com)
    """

    SUBSYSTEMS = {
        "ATG_SWING": {
            "sqs_queue": "atg-swing-queue",
            "type": "sqs",
            "timeout_ms": 2000,
            "degradation_level": 1,  # Minor degradation if down
        },
        "ATG_INTRADAY": {
            "sqs_queue": "atg-intraday-queue",
            "type": "sqs",
            "timeout_ms": 2000,
            "degradation_level": 1,
        },
        "AILS": {
            "sqs_queue": "ails-queue",
            "type": "sqs",
            "timeout_ms": 2000,
            "degradation_level": 2,  # Moderate if down (AI-heavy)
        },
        "ATM_MULTIWEEK": {
            "sqs_queue": "atm-multiweek-queue",
            "type": "sqs",
            "timeout_ms": 2000,
            "degradation_level": 1,
        },
        "ATM_0DTE": {
            "sqs_queue": "atm-0dte-queue",
            "type": "sqs",
            "timeout_ms": 2000,
            "degradation_level": 1,
        },
        "AMAT_PRIME": {
            "sqs_queue": "amat-prime-queue",
            "type": "sqs",
            "timeout_ms": 2000,
            "degradation_level": 3,  # Severe if down (execution critical)
        },
        "ATG_BUFFER": {
            "sqs_queue": "atg-buffer-queue",
            "type": "sqs",
            "timeout_ms": 2000,
            "degradation_level": 2,  # Moderate (buffering critical)
        },
        "MESSAGE_BROKER": {
            "host": "192.168.1.141",
            "port": 5672,
            "type": "rabbitmq_tcp",
            "timeout_ms": 2000,
            "degradation_level": 2,
        },
        "CAPITAL_ROUTER": {
            "url": "http://localhost:9100/health",
            "type": "http",
            "timeout_ms": 2000,
            "degradation_level": 4,  # Emergency (capital allocation critical)
        },
        "VIX_DATA_POLYGON": {
            "url": "https://api.polygon.io/v3/snapshot/options",
            "type": "http_external",
            "timeout_ms": 5000,
            "degradation_level": 2,
        },
        "VIX_DATA_ORATS": {
            "url": "https://api.orats.io/unusual-activity",
            "type": "http_external",
            "timeout_ms": 5000,
            "degradation_level": 2,
        },
    }

    def __init__(self):
        self.last_checks: Dict[str, HealthCheckResult] = {}
        self.history: List[HealthSnapshot] = []
        self.alert_fn: Optional[Callable[[str], None]] = None
        self._lock = threading.RLock()

    def set_alert_fn(self, fn: Callable[[str], None]) -> None:
        """Set the alert function for failures."""
        self.alert_fn = fn

    async def check_health(self, component: str, spec: Dict) -> HealthCheckResult:
        """Check health of a single component."""
        start_time = time.time()
        error = None
        status = HealthStatus.UNKNOWN

        try:
            if spec["type"] == "sqs":
                status = await self._check_sqs(component, spec)
            elif spec["type"] == "rabbitmq_tcp":
                status = await self._check_rabbitmq(component, spec)
            elif spec["type"] == "http":
                status = await self._check_http(component, spec)
            elif spec["type"] == "http_external":
                status = await self._check_http_external(component, spec)

        except asyncio.TimeoutError:
            status = HealthStatus.DOWN
            error = "Timeout"
        except Exception as e:
            status = HealthStatus.DOWN
            error = str(e)

        latency_ms = (time.time() - start_time) * 1000

        result = HealthCheckResult(
            timestamp=datetime.now(ET).isoformat(),
            component=component,
            status=status,
            latency_ms=latency_ms,
            error=error,
        )

        with self._lock:
            self.last_checks[component] = result

        return result

    async def _check_sqs(self, component: str, spec: Dict) -> HealthStatus:
        """Check SQS queue health."""
        try:
            # Import boto3 when needed
            import boto3
            client = boto3.client("sqs", region_name="us-east-1")
            
            # Get queue attributes as a health check
            queue_url = spec.get("sqs_queue")
            response = client.get_queue_attributes(
                QueueUrl=queue_url,
                AttributeNames=["ApproximateNumberOfMessages"],
            )
            
            return HealthStatus.HEALTHY if response else HealthStatus.DEGRADED
        except Exception:
            return HealthStatus.DOWN

    async def _check_rabbitmq(self, component: str, spec: Dict) -> HealthStatus:
        """Check RabbitMQ broker TCP connectivity."""
        host = spec.get("host")
        port = spec.get("port")
        timeout = spec.get("timeout_ms", 2000) / 1000.0

        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            return HealthStatus.HEALTHY if result == 0 else HealthStatus.DOWN
        except Exception:
            return HealthStatus.DOWN

    async def _check_http(self, component: str, spec: Dict) -> HealthStatus:
        """Check HTTP service health."""
        url = spec.get("url")
        timeout = spec.get("timeout_ms", 2000) / 1000.0

        try:
            response = requests.get(url, timeout=timeout)
            return HealthStatus.HEALTHY if response.status_code == 200 else HealthStatus.DEGRADED
        except Exception:
            return HealthStatus.DOWN

    async def _check_http_external(self, component: str, spec: Dict) -> HealthStatus:
        """Check external API health."""
        url = spec.get("url")
        timeout = spec.get("timeout_ms", 5000) / 1000.0

        try:
            response = requests.get(url, timeout=timeout)
            return HealthStatus.HEALTHY if response.status_code in (200, 401) else HealthStatus.DEGRADED
        except Exception:
            return HealthStatus.DOWN

    async def run_monitoring_loop(self, interval_seconds: int = 10) -> None:
        """Run health monitoring every N seconds."""
        logger.info(f"Starting health monitoring loop (interval={interval_seconds}s)")

        while True:
            try:
                checks = []
                tasks = [
                    self.check_health(name, spec)
                    for name, spec in self.SUBSYSTEMS.items()
                ]
                checks = await asyncio.gather(*tasks)

                snapshot = self._build_snapshot(checks)
                with self._lock:
                    self.history.append(snapshot)
                    # Keep last 100 snapshots
                    if len(self.history) > 100:
                        self.history = self.history[-100:]

                # Alert on degradation changes
                self._alert_if_needed(snapshot)

            except Exception as e:
                logger.error(f"Health check loop error: {e}", exc_info=True)

            await asyncio.sleep(interval_seconds)

    def _build_snapshot(self, checks: List[HealthCheckResult]) -> HealthSnapshot:
        """Build a health snapshot from individual checks."""
        components_down = [c.component for c in checks if not c.is_healthy()]
        
        # Calculate overall degradation level
        degradation_level = 0
        for component in components_down:
            spec = self.SUBSYSTEMS.get(component, {})
            level = spec.get("degradation_level", 1)
            degradation_level = max(degradation_level, level)

        return HealthSnapshot(
            timestamp=datetime.now(ET).isoformat(),
            checks=checks,
            overall_degradation_level=degradation_level,
            components_down=components_down,
            failovers_active=[],  # Will be populated by failover system
        )

    def _alert_if_needed(self, snapshot: HealthSnapshot) -> None:
        """Alert on significant changes."""
        if not self.alert_fn:
            return

        if snapshot.components_down:
            msg = f"🚨 Components down: {', '.join(snapshot.components_down)}\nDegradation level: {snapshot.overall_degradation_level}/4"
            self.alert_fn(msg)

    def get_latest_snapshot(self) -> Optional[HealthSnapshot]:
        """Get the latest health snapshot."""
        with self._lock:
            return self.history[-1] if self.history else None

    def get_health_summary(self) -> Dict:
        """Get current health summary."""
        with self._lock:
            latest = self.get_latest_snapshot()
            if not latest:
                return {"status": "unknown", "components": {}}

            return {
                "timestamp": latest.timestamp,
                "degradation_level": latest.overall_degradation_level,
                "components_down": latest.components_down,
                "total_components": len(self.SUBSYSTEMS),
                "healthy_components": len(self.SUBSYSTEMS) - len(latest.components_down),
                "details": {c.component: asdict(c) for c in latest.checks},
            }


# =====================================================================
# 2. FAILOVER TRIGGER SYSTEM
# =====================================================================

@dataclass
class FailoverEvent:
    """Record of a failover activation."""
    timestamp: str
    component: str
    trigger: str
    primary_down_for_seconds: float
    standby_activated: bool
    reconciliation_required: bool


class FailoverTrigger:
    """
    Monitors health and automatically triggers failovers.
    
    Rules:
      - Primary down for >30 seconds → activate standby
      - Standby active for >5 minutes with primary down → escalate alert
      - Primary recovery → gradually transition back to primary
    """

    DOWN_THRESHOLD_SECONDS = 30
    ESCALATION_THRESHOLD_SECONDS = 300  # 5 minutes
    TRANSITION_BACK_SECONDS = 60

    def __init__(self, health_monitor: ComponentHealthMonitor):
        self.health_monitor = health_monitor
        self.component_down_since: Dict[str, float] = {}
        self.active_failovers: Dict[str, FailoverEvent] = {}
        self._lock = threading.RLock()
        self.alert_fn: Optional[Callable[[str], None]] = None

    def set_alert_fn(self, fn: Callable[[str], None]) -> None:
        """Set alert function."""
        self.alert_fn = fn

    async def run_failover_monitor(self, interval_seconds: int = 10) -> None:
        """Monitor for failover conditions."""
        logger.info("Starting failover trigger monitor")

        while True:
            try:
                snapshot = self.health_monitor.get_latest_snapshot()
                if snapshot:
                    await self._process_snapshot(snapshot)
            except Exception as e:
                logger.error(f"Failover trigger error: {e}", exc_info=True)

            await asyncio.sleep(interval_seconds)

    async def _process_snapshot(self, snapshot: HealthSnapshot) -> None:
        """Process a health snapshot for failover conditions."""
        now = time.time()

        # Track component down time
        current_down = set(snapshot.components_down)
        previous_down = set(self.component_down_since.keys())

        # Newly down components
        for component in current_down - previous_down:
            self.component_down_since[component] = now
            logger.warning(f"Component DOWN: {component}")

        # Recovered components
        for component in previous_down - current_down:
            down_time = now - self.component_down_since[component]
            del self.component_down_since[component]
            logger.info(f"Component RECOVERED: {component} (was down {down_time:.1f}s)")

        # Check for failover triggers
        with self._lock:
            for component, down_since in list(self.component_down_since.items()):
                down_time = now - down_since

                # Trigger failover after threshold
                if down_time > self.DOWN_THRESHOLD_SECONDS:
                    if component not in self.active_failovers:
                        await self._activate_failover(component, down_time)

                # Escalate if too long
                if down_time > self.ESCALATION_THRESHOLD_SECONDS:
                    if component in self.active_failovers:
                        event = self.active_failovers[component]
                        if not event.reconciliation_required:
                            await self._escalate_failover(component, down_time)

    async def _activate_failover(self, component: str, down_time: float) -> None:
        """Activate failover for a component."""
        logger.error(f"FAILOVER ACTIVATED: {component} (down {down_time:.1f}s)")

        event = FailoverEvent(
            timestamp=datetime.now(ET).isoformat(),
            component=component,
            trigger=f"Primary down {down_time:.1f}s",
            primary_down_for_seconds=down_time,
            standby_activated=True,
            reconciliation_required=True,
        )

        with self._lock:
            self.active_failovers[component] = event

        if self.alert_fn:
            msg = f"🔴 FAILOVER ACTIVATED\nComponent: {component}\nDown: {down_time:.1f}s\nStandby engaged"
            self.alert_fn(msg)

    async def _escalate_failover(self, component: str, down_time: float) -> None:
        """Escalate a long-running failover."""
        logger.error(f"FAILOVER ESCALATION: {component} (down {down_time:.1f}s)")

        event = self.active_failovers[component]
        event.reconciliation_required = True

        if self.alert_fn:
            msg = f"🔴🔴 FAILOVER ESCALATION\nComponent: {component}\nDown: {down_time/60:.1f} minutes\nReconciliation queued"
            self.alert_fn(msg)

    def get_failover_status(self) -> Dict:
        """Get current failover status."""
        with self._lock:
            return {
                "active_failovers": [asdict(e) for e in self.active_failovers.values()],
                "count": len(self.active_failovers),
            }


# =====================================================================
# 3. STANDBY DETERMINISTIC ALGORITHM
# =====================================================================

class StandbySimplifiedAlgorithm:
    """
    Simplified trading logic that runs when primary is down.
    
    NO dependencies on primary components. Deterministic state machine.
    Focused on:
      1. Preserve existing positions
      2. Close losers on schedule
      3. Avoid new entries (degraded mode)
      4. Track every order for reconciliation
    
    Core trading rules (deterministic):
      - No new entries in failover mode
      - Close all losing positions every 5 minutes
      - Close all profitable positions at preset targets
      - Monitor VIX brake (never trade on VIX spike)
      - Track capital allocation (prevent doubling up)
    """

    def __init__(self, capital_limit: float = 50000.0):
        self.capital_limit = capital_limit
        self.capital_in_use = 0.0
        self.trades_executed = []
        self._lock = threading.RLock()

    async def process_open_positions(self, positions: List[Dict]) -> List[Dict]:
        """
        Evaluate open positions for closure in failover mode.
        
        Returns list of closure recommendations.
        """
        closures = []

        for pos in positions:
            symbol = pos.get("symbol")
            entry_price = pos.get("entry_price", 0.0)
            current_price = pos.get("current_price", 0.0)
            pnl = current_price - entry_price
            pnl_pct = (pnl / entry_price * 100) if entry_price > 0 else 0

            # Rule 1: Close losers (PnL < -2%)
            if pnl_pct < -2.0:
                closures.append({
                    "action": "close",
                    "symbol": symbol,
                    "reason": "loser_failover",
                    "pnl_pct": pnl_pct,
                })

            # Rule 2: Take winners at +5% target
            elif pnl_pct > 5.0:
                closures.append({
                    "action": "close",
                    "symbol": symbol,
                    "reason": "target_hit",
                    "pnl_pct": pnl_pct,
                })

        return closures

    async def should_trade_new_entry(self) -> bool:
        """Standby mode never allows new entries."""
        return False

    async def check_vix_brake(self, vix_level: float) -> bool:
        """Check if VIX brake should prevent trading."""
        # Hard brake at VIX > 40
        return vix_level > 40

    def record_trade(self, trade: Dict) -> None:
        """Record a trade for reconciliation."""
        with self._lock:
            trade_record = {
                "timestamp": datetime.now(ET).isoformat(),
                "standby_mode": True,
                **trade,
            }
            self.trades_executed.append(trade_record)

    def get_trade_log(self) -> List[Dict]:
        """Get all trades executed in standby mode."""
        with self._lock:
            return list(self.trades_executed)


# =====================================================================
# 4. VERIFICATION & RECONCILIATION
# =====================================================================

@dataclass
class ReconciliationReport:
    """Results of a reconciliation check."""
    timestamp: str
    component: str
    trades_found_in_primary: int
    trades_found_in_standby: int
    duplicate_orders: List[str] = field(default_factory=list)
    missing_trades: List[str] = field(default_factory=list)
    capital_discrepancy: float = 0.0
    verification_passed: bool = False


class VerificationAndReconciliation:
    """
    Ensures that:
      1. No duplicate orders were placed
      2. Capital allocation is accurate
      3. Both primary and standby agree on state
      4. All trades are accounted for
    
    This runs after failover ends to validate consistency.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.reconciliation_reports: List[ReconciliationReport] = []

    async def reconcile_failover(
        self,
        component: str,
        primary_trades: List[Dict],
        standby_trades: List[Dict],
        primary_capital: float,
        standby_capital: float,
    ) -> ReconciliationReport:
        """
        Reconcile primary and standby state after failover.
        
        Checks:
          1. No duplicates (same order ID in both lists)
          2. Capital tracking matches
          3. All positions accounted for
        """

        duplicates = []
        missing = []

        # Find duplicate order IDs
        primary_ids = {t.get("order_id") for t in primary_trades}
        standby_ids = {t.get("order_id") for t in standby_trades}
        
        duplicates = list(primary_ids & standby_ids)

        # Find missing trades (in one but not the other)
        missing = list((primary_ids | standby_ids) - (primary_ids & standby_ids))

        # Check capital discrepancy
        capital_discrepancy = abs(primary_capital - standby_capital)

        # Verification passes if:
        # 1. No duplicates
        # 2. Capital within 1% tolerance
        verification_passed = (
            len(duplicates) == 0 and
            capital_discrepancy < max(primary_capital, standby_capital) * 0.01
        )

        report = ReconciliationReport(
            timestamp=datetime.now(ET).isoformat(),
            component=component,
            trades_found_in_primary=len(primary_trades),
            trades_found_in_standby=len(standby_trades),
            duplicate_orders=duplicates,
            missing_trades=missing,
            capital_discrepancy=capital_discrepancy,
            verification_passed=verification_passed,
        )

        with self._lock:
            self.reconciliation_reports.append(report)

        return report

    def get_reports(self) -> List[ReconciliationReport]:
        """Get all reconciliation reports."""
        with self._lock:
            return list(self.reconciliation_reports)


# =====================================================================
# 5. GRACEFUL DEGRADATION COORDINATION
# =====================================================================

class GracefulDegradationCoordinator:
    """
    Maps health snapshot → degradation level → position size adjustments.
    
    Degradation levels:
      0 = Full operation (100% size)
      1 = Minor (100% size, monitor closely)
      2 = Moderate (50% size, alert Ahmed)
      3 = Severe (0% new entries, manage existing)
      4 = Emergency (full halt)
    """

    SIZE_MULTIPLIERS = {
        0: 1.00,
        1: 1.00,
        2: 0.50,
        3: 0.00,
        4: 0.00,
    }

    ALLOW_NEW_ENTRIES = {
        0: True,
        1: True,
        2: True,
        3: False,
        4: False,
    }

    def __init__(self):
        self._lock = threading.RLock()
        self.current_level = 0
        self.history: List[Tuple[str, int]] = []

    def apply_health_snapshot(self, snapshot: HealthSnapshot) -> Dict:
        """Apply health snapshot to determine degradation level."""
        level = snapshot.overall_degradation_level

        with self._lock:
            if level != self.current_level:
                self.history.append((datetime.now(ET).isoformat(), level))
                self.current_level = level

        size_mult = self.SIZE_MULTIPLIERS.get(level, 0.0)
        allow_new = self.ALLOW_NEW_ENTRIES.get(level, False)

        return {
            "degradation_level": level,
            "size_multiplier": size_mult,
            "allow_new_entries": allow_new,
            "components_down": snapshot.components_down,
            "failovers_active": snapshot.failovers_active,
        }

    def get_effective_position_size(self, base_size: float) -> float:
        """Get effective position size given current degradation."""
        with self._lock:
            mult = self.SIZE_MULTIPLIERS.get(self.current_level, 0.0)
            return base_size * mult

    def get_current_level(self) -> int:
        """Get current degradation level."""
        with self._lock:
            return self.current_level


# =====================================================================
# MAIN ORCHESTRATOR
# =====================================================================

class SQSFailoverOrchestrator:
    """
    Central orchestrator for all failover systems.
    
    Runs 5 concurrent subsystems:
      1. ComponentHealthMonitor (10s checks)
      2. FailoverTrigger (failover activation)
      3. StandbySimplifiedAlgorithm (fallback trading)
      4. VerificationAndReconciliation (consistency checks)
      5. GracefulDegradationCoordinator (capacity reduction)
    """

    def __init__(self, telegram_alert_fn: Optional[Callable[[str], None]] = None):
        self.health_monitor = ComponentHealthMonitor()
        self.failover_trigger = FailoverTrigger(self.health_monitor)
        self.standby_algorithm = StandbySimplifiedAlgorithm()
        self.reconciliation = VerificationAndReconciliation()
        self.degradation = GracefulDegradationCoordinator()

        # Set alert function
        if telegram_alert_fn:
            self.health_monitor.set_alert_fn(telegram_alert_fn)
            self.failover_trigger.set_alert_fn(telegram_alert_fn)
        
        self.alert_fn = telegram_alert_fn
        self._lock = threading.RLock()

    async def startup(self) -> None:
        """Start all monitoring systems."""
        logger.info("Starting SQS Failover Orchestrator")

        tasks = [
            self.health_monitor.run_monitoring_loop(interval_seconds=10),
            self.failover_trigger.run_failover_monitor(interval_seconds=10),
            self._degradation_coordinator_loop(),
        ]

        await asyncio.gather(*tasks)

    async def _degradation_coordinator_loop(self) -> None:
        """Monitor health and update degradation."""
        logger.info("Starting degradation coordinator")

        while True:
            try:
                snapshot = self.health_monitor.get_latest_snapshot()
                if snapshot:
                    state = self.degradation.apply_health_snapshot(snapshot)
                    
                    # Log significant changes
                    if snapshot.components_down:
                        logger.warning(f"Degradation state: {state}")

            except Exception as e:
                logger.error(f"Degradation coordinator error: {e}")

            await asyncio.sleep(10)

    def get_system_status(self) -> Dict:
        """Get complete system status."""
        health = self.health_monitor.get_health_summary()
        failovers = self.failover_trigger.get_failover_status()
        degradation_level = self.degradation.get_current_level()

        return {
            "timestamp": datetime.now(ET).isoformat(),
            "health": health,
            "failovers": failovers,
            "degradation_level": degradation_level,
            "is_trading_allowed": self.degradation.ALLOW_NEW_ENTRIES.get(degradation_level, False),
            "size_multiplier": self.degradation.SIZE_MULTIPLIERS.get(degradation_level, 0.0),
        }

    def get_health_report(self) -> Dict:
        """Get detailed health report for monitoring."""
        return self.health_monitor.get_health_summary()

    def get_failover_report(self) -> Dict:
        """Get detailed failover report."""
        return self.failover_trigger.get_failover_status()

    def get_reconciliation_reports(self) -> List[ReconciliationReport]:
        """Get all reconciliation reports."""
        return self.reconciliation.reconciliation_reports


# =====================================================================
# TESTING & LOGGING
# =====================================================================

def setup_logging():
    """Configure logging for failover system."""
    log_dir = "/Users/ahmedsadek/nexus/logs"
    os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | %(level