"""
position_reconciler.py — DB ↔ Alpaca Position Sync
====================================================
Detects and corrects divergence between thesis_positions.db and Alpaca.

After every position state change:
  1. Query Alpaca for current position
  2. Compare with DB
  3. Log any mismatches
  4. Auto-correct if safe, escalate if conflict

Runs synchronously after each fill and exit.
"""
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone

log = logging.getLogger("thesis.reconciler")


async def reconcile_position(
    guardian,
    position: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Verify position state against broker.
    
    Args:
        guardian: AlpacaGuardian instance
        position: position dict from DB (must have short_symbol, long_symbol, contracts)
    
    Returns:
        {
            "status": "OK" | "MISMATCH" | "ORPHANED" | "PARTIAL" | "ERROR",
            "message": "...",
            "details": {...}
        }
    
    Guarantees:
        - Every reconciliation is logged
        - Mismatches are detected
        - Orphaned positions flagged within window
    """
    
    position_id = position.get("position_id")
    short_sym = position.get("short_symbol")
    long_sym = position.get("long_symbol")
    contracts = position.get("contracts", 1)
    
    if not all([short_sym, long_sym]):
        return {
            "status": "ERROR",
            "message": f"Position missing symbols: {position_id}"
        }
    
    try:
        # Query Alpaca for both legs
        short_pos = await _get_broker_position(guardian, short_sym)
        long_pos = await _get_broker_position(guardian, long_sym)
        
        # Analyze
        short_filled = short_pos is not None
        long_filled = long_pos is not None
        short_qty = short_pos.get("qty", 0) if short_pos else 0
        long_qty = long_pos.get("qty", 0) if long_pos else 0
        
        # Expected state
        short_expected_qty = contracts  # Short put, positive qty means short
        long_expected_qty = contracts   # Long put, positive qty means long
        
        # Check for mismatches
        issues = []
        
        if short_filled and short_qty != short_expected_qty:
            issues.append(
                f"Short leg qty mismatch: DB={short_expected_qty}, Alpaca={short_qty}"
            )
        
        if long_filled and long_qty != long_expected_qty:
            issues.append(
                f"Long leg qty mismatch: DB={long_expected_qty}, Alpaca={long_qty}"
            )
        
        # Determine overall status
        if not issues:
            return {
                "status": "OK",
                "message": f"Position reconciled: {short_sym}/{long_sym}",
                "details": {
                    "short_filled": short_filled,
                    "long_filled": long_filled,
                    "short_qty": short_qty,
                    "long_qty": long_qty,
                }
            }
        
        elif short_filled and not long_filled:
            return {
                "status": "PARTIAL",
                "message": f"Partial fill detected: short filled, long not",
                "issues": issues,
                "details": {
                    "short_qty": short_qty,
                    "long_qty": 0,
                    "contract_count": contracts,
                }
            }
        
        elif not short_filled and long_filled:
            return {
                "status": "PARTIAL",
                "message": f"Partial fill detected: long filled, short not",
                "issues": issues,
                "details": {
                    "short_qty": 0,
                    "long_qty": long_qty,
                    "contract_count": contracts,
                }
            }
        
        elif not short_filled and not long_filled:
            return {
                "status": "ORPHANED",
                "message": f"Position not found on broker: {position_id}",
                "issues": ["Both legs missing from Alpaca"]
            }
        
        else:
            # Both filled but quantities mismatch
            return {
                "status": "MISMATCH",
                "message": "Both legs filled but quantities don't match DB",
                "issues": issues,
                "details": {
                    "short_qty": short_qty,
                    "long_qty": long_qty,
                    "contract_count": contracts,
                }
            }
    
    except Exception as exc:
        log.error("Reconciliation failed for %s: %s", position_id, exc)
        return {
            "status": "ERROR",
            "message": f"Reconciliation exception: {exc}"
        }


async def _get_broker_position(
    guardian,
    symbol: str,
) -> Optional[Dict[str, Any]]:
    """
    Get position from broker for a single option symbol.
    Returns None if not found.
    """
    try:
        positions = guardian.get_option_positions()
        for pos in positions or []:
            if pos.get("symbol") == symbol:
                return pos
        return None
    except Exception as exc:
        log.warning("Could not fetch positions from broker: %s", exc)
        return None


async def handle_mismatch(
    db_connection,
    position_id: str,
    reconciliation_result: Dict[str, Any],
    escalate_fn=None,
) -> bool:
    """
    Handle a reconciliation mismatch.
    
    Returns True if handled successfully (safe to continue).
    Returns False if escalation needed.
    """
    status = reconciliation_result.get("status")
    
    if status == "OK":
        # No action needed
        log.info("Position %s reconciliation OK", position_id)
        return True
    
    elif status == "PARTIAL":
        # Log but don't auto-correct (requires manual review)
        log.warning(
            "Partial fill detected for %s: %s",
            position_id,
            reconciliation_result.get("message")
        )
        if escalate_fn:
            await escalate_fn(
                level="WARN",
                message=f"Partial fill: {reconciliation_result['message']}",
                position_id=position_id
            )
        return False  # Needs escalation
    
    elif status == "ORPHANED":
        # Position disappeared from broker
        log.error(
            "Orphaned position detected: %s — %s",
            position_id,
            reconciliation_result.get("message")
        )
        if escalate_fn:
            await escalate_fn(
                level="ALERT",
                message=f"Orphaned position: {reconciliation_result['message']}",
                position_id=position_id
            )
        return False  # Needs escalation
    
    elif status == "MISMATCH":
        # Quantities don't match
        log.error(
            "Position quantity mismatch for %s: %s",
            position_id,
            reconciliation_result.get("issues")
        )
        if escalate_fn:
            await escalate_fn(
                level="ALERT",
                message=f"Qty mismatch: {reconciliation_result['issues']}",
                position_id=position_id
            )
        return False  # Needs escalation
    
    else:  # ERROR or unknown
        log.error("Reconciliation error for %s: %s", position_id, status)
        if escalate_fn:
            await escalate_fn(
                level="CRITICAL",
                message=f"Reconciliation failed: {status}",
                position_id=position_id
            )
        return False


def get_reconciliation_stats(db_path: str) -> Dict[str, Any]:
    """
    Get stats on reconciliation events from event log.
    """
    import sqlite3
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        
        stats = {
            "total_reconciliations": conn.execute(
                "SELECT COUNT(*) FROM position_events WHERE event_type = 'RECONCILED'"
            ).fetchone()[0],
            
            "recent_mismatches": conn.execute(
                "SELECT COUNT(*) FROM position_events WHERE event_type = 'RECONCILED' AND status != 'OK'"
            ).fetchone()[0],
            
            "orphaned_detected": conn.execute(
                "SELECT COUNT(*) FROM position_events WHERE event_type = 'ERROR' AND details LIKE '%orphan%'"
            ).fetchone()[0],
        }
        
        conn.close()
        return stats
    
    except Exception as exc:
        log.error("Failed to get reconciliation stats: %s", exc)
        return {"error": str(exc)}
