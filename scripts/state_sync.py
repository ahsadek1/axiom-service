#!/usr/bin/env python3
"""Continuous state sync daemon - updates nexus_state.json every 5 minutes"""

import json
import requests
import time
from datetime import datetime, timezone
import sys
import os

STATE_FILE = "/Users/ahmedsadek/.openclaw/workspace-shared/nexus_state.json"
LOG_FILE = "/Users/ahmedsadek/nexus/logs/state_sync.log"

def log(msg):
    ts = datetime.utcnow().isoformat() + "Z"
    line = f"[{ts}] {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except:
        pass

def update_state():
    """Update shared state file with current system status"""
    try:
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
    except:
        state = {"schema": "nexus.shared_state.v1"}
    
    now_iso = datetime.now(timezone.utc).isoformat()
    state["last_updated"] = now_iso
    state["updated_by"] = "state_sync_daemon"
    
    # Get actual positions from Alpaca
    try:
        resp = requests.get(
            "https://paper-api.alpaca.markets/v2/positions",
            headers={
                "APCA-API-KEY-ID": "PKPGM3BRNYPGCF5Z56IAUZCZJL",
                "APCA-API-SECRET-KEY": "5uVVmmB2dYnpA1SsTbkde8V2wixocBfAvGBsnrWSnJDs"
            },
            timeout=5
        )
        positions = resp.json() if resp.status_code == 200 else []
    except Exception as e:
        log(f"ERROR fetching positions: {e}")
        positions = []
    
    # Get synthesis health
    try:
        resp = requests.get("http://localhost:8004/health", timeout=3)
        synth = resp.json() if resp.status_code == 200 else {}
        synth_status = synth.get("status", "unknown")
    except:
        synth = {}
        synth_status = "unreachable"
    
    state["open_positions"] = positions
    state["open_positions_count"] = len(positions)
    state["system_health"] = {
        **state.get("system_health", {}),
        "as_of": now_iso,
        "open_positions_count": len(positions),
        "synthesis_service": synth_status,
        "scanner_active": synth.get("scanner_active", False),
        "vix": synth.get("market_state", {}).get("vix", 0),
        "regime": synth.get("market_state", {}).get("regime", "UNKNOWN"),
        "pipeline_flow": "LIVE" if synth_status == "healthy" else "DEGRADED"
    }
    
    # Write state
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    
    log(f"State synced: {len(positions)} positions, {synth_status}")

def main():
    log("=== STATE_SYNC DAEMON START ===")
    while True:
        try:
            update_state()
        except Exception as e:
            log(f"ERROR in state update: {e}")
        
        # Sleep 5 minutes
        time.sleep(300)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("=== STATE_SYNC DAEMON STOPPED ===")
        sys.exit(0)
