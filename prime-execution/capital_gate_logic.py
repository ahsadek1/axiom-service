#!/usr/bin/env python3
"""
OMNI Capital Router Gate Logic
Implements position + allocation checks before Alpha/Prime execution
Ready to integrate into Alpha/Prime execution paths

Status: IMPLEMENTATION READY (awaiting Capital Router Railway deployment)
"""

import requests
import json
from typing import Dict, Tuple
from datetime import datetime, timedelta

CAPITAL_ROUTER_URL = "http://localhost:9100"  # Live as of 2026-05-21 08:59 AM (Capital Router started, auto-bound to 9100 due to port conflict)
ALLOCATION_LOCK_TTL_SEC = 5

class CapitalGate:
    """Gate logic to enforce cross-system position + allocation checks"""
    
    def __init__(self, router_url: str = CAPITAL_ROUTER_URL, timeout_sec: int = 3):
        self.router_url = router_url
        self.timeout = timeout_sec
        self.last_error = None
    
    def check_position_availability(self, ticker: str, system: str) -> Tuple[bool, Dict]:
        """
        Query Capital Router: is this ticker available for execution?
        
        Returns: (allowed: bool, details: dict)
        Details include: position_open, system_holding, qty, gate_result
        """
        try:
            resp = requests.get(
                f"{self.router_url}/positions/cross-system?ticker={ticker}",
                timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            
            # GATE LOGIC:
            # Block if: other system already holding this ticker
            if data.get("position_open"):
                holding_system = data.get("system_holding")
                if holding_system and holding_system != system:
                    return (False, {
                        "gate": "POSITION_RACE_BLOCKED",
                        "reason": f"Ticker {ticker} held by {holding_system}",
                        "position_open": True,
                        "system_holding": holding_system,
                        "qty": data.get("qty", 0)
                    })
            
            return (True, {
                "gate": "POSITION_AVAILABLE",
                "position_open": data.get("position_open", False),
                "system_holding": data.get("system_holding"),
                "qty": data.get("qty", 0)
            })
            
        except requests.RequestException as e:
            self.last_error = str(e)
            # FAIL-SAFE: if router is unreachable, allow execution with caution
            # (don't block trading on router outage)
            return (True, {
                "gate": "ROUTER_UNREACHABLE_PASS",
                "error": str(e),
                "warning": "Position race gate not available — continuing with caution"
            })
    
    def request_allocation(self, ticker: str, system: str, amount_usd: float) -> Tuple[bool, Dict]:
        """
        Request capital allocation lock from router.
        Returns allocation_id + locked_until timestamp if successful.
        
        Returns: (locked: bool, details: dict)
        """
        try:
            resp = requests.post(
                f"{self.router_url}/allocate-capital",
                json={
                    "ticker": ticker,
                    "system": system,
                    "amount_usd": amount_usd
                },
                timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            
            return (True, {
                "gate": "ALLOCATION_LOCKED",
                "allocation_id": data.get("allocation_id"),
                "locked_until": data.get("locked_until"),
                "system": system,
                "ticker": ticker,
                "amount": amount_usd
            })
            
        except requests.RequestException as e:
            self.last_error = str(e)
            # FAIL-SAFE: allow execution but log warning
            return (True, {
                "gate": "ALLOCATION_ROUTER_UNAVAILABLE",
                "error": str(e),
                "warning": "Allocation lock not available — continuing with caution",
                "ticker": ticker,
                "system": system
            })
    
    def release_allocation(self, allocation_id: str) -> bool:
        """Release capital allocation lock after execution completes"""
        try:
            resp = requests.post(
                f"{self.router_url}/release-allocation/{allocation_id}",
                timeout=self.timeout
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            self.last_error = str(e)
            # Non-critical: lock will expire naturally after TTL
            return False
    
    def pre_execution_check(self, ticker: str, system: str, amount_usd: float) -> Tuple[bool, Dict]:
        """
        Full pre-execution gate:
        1. Check if ticker position is available (no race)
        2. Request capital allocation lock
        3. Return gate result
        
        Returns: (allowed: bool, result: dict with all checks)
        """
        result = {
            "timestamp": datetime.utcnow().isoformat(),
            "system": system,
            "ticker": ticker,
            "amount_usd": amount_usd,
            "checks": {}
        }
        
        # Check 1: Position availability
        pos_allowed, pos_details = self.check_position_availability(ticker, system)
        result["checks"]["position"] = pos_details
        if not pos_allowed:
            result["allowed"] = False
            result["blocked_by"] = "POSITION_RACE"
            return (False, result)
        
        # Check 2: Capital allocation
        alloc_ok, alloc_details = self.request_allocation(ticker, system, amount_usd)
        result["checks"]["allocation"] = alloc_details
        if not alloc_ok:
            result["allowed"] = False
            result["blocked_by"] = "ALLOCATION_FAILED"
            return (False, result)
        
        # All gates passed
        result["allowed"] = True
        result["allocation_id"] = alloc_details.get("allocation_id")
        return (True, result)


# INTEGRATION POINTS (for Alpha/Prime execution)
#
# Before Alpha/Prime calls /submit or Alpaca order:
#
# gate = CapitalGate(router_url="http://localhost:9100")
# allowed, gate_result = gate.pre_execution_check(
#     ticker=body.ticker,
#     system="alpha",  # or "prime"
#     amount_usd=position_size_usd
# )
#
# if not allowed:
#     logger.warning(f"Gate blocked: {gate_result}")
#     return JSONResponse(
#         status_code=403,
#         content={"executed": False, "reason": gate_result["blocked_by"], "gate": gate_result}
#     )
#
# allocation_id = gate_result["allocation_id"]
# 
# # Execute order...
# order_result = alpaca_client.submit_order(...)
#
# # After successful execution, release lock:
# if order_result.status == "filled":
#     gate.release_allocation(allocation_id)
