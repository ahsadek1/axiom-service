"""NEXUS Universal Health System"""
import os
from datetime import datetime
from .agent_health_base import AgentHealthMonitor, Incident
from .error_registry import ERROR_REGISTRY, ErrorDefinition, Severity, Category

def get_health(db_path: str, market_state, preflight_passed: bool, scanner_active: bool, version: str) -> dict:
    """
    Unified health endpoint for Nexus V2.
    
    Args:
        db_path: Path to SQLite DB file
        market_state: MarketState instance with market context
        preflight_passed: Boolean indicating if preflight checks passed
        scanner_active: Boolean indicating if APScheduler scanner is running
        version: Service version string
    
    Returns:
        Dict with status, service name, version, and component health
    """
    # Check DB accessibility
    db_ok = os.path.exists(db_path) and os.path.getsize(db_path) > 0
    
    # Determine overall status
    overall_status = "healthy" if (db_ok and preflight_passed and scanner_active) else "degraded"
    
    return {
        "status": overall_status,
        "service": "nexus-v2",
        "version": version,
        "db_ok": db_ok,
        "preflight_passed": preflight_passed,
        "scanner_active": scanner_active,
        "market_open": getattr(market_state, 'is_market_open', False),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
