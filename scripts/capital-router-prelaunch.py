#!/usr/bin/env python3
"""
Pre-launch cleanup for Capital Router
Kills any lingering process on port 8000 to avoid TIME_WAIT binding failures

Primus 2026-05-30
"""

import subprocess
import time
import sys

PORT = 8000

def kill_port(port, timeout=10):
    """Kill any process using the given port."""
    for attempt in range(timeout):
        # Find PIDs using the port
        try:
            result = subprocess.run(
                ['lsof', '-ti', f':{port}'],
                capture_output=True, text=True, timeout=3
            )
            if result.stdout.strip():
                pids = result.stdout.strip().split('\n')
                for pid in pids:
                    if pid:
                        print(f"Killing PID {pid} on port {port}")
                        subprocess.run(['kill', '-9', pid], timeout=1)
            else:
                print(f"Port {port} is free")
                return True
        except Exception as e:
            print(f"Error checking port: {e}")
        
        time.sleep(1)
    
    return False

if __name__ == "__main__":
    print(f"Pre-launch cleanup: Clearing port {PORT}...")
    if kill_port(PORT):
        print("Port cleared successfully")
        sys.exit(0)
    else:
        print(f"WARNING: Port {PORT} still in use (may be in TIME_WAIT state)")
        # Don't fail — let the service try anyway
        time.sleep(2)
        sys.exit(0)
