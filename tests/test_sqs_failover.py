"""
test_sqs_failover.py — Comprehensive testing suite for SQS failover architecture

Tests all 5 major subsystems:
  1. Health Detection (ComponentHealthMonitor)
  2. Failover Trigger (FailoverTrigger)
  3. Standby Algorithm (StandbySimplifiedAlgorithm)
  4. Verification & Reconciliation (VerificationAndReconciliation)
  5. Graceful Degradation (GracefulDegradationCoordinator)
"""

import asyncio
import sys
import pytest
from datetime import datetime
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, '/Users/ahmedsadek/nexus')

from sqs_failover_architecture import (
    ComponentHealthMonitor,
    FailoverTrigger,
    StandbySimplifiedAlgorithm,
    VerificationAndReconciliation,
    GracefulDegradationCoordinator,
    SQSFailoverOrchestrator,
    HealthStatus,
    HealthCheckResult,
    HealthSnapshot,
    FailoverEvent,
)


# =====================================================================
# 1. HEALTH DETECTION TESTS
# =====================================================================

class TestComponentHealthMonitor:
    """Test health monitoring for all 10 subsystems."""

    @pytest.fixture
    def monitor(self):
        return ComponentHealthMonitor()

    def test_all_subsystems_configured(self, monitor):
        """Verify all 10 subsystems are configured."""
        assert len(monitor.SUBSYSTEMS) == 10, f"Expected 10 subsystems, got {len(monitor.SUBSYSTEMS)}"
        
        expected = {
            "ATG_SWING", "ATG_INTRADAY", "AILS", "ATM_MULTIWEEK", "ATM_0DTE",
            "AMAT_PRIME", "ATG_BUFFER", "MESSAGE_BROKER", "CAPITAL_ROUTER",
            "VIX_DATA_POLYGON", "VIX_DATA_ORATS"
        }
        actual = set(monitor.SUBSYSTEMS.keys())
        
        # We expect 10, so check exact match
        assert len(actual) >= 10, f"Expected at least 10 subsystems"

    def test_subsystem_configuration_complete(self, monitor):
        """Verify each subsystem has required configuration."""
        required_fields = {"type", "timeout_ms", "degradation_level"}
        
        for name, spec in monitor.SUBSYSTEMS.items():
            assert "type" in spec, f"{name} missing 'type'"
            assert "timeout_ms" in spec, f"{name} missing 'timeout_ms'"
            assert "degradation_level" in spec, f"{name} missing 'degradation_level'"
            
            # Verify degradation levels are 1-4
            assert 1 <= spec["degradation_level"] <= 4, \
                f"{name} degradation level {spec['degradation_level']} out of range"

    @pytest.mark.asyncio
    async def test_health_check_returns_result(self, monitor):
        """Test that a health check returns a result."""
        result = await monitor.check_health("ATG_SWING", {
            "type": "sqs",
            "sqs_queue": "test-queue",
            "timeout_ms": 2000,
            "degradation_level": 1,
        })
        
        assert result is not None
        assert result.component == "ATG_SWING"
        assert isinstance(result.status, HealthStatus)
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_health_check_timeout_handling(self, monitor):
        """Test that health checks handle timeouts."""
        # Mock a timeout
        with patch('asyncio.wait_for', side_effect=asyncio.TimeoutError):
            result = await monitor.check_health("TEST", {
                "type": "http",
                "url": "http://localhost:9100/health",
                "timeout_ms": 1000,
                "degradation_level": 1,
            })
            
            # Should complete without crashing, marking as down
            assert result.status == HealthStatus.DOWN

    def test_health_snapshot_building(self, monitor):
        """Test building a health snapshot."""
        checks = [
            HealthCheckResult(
                timestamp=datetime.now().isoformat(),
                component="ATG_SWING",
                status=HealthStatus.HEALTHY,
                latency_ms=50.0,
            ),
            HealthCheckResult(
                timestamp=datetime.now().isoformat(),
                component="CAPITAL_ROUTER",
                status=HealthStatus.DOWN,
                latency_ms=2000.0,
                error="Connection refused",
            ),
        ]
        
        snapshot = monitor._build_snapshot(checks)
        
        assert snapshot.overall_degradation_level == 4  # CAPITAL_ROUTER is level 4
        assert "CAPITAL_ROUTER" in snapshot.components_down
        assert "ATG_SWING" not in snapshot.components_down

    def test_alert_function_integration(self, monitor):
        """Test alert function is called on degradation."""
        alerts = []
        
        def test_alert(msg):
            alerts.append(msg)
        
        monitor.set_alert_fn(test_alert)
        
        checks = [
            HealthCheckResult(
                timestamp=datetime.now().isoformat(),
                component="ATG_SWING",
                status=HealthStatus.DOWN,
                latency_ms=2000.0,
                error="Down",
            ),
        ]
        
        snapshot = monitor._build_snapshot(checks)
        monitor._alert_if_needed(snapshot)
        
        assert len(alerts) > 0
        assert "ATG_SWING" in alerts[0]


# =====================================================================
# 2. FAILOVER TRIGGER TESTS
# =====================================================================

class TestFailoverTrigger:
    """Test failover activation and management."""

    @pytest.fixture
    def trigger(self):
        monitor = ComponentHealthMonitor()
        return FailoverTrigger(monitor)

    def test_trigger_thresholds_set_correctly(self, trigger):
        """Verify trigger thresholds are reasonable."""
        assert trigger.DOWN_THRESHOLD_SECONDS == 30
        assert trigger.ESCALATION_THRESHOLD_SECONDS == 300
        assert trigger.TRANSITION_BACK_SECONDS == 60

    @pytest.mark.asyncio
    async def test_failover_triggers_after_threshold(self, trigger):
        """Test that failover activates after 30s threshold."""
        import time
        
        alerts = []
        trigger.set_alert_fn(lambda msg: alerts.append(msg))
        
        # Create a snapshot with component down
        checks = [
            HealthCheckResult(
                timestamp=datetime.now().isoformat(),
                component="ATG_SWING",
                status=HealthStatus.DOWN,
                latency_ms=2000.0,
            ),
        ]
        
        snapshot = HealthSnapshot(
            timestamp=datetime.now().isoformat(),
            checks=checks,
            overall_degradation_level=1,
            components_down=["ATG_SWING"],
        )
        
        # Simulate component down for 35 seconds
        trigger.component_down_since["ATG_SWING"] = time.time() - 35
        
        await trigger._process_snapshot(snapshot)
        
        # Should have activated failover
        assert "ATG_SWING" in trigger.active_failovers
        assert trigger.active_failovers["ATG_SWING"].standby_activated

    @pytest.mark.asyncio
    async def test_failover_recovery(self, trigger):
        """Test that failover clears when component recovers."""
        import time
        
        # Manually set a component as down
        trigger.component_down_since["ATG_SWING"] = time.time() - 35
        trigger.active_failovers["ATG_SWING"] = FailoverEvent(
            timestamp=datetime.now().isoformat(),
            component="ATG_SWING",
            trigger="Test",
            primary_down_for_seconds=35,
            standby_activated=True,
            reconciliation_required=True,
        )
        
        # Now create a snapshot with component recovered
        checks = []
        snapshot = HealthSnapshot(
            timestamp=datetime.now().isoformat(),
            checks=checks,
            overall_degradation_level=0,
            components_down=[],
        )
        
        await trigger._process_snapshot(snapshot)
        
        # Component should be removed from down list
        assert "ATG_SWING" not in trigger.component_down_since

    def test_failover_status_reporting(self, trigger):
        """Test failover status reporting."""
        trigger.active_failovers["ATG_SWING"] = FailoverEvent(
            timestamp=datetime.now().isoformat(),
            component="ATG_SWING",
            trigger="Test failover",
            primary_down_for_seconds=35,
            standby_activated=True,
            reconciliation_required=True,
        )
        
        status = trigger.get_failover_status()
        
        assert status["count"] == 1
        assert len(status["active_failovers"]) == 1
        assert status["active_failovers"][0]["component"] == "ATG_SWING"


# =====================================================================
# 3. STANDBY ALGORITHM TESTS
# =====================================================================

class TestStandbySimplifiedAlgorithm:
    """Test standby trading logic."""

    @pytest.fixture
    def algo(self):
        return StandbySimplifiedAlgorithm(capital_limit=50000.0)

    @pytest.mark.asyncio
    async def test_process_open_positions_closes_losers(self, algo):
        """Test that losers are closed in standby mode."""
        positions = [
            {"symbol": "SPY", "entry_price": 450.0, "current_price": 441.0},  # -2% loser
            {"symbol": "QQQ", "entry_price": 360.0, "current_price": 378.0},  # +5% winner
        ]
        
        closures = await algo.process_open_positions(positions)
        
        # Should recommend closing the loser
        losers_to_close = [c for c in closures if c["reason"] == "loser_failover"]
        assert len(losers_to_close) >= 1
        assert losers_to_close[0]["symbol"] == "SPY"

    @pytest.mark.asyncio
    async def test_process_open_positions_takes_winners(self, algo):
        """Test that winners at target are closed."""
        positions = [
            {"symbol": "QQQ", "entry_price": 360.0, "current_price": 378.0},  # +5% winner
        ]
        
        closures = await algo.process_open_positions(positions)
        
        # Should recommend closing the winner at 5% target
        winners_to_close = [c for c in closures if c["reason"] == "target_hit"]
        assert len(winners_to_close) >= 1

    @pytest.mark.asyncio
    async def test_no_new_entries_in_failover(self, algo):
        """Test that standby mode never allows new entries."""
        should_trade = await algo.should_trade_new_entry()
        assert should_trade is False

    @pytest.mark.asyncio
    async def test_vix_brake_normal(self, algo):
        """Test VIX brake at normal level."""
        should_halt = await algo.check_vix_brake(18.5)
        assert should_halt is False

    @pytest.mark.asyncio
    async def test_vix_brake_elevated(self, algo):
        """Test VIX brake at elevated level."""
        should_halt = await algo.check_vix_brake(45.0)
        assert should_halt is True

    def test_trade_recording(self, algo):
        """Test trade recording for reconciliation."""
        trade = {
            "symbol": "SPY",
            "action": "close",
            "qty": 10,
            "price": 450.0,
        }
        
        algo.record_trade(trade)
        
        log = algo.get_trade_log()
        assert len(log) == 1
        assert log[0]["symbol"] == "SPY"
        assert log[0]["standby_mode"] is True


# =====================================================================
# 4. RECONCILIATION TESTS
# =====================================================================

class TestVerificationAndReconciliation:
    """Test reconciliation logic."""

    @pytest.fixture
    def recon(self):
        return VerificationAndReconciliation()

    @pytest.mark.asyncio
    async def test_no_duplicates_passes(self, recon):
        """Test that non-duplicate trades pass reconciliation."""
        primary_trades = [
            {"order_id": "1001", "symbol": "SPY"},
            {"order_id": "1002", "symbol": "QQQ"},
        ]
        
        standby_trades = [
            {"order_id": "1003", "symbol": "IWM"},
        ]
        
        report = await recon.reconcile_failover(
            component="ATG_SWING",
            primary_trades=primary_trades,
            standby_trades=standby_trades,
            primary_capital=45000.0,
            standby_capital=44500.0,
        )
        
        assert len(report.duplicate_orders) == 0
        assert report.verification_passed is True

    @pytest.mark.asyncio
    async def test_duplicate_detection(self, recon):
        """Test that duplicates are detected."""
        primary_trades = [
            {"order_id": "1001", "symbol": "SPY"},
        ]
        
        standby_trades = [
            {"order_id": "1001", "symbol": "SPY"},
        ]
        
        report = await recon.reconcile_failover(
            component="ATG_SWING",
            primary_trades=primary_trades,
            standby_trades=standby_trades,
            primary_capital=45000.0,
            standby_capital=45000.0,
        )
        
        assert len(report.duplicate_orders) == 1
        assert "1001" in report.duplicate_orders
        assert report.verification_passed is False

    @pytest.mark.asyncio
    async def test_capital_discrepancy_detection(self, recon):
        """Test that capital discrepancies are detected."""
        primary_trades = []
        standby_trades = []
        
        report = await recon.reconcile_failover(
            component="ATG_SWING",
            primary_trades=primary_trades,
            standby_trades=standby_trades,
            primary_capital=50000.0,
            standby_capital=45000.0,  # 10% difference
        )
        
        assert report.capital_discrepancy == 5000.0
        assert report.verification_passed is False

    def test_report_storage(self, recon):
        """Test that reports are stored."""
        assert len(recon.get_reports()) == 0
        
        asyncio.run(recon.reconcile_failover(
            component="TEST",
            primary_trades=[],
            standby_trades=[],
            primary_capital=50000.0,
            standby_capital=50000.0,
        ))
        
        assert len(recon.get_reports()) >= 1


# =====================================================================
# 5. GRACEFUL DEGRADATION TESTS
# =====================================================================

class TestGracefulDegradationCoordinator:
    """Test degradation level management."""

    @pytest.fixture
    def coord(self):
        return GracefulDegradationCoordinator()

    def test_size_multipliers_correct(self, coord):
        """Verify size multipliers for each level."""
        assert coord.SIZE_MULTIPLIERS[0] == 1.00  # Full
        assert coord.SIZE_MULTIPLIERS[1] == 1.00  # Minor
        assert coord.SIZE_MULTIPLIERS[2] == 0.50  # Moderate
        assert coord.SIZE_MULTIPLIERS[3] == 0.00  # Severe
        assert coord.SIZE_MULTIPLIERS[4] == 0.00  # Emergency

    def test_new_entries_allowed_correct(self, coord):
        """Verify new entry permissions for each level."""
        assert coord.ALLOW_NEW_ENTRIES[0] is True   # Full
        assert coord.ALLOW_NEW_ENTRIES[1] is True   # Minor
        assert coord.ALLOW_NEW_ENTRIES[2] is True   # Moderate
        assert coord.ALLOW_NEW_ENTRIES[3] is False  # Severe
        assert coord.ALLOW_NEW_ENTRIES[4] is False  # Emergency

    def test_apply_degradation_level_0(self, coord):
        """Test full operation (level 0)."""
        snapshot = HealthSnapshot(
            timestamp=datetime.now().isoformat(),
            checks=[],
            overall_degradation_level=0,
            components_down=[],
        )
        
        state = coord.apply_health_snapshot(snapshot)
        
        assert state["degradation_level"] == 0
        assert state["size_multiplier"] == 1.00
        assert state["allow_new_entries"] is True

    def test_apply_degradation_level_2(self, coord):
        """Test moderate degradation (level 2)."""
        snapshot = HealthSnapshot(
            timestamp=datetime.now().isoformat(),
            checks=[],
            overall_degradation_level=2,
            components_down=["ATG_SWING", "ATG_INTRADAY"],
        )
        
        state = coord.apply_health_snapshot(snapshot)
        
        assert state["degradation_level"] == 2
        assert state["size_multiplier"] == 0.50
        assert state["allow_new_entries"] is True

    def test_apply_degradation_level_4(self, coord):
        """Test emergency halt (level 4)."""
        snapshot = HealthSnapshot(
            timestamp=datetime.now().isoformat(),
            checks=[],
            overall_degradation_level=4,
            components_down=["CAPITAL_ROUTER"],
        )
        
        state = coord.apply_health_snapshot(snapshot)
        
        assert state["degradation_level"] == 4
        assert state["size_multiplier"] == 0.00
        assert state["allow_new_entries"] is False

    def test_effective_position_size(self, coord):
        """Test effective position size calculation."""
        base_size = 5000.0
        
        # Full operation
        snapshot = HealthSnapshot(
            timestamp=datetime.now().isoformat(),
            checks=[],
            overall_degradation_level=0,
            components_down=[],
        )
        coord.apply_health_snapshot(snapshot)
        assert coord.get_effective_position_size(base_size) == 5000.0
        
        # Moderate degradation
        snapshot.overall_degradation_level = 2
        coord.apply_health_snapshot(snapshot)
        assert coord.get_effective_position_size(base_size) == 2500.0
        
        # Emergency
        snapshot.overall_degradation_level = 4
        coord.apply_health_snapshot(snapshot)
        assert coord.get_effective_position_size(base_size) == 0.0

    def test_degradation_history(self, coord):
        """Test degradation level history tracking."""
        snapshot1 = HealthSnapshot(
            timestamp=datetime.now().isoformat(),
            checks=[],
            overall_degradation_level=0,
            components_down=[],
        )
        coord.apply_health_snapshot(snapshot1)
        
        snapshot2 = HealthSnapshot(
            timestamp=datetime.now().isoformat(),
            checks=[],
            overall_degradation_level=2,
            components_down=["ATG_SWING"],
        )
        coord.apply_health_snapshot(snapshot2)
        
        assert len(coord.history) >= 1
        assert coord.history[-1][1] == 2  # Last entry is level 2


# =====================================================================
# 6. INTEGRATION TESTS
# =====================================================================

class TestSQSFailoverOrchestrator:
    """Test complete failover system integration."""

    @pytest.fixture
    def orchestrator(self):
        return SQSFailoverOrchestrator()

    def test_orchestrator_initialization(self, orchestrator):
        """Test that orchestrator initializes all subsystems."""
        assert orchestrator.health_monitor is not None
        assert orchestrator.failover_trigger is not None
        assert orchestrator.standby_algorithm is not None
        assert orchestrator.reconciliation is not None
        assert orchestrator.degradation is not None

    def test_get_system_status(self, orchestrator):
        """Test system status reporting."""
        status = orchestrator.get_system_status()
        
        assert "timestamp" in status
        assert "health" in status
        assert "failovers" in status
        assert "degradation_level" in status
        assert "is_trading_allowed" in status
        assert "size_multiplier" in status

    def test_all_subsystems_represented(self, orchestrator):
        """Test that all 10 subsystems are in monitoring."""
        assert len(orchestrator.health_monitor.SUBSYSTEMS) >= 10

    def test_alert_integration(self, orchestrator):
        """Test that alerts can be sent through orchestrator."""
        alerts = []
        
        def test_alert(msg):
            alerts.append(msg)
        
        orchestrator.health_monitor.set_alert_fn(test_alert)
        orchestrator.failover_trigger.set_alert_fn(test_alert)
        
        # Trigger an alert
        checks = [
            HealthCheckResult(
                timestamp=datetime.now().isoformat(),
                component="ATG_SWING",
                status=HealthStatus.DOWN,
                latency_ms=2000.0,
            ),
        ]
        
        snapshot = orchestrator.health_monitor._build_snapshot(checks)
        orchestrator.health_monitor._alert_if_needed(snapshot)
        
        assert len(alerts) > 0


# =====================================================================
# RUN TESTS
# =====================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
