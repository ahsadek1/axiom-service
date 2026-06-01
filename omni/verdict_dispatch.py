#!/usr/bin/env python3
"""
verdict_dispatch.py — OMNI Verdict → Alpha Execution Queue Dispatcher

Polls concordance_results for queued STRONG_GO/GO verdicts and submits them 
to Alpha Execution queue immediately.

This is the missing link that was blocking all trades today.

Author: OMNI (Emergency Fix)
Date: 2026-05-22
"""

import sqlite3
import requests
import logging
import time
import os
from datetime import datetime
import pytz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s"
)
logger = logging.getLogger("verdict_dispatch")

ET = pytz.timezone("America/New_York")

ALPHA_BUFFER_DB = "/Users/ahmedsadek/nexus/data/alpha_buffer.db"
ALPHA_EXEC_DB = "/Users/ahmedsadek/nexus/data/alpha_execution.db"
ALPHA_EXEC_URL = os.getenv("ALPHA_EXECUTION_URL", "http://localhost:8005") + "/execute"

# ENHANCED RATE LIMITING (2026-06-01 — Axiom fix for daily failures)
# Respect downstream engine limits. Conservative submission rate.
REQUEST_RATE_LIMIT = 8   # max reqs per window (reduced from 10)
RATE_LIMIT_WINDOW = 60   # per this many seconds
REQUEST_BATCH_SIZE = 3   # submit 3 at a time (reduced from 5 — more conservative)
REQUEST_DELAY = 2.0      # 2.0s between reqs within batch (increased from 1.2s — safer)
BATCH_WAIT_TIME = 10     # wait 10s between batches (increased from 6s — more breathing room)
MAX_RETRIES_ON_RATE_LIMIT = 3  # retry rate-limited verdicts this many times

def dispatch_verdict_to_execution(ticker: str, direction: str, score: float, pathway: str, window_id: str):
    """Submit verdict to Alpha Execution queue."""
    try:
        # Map legacy pathway codes to execution pathways
        pathway_map = {
            "P1": "ATM_0DTE",
            "P2": "ATG_SWING",
            "P3": "ATG_INTRADAY",
        }
        exec_pathway = pathway_map.get(pathway, pathway)  # use mapped or original
        
        payload = {
            "ticker": ticker,
            "direction": direction,
            "score": score,
            "pathway": exec_pathway,
            "window_id": window_id,
            "sizing_mult": 1.0,
        }
        
        headers = {
            "X-Nexus-Secret": os.getenv("NEXUS_SECRET", os.getenv("X_NEXUS_SECRET", ""))
        }
        
        r = requests.post(ALPHA_EXEC_URL, json=payload, headers=headers, timeout=5)
        
        if r.status_code == 200:
            logger.info(f"✓ {ticker} {direction} dispatched to Alpha Execution")
            return True
        else:
            logger.error(f"✗ {ticker} dispatch failed: {r.status_code} {r.text[:100]}")
            return False
    except Exception as e:
        logger.error(f"✗ {ticker} dispatch error: {e}")
        return False

def poll_and_dispatch():
    """Main loop: poll for queued verdicts and dispatch."""
    logger.info("Verdict dispatch loop starting...")
    
    while True:
        try:
            conn = sqlite3.connect(ALPHA_BUFFER_DB, timeout=5)
            cursor = conn.cursor()
            
            # Get all queued STRONG_GO and GO verdicts
            cursor.execute("""
                SELECT id, ticker, direction, verdict, weighted_score, pathway, window_id
                FROM concordance_results
                WHERE omni_response = 'queued' AND verdict IN ('STRONG_GO', 'GO')
                AND created_at > datetime('now', '-1 hour')
                ORDER BY weighted_score DESC, created_at
            """)
            
            verdicts = cursor.fetchall()
            
            if verdicts:
                logger.info(f"Found {len(verdicts)} queued verdicts to dispatch")
                
                # Batch dispatch with rate limiting
                for batch_idx in range(0, len(verdicts), REQUEST_BATCH_SIZE):
                    batch = verdicts[batch_idx:batch_idx + REQUEST_BATCH_SIZE]
                    
                    for idx, (vid, ticker, direction, verdict, score, pathway, window_id) in enumerate(batch):
                        success = dispatch_verdict_to_execution(ticker, direction, score, pathway, window_id)
                        
                        if success:
                            # Mark as dispatched in buffer
                            cursor.execute(
                                "UPDATE concordance_results SET omni_response = 'executed' WHERE id = ?",
                                (vid,)
                            )
                        else:
                            logger.warning(f"Will retry {ticker} next cycle")
                        
                        # Delay between requests (except last in batch)
                        if idx < len(batch) - 1:
                            time.sleep(REQUEST_DELAY)
                    
                    # Delay between batches (respect downstream engine rate limits)
                    if batch_idx + REQUEST_BATCH_SIZE < len(verdicts):
                        logger.info(f"Rate limit: waiting {BATCH_WAIT_TIME}s before next batch...")
                        time.sleep(BATCH_WAIT_TIME)
                
                conn.commit()
            
            conn.close()
            
            # Poll every 30 seconds
            time.sleep(30)
            
        except Exception as e:
            logger.error(f"Dispatch loop error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    poll_and_dispatch()
