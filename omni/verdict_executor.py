"""
verdict_executor.py — Poll OMNI verdicts and route GO signals to Alpha execution.

CRITICAL: This service fills the gap between OMNI synthesis → Alpha execution.
Without this, GO verdicts are synthesized but never executed.

Runs as a background loop in OMNI lifespan.
"""

import logging
import sqlite3
import os
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import requests
import json

logger = logging.getLogger("omni.verdict_executor")

OMNI_DB = os.getenv("OMNI_DB", "/Users/ahmedsadek/nexus/data/omni.db")
ALPHA_EXEC_URL = os.getenv("ALPHA_EXECUTION_URL", "http://localhost:8005")
NEXUS_SECRET = os.getenv("NEXUS_SECRET", "")
POLL_INTERVAL_SEC = 5  # Poll every 5 seconds


def get_pending_go_verdicts() -> list[Dict[str, Any]]:
    """Fetch all GO verdicts that haven't been dispatched to Alpha yet."""
    try:
        conn = sqlite3.connect(OMNI_DB)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        # Get GO verdicts from omni.db synthesis_results
        # These are already synthesized and ready to execute
        cur.execute('''
            SELECT id, ticker, direction, system, pathway, agent_weighted_score, window_id, created_at
            FROM synthesis_results
            WHERE agent_weighted_score >= 70
            AND system = "alpha"
            AND created_at > datetime('now', '-2 hours')
            AND (execution_response IS NULL OR execution_response NOT LIKE "HTTP 20%")
            ORDER BY created_at DESC
            LIMIT 20
        ''')
        
        verdicts = [dict(row) for row in cur.fetchall()]
        conn.close()
        return verdicts
        
    except Exception as e:
        logger.error("Failed to fetch pending GO verdicts: %s", e)
        return []


def submit_to_alpha(verdict: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """
    Convert verdict to execution request and POST to Alpha's /execute endpoint.
    
    Returns: (success: bool, message: str)
    """
    ticker = verdict.get("ticker")
    direction = verdict.get("direction")
    pathway = verdict.get("pathway", "P1")
    window_id = verdict.get("window_id", "")
    weighted_score = verdict.get("agent_weighted_score", 75.0)
    
    if not ticker:
        return False, "No ticker in verdict"
    
    # Build execution payload — must match ExecuteRequest schema in alpha-execution/main.py
    payload = {
        "ticker": ticker,
        "direction": direction,  # "bullish" or "bearish"
        "pathway": pathway,
        "window_id": window_id,  # Required by Alpha's gate
        "weighted_score": weighted_score,
        "sizing_mult": 1.0,  # No default multiplication
        "echo_chamber": False,
        "auto_execute": True,
    }
    
    try:
        resp = requests.post(
            f"{ALPHA_EXEC_URL}/execute",
            json=payload,
            headers={
                "X-Nexus-Secret": NEXUS_SECRET,
                "Content-Type": "application/json"
            },
            timeout=5,
        )
        
        if resp.status_code in (200, 201, 202):
            logger.info(
                "✅ Dispatch success: %s/%s → Alpha (HTTP %d)",
                ticker, direction, resp.status_code
            )
            return True, f"HTTP {resp.status_code}"
        else:
            logger.error(
                "❌ Dispatch failed: %s/%s → Alpha (HTTP %d): %s",
                ticker, direction, resp.status_code, resp.text[:200]
            )
            return False, f"HTTP {resp.status_code}: {resp.text[:100]}"
            
    except requests.Timeout:
        logger.error("Dispatch timeout: %s/%s → Alpha", ticker, direction)
        return False, "Timeout"
    except Exception as e:
        logger.error("Dispatch exception: %s/%s → %s", ticker, direction, str(e))
        return False, str(e)


def log_execution_attempt(ticker: str, success: bool, message: str) -> None:
    """Log execution attempt — update synthesis_results.execution_response."""
    try:
        conn = sqlite3.connect(OMNI_DB)
        cur = conn.cursor()
        
        cur.execute('''
            UPDATE synthesis_results 
            SET execution_response = ?
            WHERE ticker = ? 
            AND created_at > datetime('now', '-1 hour')
            ORDER BY created_at DESC
            LIMIT 1
        ''', (
            message,
            ticker,
        ))
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("Failed to log execution attempt: %s", e)


def run_executor_loop() -> None:
    """Main executor loop — dispatch GO verdicts to Alpha as fast as they arrive."""
    logger.info("✅ Verdict Executor started")
    
    while True:
        try:
            verdicts = get_pending_go_verdicts()
            
            if verdicts:
                logger.info(f"Found {len(verdicts)} pending GO verdicts to execute")
                
                for verdict in verdicts:
                    success, msg = submit_to_alpha(verdict)
                    log_execution_attempt(verdict["ticker"], success, msg)
                    
                    if success:
                        logger.info("✅ %s/%s dispatched to Alpha", verdict["ticker"], verdict["direction"])
                    else:
                        # Alpha will backoff on 429 — just log and try next
                        logger.debug("⚠️ %s/%s: %s", verdict["ticker"], verdict["direction"], msg[:60])
                    
                    # Small delay between requests (100ms) to avoid hammering
                    time.sleep(0.1)
            
            # Poll every 2 seconds for new verdicts
            time.sleep(2)
            
        except Exception as e:
            logger.error("Executor loop error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_executor_loop()
