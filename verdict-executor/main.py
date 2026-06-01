#!/usr/bin/env python3
"""
verdict-executor — Verdict Execution Loop  
==========================================
Pulls GO verdicts from alpha_buffer.db concordance_results.
Submits them to alpha-execution /execute endpoint.
Marks as dispatched upon success.

This closes the gap between synthesis (alpha_buffer) and execution (Alpha 8005).

Run: python3 /Users/ahmedsadek/nexus/verdict-executor/main.py
"""

import sys
import sqlite3
import logging
import time
import os
from typing import Optional, Dict, List

sys.path.insert(0, "/Users/ahmedsadek/nexus")

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] verdict_executor: %(message)s",
)
logger = logging.getLogger("verdict_executor")

ALPHA_BUFFER_DB = "/Users/ahmedsadek/nexus/data/alpha_buffer.db"
ALPHA_EXEC_URL = "http://localhost:8005"
NEXUS_SECRET = os.getenv("NEXUS_SECRET", "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2")
POLL_INTERVAL = 10  # Check every 10 seconds
HEADERS = {"X-Nexus-Secret": NEXUS_SECRET, "Content-Type": "application/json"}


def get_db():
    conn = sqlite3.connect(ALPHA_BUFFER_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def fetch_pending_verdicts(limit: int = 50) -> List[Dict]:
    """Fetch GO verdicts that haven't been dispatched to alpha-execution yet."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, ticker, direction, pathway, window_id, weighted_score, agents_involved "
            "FROM concordance_results "
            "WHERE verdict IN ('GO', 'STRONG_GO') AND omni_dispatched=0 "
            "ORDER BY created_at ASC "
            "LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError as e:
        logger.warning("DB locked (transient): %s", e)
        return []
    except Exception as e:
        logger.error("Error fetching verdicts: %s", e)
        return []


def execute_verdict(verdict: Dict) -> bool:
    """Submit verdict to alpha-execution /execute."""
    try:
        payload = {
            "ticker": verdict["ticker"],
            "direction": verdict["direction"].lower(),
            "pathway": verdict.get("pathway", "P1"),
            "window_id": verdict.get("window_id", ""),
            "strategy": "options",
            "arena": "alpha",
            "sizing_mult": 1.0,
        }
        
        r = requests.post(
            f"{ALPHA_EXEC_URL}/execute",
            json=payload,
            headers=HEADERS,
            timeout=15,
        )
        
        if r.status_code == 200:
            logger.info(
                "[EXECUTE] %s %s (score=%.1f, pathway=%s) — SUCCESS",
                verdict["ticker"],
                verdict["direction"],
                verdict.get("weighted_score", 0),
                verdict.get("pathway", "?"),
            )
            return True
        else:
            logger.warning(
                "[EXECUTE] %s %s — FAILED (HTTP %d): %s",
                verdict["ticker"],
                verdict["direction"],
                r.status_code,
                r.text[:200],
            )
            return False
            
    except requests.Timeout:
        logger.warning("[EXECUTE] %s %s — TIMEOUT", verdict["ticker"], verdict["direction"])
        return False
    except Exception as e:
        logger.error("[EXECUTE] %s %s — ERROR: %s", verdict["ticker"], verdict["direction"], e)
        return False


def mark_dispatched(verdict_id: int, response: str = "HTTP 200") -> bool:
    """Mark verdict as dispatched in DB."""
    try:
        conn = get_db()
        conn.execute(
            "UPDATE concordance_results SET omni_dispatched=1, omni_response=? WHERE id=?",
            (response, verdict_id)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error("Error marking dispatched (id=%d): %s", verdict_id, e)
        return False


def poll_and_execute():
    """Main polling loop."""
    logger.info("Starting verdict executor (alpha-buffer → alpha-execution 8005)")
    logger.info("Pulling unexecuted GO verdicts from concordance_results...")
    
    execution_count = 0
    
    while True:
        try:
            verdicts = fetch_pending_verdicts(limit=50)
            if verdicts:
                logger.info("Found %d pending GO verdicts", len(verdicts))
                
                for verdict in verdicts:
                    if execute_verdict(verdict):
                        mark_dispatched(verdict["id"], "HTTP 200")
                        execution_count += 1
                    else:
                        mark_dispatched(verdict["id"], "HTTP_FAILED")
                    time.sleep(0.5)  # Rate limit - don't hammer Alpha
                
                logger.info("Execution cycle complete. Total executed this session: %d", execution_count)
            
            time.sleep(POLL_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("Shutdown requested. Total executed: %d", execution_count)
            break
        except Exception as e:
            logger.error("Unexpected error in main loop: %s", e)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    poll_and_execute()
