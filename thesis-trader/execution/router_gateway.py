"""
router_gateway.py — Capital Router Integration
===============================================
All position allocations coordinate with Capital Router at /localhost:9100.

Lifecycle:
  1. Before order: allocate capital with router
  2. After fill: confirm allocation (idempotent)
  3. On exit: release allocation

Guarantees:
  - No capital allocated without router knowing
  - No double allocation across systems
  - Release is idempotent
"""
import logging
import asyncio
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import httpx

log = logging.getLogger("thesis.router_gateway")

ROUTER_BASE_URL = "http://localhost:9100"
ROUTER_TIMEOUT = 5.0


class RouterGateway:
    """Client for Capital Router API."""
    
    def __init__(self, base_url: str = ROUTER_BASE_URL, timeout: float = ROUTER_TIMEOUT):
        self.base_url = base_url
        self.timeout = timeout
    
    async def check_health(self) -> Dict[str, Any]:
        """Check if router is alive."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(f"{self.base_url}/health")
                if r.status_code == 200:
                    return r.json()
        except Exception as exc:
            log.warning("Router health check failed: %s", exc)
        return {"status": "UNAVAILABLE"}
    
    async def allocate_capital(
        self,
        ticker: str,
        amount_usd: float,
        position_type: str = "bull_put_spread",
        dte: int = 37,
        contracts: int = 1,
    ) -> Dict[str, Any]:
        """
        Allocate capital for a position.
        
        Args:
            ticker: stock ticker (e.g., "AAPL")
            amount_usd: amount to allocate in dollars
            position_type: "bull_put_spread", "cash_secured_put", etc.
            dte: days to expiration
            contracts: number of contracts
        
        Returns:
            {
                "allocation_id": "alloc_...",
                "status": "ALLOCATED" | "CONFLICT" | "ERROR",
                "amount": 5000,
                "message": "..."
            }
        
        Conflict: ticker already allocated elsewhere. Must release first.
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(
                    f"{self.base_url}/allocate-capital",
                    json={
                        "ticker": ticker,
                        "amount_usd": amount_usd,
                        "position_type": position_type,
                        "dte": dte,
                        "contracts": contracts,
                        "system": "THESIS",
                    }
                )
                
                if r.status_code == 200:
                    result = r.json()
                    log.info(
                        "Capital allocated: %s $%d → %s",
                        ticker, amount_usd, result.get("allocation_id")
                    )
                    return result
                else:
                    log.warning(
                        "Capital allocation failed (%d): %s",
                        r.status_code, r.text[:100]
                    )
                    return {
                        "allocation_id": None,
                        "status": "ERROR",
                        "message": f"HTTP {r.status_code}: {r.text[:50]}"
                    }
        
        except asyncio.TimeoutError:
            log.error("Capital allocation timeout for %s", ticker)
            return {
                "allocation_id": None,
                "status": "TIMEOUT",
                "message": "Router timeout"
            }
        except Exception as exc:
            log.error("Capital allocation error for %s: %s", ticker, exc)
            return {
                "allocation_id": None,
                "status": "ERROR",
                "message": str(exc)
            }
    
    async def release_allocation(
        self,
        allocation_id: str,
    ) -> Dict[str, Any]:
        """
        Release a capital allocation (idempotent).
        
        Args:
            allocation_id: from allocate_capital response
        
        Returns:
            {
                "status": "RELEASED" | "NOT_FOUND" | "ERROR",
                "message": "..."
            }
        
        Idempotent: calling twice is safe (second call returns NOT_FOUND).
        """
        if not allocation_id:
            return {
                "status": "SKIPPED",
                "message": "No allocation_id to release"
            }
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(
                    f"{self.base_url}/release-allocation/{allocation_id}"
                )
                
                if r.status_code == 200:
                    result = r.json()
                    log.info("Allocation released: %s", allocation_id)
                    return result
                elif r.status_code == 404:
                    log.info("Allocation already released or not found: %s", allocation_id)
                    return {
                        "status": "NOT_FOUND",
                        "message": "Allocation not found (already released?)"
                    }
                else:
                    log.warning(
                        "Release failed (%d): %s",
                        r.status_code, r.text[:100]
                    )
                    return {
                        "status": "ERROR",
                        "message": f"HTTP {r.status_code}"
                    }
        
        except asyncio.TimeoutError:
            log.error("Release allocation timeout: %s", allocation_id)
            return {
                "status": "TIMEOUT",
                "message": "Router timeout"
            }
        except Exception as exc:
            log.error("Release allocation error: %s", exc)
            return {
                "status": "ERROR",
                "message": str(exc)
            }
    
    async def verify_position_state(
        self,
        ticker: str,
    ) -> Dict[str, Any]:
        """
        Check if ticker is allocated elsewhere.
        
        Returns:
            {
                "ticker": "AAPL",
                "allocated": True | False,
                "allocated_to": "SQS" | "THESIS" | etc,
                "amount": 5000,
                "message": "..."
            }
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(
                    f"{self.base_url}/positions/cross-system",
                    params={"ticker": ticker}
                )
                
                if r.status_code == 200:
                    return r.json()
                else:
                    log.warning(
                        "Position state check failed (%d): %s",
                        r.status_code, r.text[:100]
                    )
                    return {
                        "ticker": ticker,
                        "allocated": None,
                        "message": f"HTTP {r.status_code}"
                    }
        
        except Exception as exc:
            log.error("Position state check error: %s", exc)
            return {
                "ticker": ticker,
                "allocated": None,
                "message": str(exc)
            }


# Global instance
_gateway: Optional[RouterGateway] = None


def get_gateway() -> RouterGateway:
    """Get or create gateway instance."""
    global _gateway
    if _gateway is None:
        _gateway = RouterGateway()
    return _gateway


async def test_router_connection() -> bool:
    """Test if router is reachable."""
    gateway = get_gateway()
    health = await gateway.check_health()
    is_healthy = health.get("status") == "ok"
    
    if is_healthy:
        log.info("Router is healthy ✅")
    else:
        log.error("Router is DOWN ❌ — %s", health.get("message", "unknown"))
    
    return is_healthy
