"""
service_monitors.py — Per-Service Health Monitor Configurations
===============================================================
Each V2 service gets its own AgentHealthMonitor with its specific
domain checks. Import and call start_monitor() from each service's
lifespan/startup sequence.

Usage in a service's main.py:
    from shared.health.service_monitors import start_omni_monitor
    start_omni_monitor()  # call once at startup
"""
from __future__ import annotations
from .agent_health_base import AgentHealthMonitor
from .domain_checks import (
    check_omni_synthesis_silence,
    check_omni_deterministic_mode,
    check_circuit_breaker,
    check_concordance_rate,
    check_vix_brake,
    check_dlq_backlog,
    check_axiom_pool,
    check_cipher_submissions,
    check_atlas_submissions,
    check_sage_submissions,
    check_memory,
    check_disk,
    check_backtest_db,
)

# ---------------------------------------------------------------------------
# One monitor per service — owns its domain completely
# ---------------------------------------------------------------------------

def start_omni_monitor() -> AgentHealthMonitor:
    """
    OMNI owns: synthesis pipeline, deterministic mode, THESIS archive removal.
    Checks: self health, synthesis silence, deterministic mode.
    """
    monitor = AgentHealthMonitor(
        agent_name   = "OMNI",
        service_port = 8004,
        plist_name   = "ai.nexus.omni",
        domain_checks = [
            check_omni_synthesis_silence,
            check_omni_deterministic_mode,
            check_backtest_db,
        ],
    )
    monitor.start()
    return monitor


def start_alpha_buffer_monitor() -> AgentHealthMonitor:
    """
    Alpha-buffer owns: circuit breaker, concordance pipeline, submission intake.
    Checks: self health, CB state, concordance rate.
    """
    monitor = AgentHealthMonitor(
        agent_name   = "alpha-buffer",
        service_port = 8002,
        plist_name   = "ai.nexus.alpha-buffer",
        domain_checks = [
            check_circuit_breaker,
            check_concordance_rate,
            check_cipher_submissions,
            check_atlas_submissions,
            check_sage_submissions,
        ],
    )
    monitor.start()
    return monitor


def start_alpha_execution_monitor() -> AgentHealthMonitor:
    """
    Alpha-execution owns: VIX brake, DLQ, position limits, order routing.
    Checks: self health, VIX brake, DLQ backlog.
    """
    monitor = AgentHealthMonitor(
        agent_name   = "alpha-execution",
        service_port = 8005,
        plist_name   = "ai.nexus.alpha-execution",
        domain_checks = [
            check_vix_brake,
            check_dlq_backlog,
            check_memory,
        ],
    )
    monitor.start()
    return monitor


def start_axiom_monitor() -> AgentHealthMonitor:
    """
    Axiom owns: ticker pool, regime classification, risk assessment.
    Checks: self health, pool size during market hours.
    """
    monitor = AgentHealthMonitor(
        agent_name   = "Axiom",
        service_port = 8001,
        plist_name   = "ai.nexus.axiom",
        domain_checks = [
            check_axiom_pool,
            check_disk,
        ],
    )
    monitor.start()
    return monitor


def start_oracle_monitor() -> AgentHealthMonitor:
    """ORACLE owns: options flow data, IV rank, market context."""
    monitor = AgentHealthMonitor(
        agent_name   = "ORACLE",
        service_port = 8007,
        plist_name   = "ai.nexus.oracle",
        domain_checks = [],
    )
    monitor.start()
    return monitor


def start_ails_monitor() -> AgentHealthMonitor:
    """AILS owns: learning loop, universe management, backtest extension."""
    monitor = AgentHealthMonitor(
        agent_name   = "AILS",
        service_port = 8008,
        plist_name   = "ai.nexus.ails",
        domain_checks = [
            check_backtest_db,
        ],
    )
    monitor.start()
    return monitor


def start_thesis_trader_monitor() -> AgentHealthMonitor:
    """
    THESIS owns: Five Legends screening, execution pipeline,
    exit monitor, learning loop, own Alpaca account.
    Full self-sovereign domain coverage.
    """
    from .domain_checks import (
        check_thesis_alpaca_connected,
        check_thesis_buying_power,
        check_thesis_positions_db,
        check_thesis_screening_active,
        check_thesis_exit_monitor,
    )
    monitor = AgentHealthMonitor(
        agent_name   = "THESIS-Trader",
        service_port = 8070,
        plist_name   = "ai.nexus.thesis-trader",
        domain_checks = [
            check_thesis_alpaca_connected,
            check_thesis_buying_power,
            check_thesis_positions_db,
            check_thesis_screening_active,
            check_thesis_exit_monitor,
            check_backtest_db,
            check_disk,
        ],
    )
    monitor.start()
    return monitor


# ---------------------------------------------------------------------------
# Start all monitors at once (called by Autonomous Action Engine)
# ---------------------------------------------------------------------------

def start_all_monitors() -> list[AgentHealthMonitor]:
    """
    Start health monitors for all V2 services.
    Called once at system startup.
    Each monitor runs independently in its own background thread.
    """
    monitors = [
        start_axiom_monitor(),
        start_oracle_monitor(),
        start_ails_monitor(),
        start_alpha_buffer_monitor(),
        start_omni_monitor(),
        start_alpha_execution_monitor(),
        start_thesis_trader_monitor(),
    ]
    return monitors
