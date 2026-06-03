"""
order_guard.py — Idempotent Order Placement with Retry
========================================================
Wraps Alpaca order placement with:
  - Idempotent client_order_id (based on spread signature)
  - Exponential backoff retry (3 attempts)
  - Timeout recovery (escalate after 45s)
  - Prevents double-submission on failure

Integration point: spread_executor.place_option_order()
"""
import hashlib
import logging
import time
from typing import Optional, Dict, Any
from datetime import datetime, timezone

log = logging.getLogger("thesis.order_guard")

# Idempotency cache: symbol+side+contracts+limit_price → order_id
_idempotency_cache: Dict[str, str] = {}

def _compute_idempotency_key(
    symbol: str,
    side: str,
    contracts: int,
    limit_price: Optional[float] = None,
) -> str:
    """
    Compute deterministic idempotency key.
    Same input always produces same key.
    Key is tied to the spread signature, not random.
    """
    sig = f"{symbol}|{side}|{contracts}|{limit_price}"
    return hashlib.sha256(sig.encode()).hexdigest()[:48]


def place_option_order_safe(
    guardian,
    symbol: str,
    side: str,
    contracts: int = 1,
    order_type: str = "limit",
    limit_price: Optional[float] = None,
    max_retries: int = 3,
    initial_backoff: float = 1.0,
) -> Dict[str, Any]:
    """
    Place option order with idempotency + retry.
    
    Args:
        guardian: AlpacaGuardian instance
        symbol: option symbol (e.g., "SPY250117P450")
        side: "buy" or "sell"
        contracts: number of contracts
        order_type: "limit" or "market"
        limit_price: limit price (for limit orders)
        max_retries: max retry attempts
        initial_backoff: initial backoff in seconds
    
    Returns:
        {
            "status": "FILLED" | "TIMEOUT" | "REJECTED" | "ERROR",
            "order_id": "...",
            "filled_price": 0.50,
            "contracts": 1,
            "message": "...",
        }
    
    Guarantees:
        - Idempotent: same input → same result
        - No double-submit: retries use same client_order_id
        - Timeout escalates after 45s
        - All errors are explicit (not silent)
    """
    
    idempotency_key = _compute_idempotency_key(symbol, side, contracts, limit_price)
    
    # Check cache: have we already placed this order?
    if idempotency_key in _idempotency_cache:
        cached_order_id = _idempotency_cache[idempotency_key]
        log.info(
            "Order already placed (cached idempotency): %s %s → id=%s (re-using)",
            side, symbol, cached_order_id
        )
        return {
            "status": "CACHED",
            "order_id": cached_order_id,
            "message": "Using cached order (idempotent)"
        }
    
    attempt = 0
    backoff = initial_backoff
    start_time = time.time()
    timeout_limit = 45.0  # 45 second hard timeout
    
    while attempt < max_retries:
        attempt += 1
        elapsed = time.time() - start_time
        
        # Hard timeout after 45 seconds
        if elapsed > timeout_limit:
            log.error(
                "Order timeout after %.1f seconds (%d attempts): %s %s",
                elapsed, attempt, side, symbol
            )
            return {
                "status": "TIMEOUT",
                "order_id": None,
                "message": f"Order timeout after {elapsed:.1f}s and {attempt} attempts"
            }
        
        try:
            log.info(
                "Placing order (attempt %d/%d): %s %s contracts=%d limit=%.2f",
                attempt, max_retries, side, symbol, contracts,
                limit_price or 0
            )
            
            order = guardian.place_option_order(
                contract_symbol=symbol,
                qty=contracts,
                side=side,
                order_type=order_type,
                limit_price=limit_price,
                client_order_id=idempotency_key,  # Deterministic, not random
            )
            
            if not order:
                raise ValueError("Guardian returned None")
            
            order_id = order.get("id")
            if not order_id:
                raise ValueError("Order missing ID")
            
            # Cache the successful order
            _idempotency_cache[idempotency_key] = order_id
            
            log.info("Order placed successfully: %s → id=%s", symbol, order_id)
            
            return {
                "status": "PLACED",
                "order_id": order_id,
                "filled_price": order.get("filled_avg_price"),
                "message": "Order placed and cached"
            }
        
        except Exception as exc:
            log.warning(
                "Order placement failed (attempt %d/%d): %s — %s",
                attempt, max_retries, symbol, exc
            )
            
            # On first attempt, check if it's a rejection (not retryable)
            error_msg = str(exc).lower()
            if "not found" in error_msg or "invalid" in error_msg or "not tradable" in error_msg:
                log.error("Order rejected (not retryable): %s", exc)
                return {
                    "status": "REJECTED",
                    "order_id": None,
                    "message": f"Order rejected: {exc}"
                }
            
            # Retryable error — backoff and retry
            if attempt < max_retries:
                elapsed = time.time() - start_time
                if elapsed + backoff > timeout_limit:
                    # Would exceed timeout if we backoff
                    log.error(
                        "Would exceed timeout on next retry. Giving up after %.1f seconds",
                        elapsed
                    )
                    return {
                        "status": "TIMEOUT",
                        "order_id": None,
                        "message": f"Timeout after {elapsed:.1f}s (would exceed limit on retry)"
                    }
                
                log.info("Backing off %.1f seconds before retry", backoff)
                time.sleep(backoff)
                backoff *= 2  # Exponential backoff
            else:
                # Final attempt failed
                log.error("Order failed after %d attempts: %s", max_retries, symbol)
                return {
                    "status": "ERROR",
                    "order_id": None,
                    "message": f"Failed after {max_retries} attempts: {exc}"
                }
    
    # Should not reach here
    return {
        "status": "ERROR",
        "order_id": None,
        "message": "Order guard logic error"
    }


def clear_idempotency_cache():
    """Clear the idempotency cache. Use with caution."""
    global _idempotency_cache
    _idempotency_cache.clear()
    log.info("Idempotency cache cleared")


def get_idempotency_stats() -> Dict[str, Any]:
    """Get stats on idempotency cache."""
    return {
        "cached_orders": len(_idempotency_cache),
        "sample": list(_idempotency_cache.items())[:5]
    }
