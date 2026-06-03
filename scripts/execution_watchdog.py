#!/usr/bin/env python3
"""
OMNI Execution Watchdog — selftest compliance check
Ensures all Railway services are responsive before market open
"""
import sys
import subprocess
import json
from datetime import datetime

def selftest():
    """Run selftest on all Railway services"""
    checks = {
        "alpha": ("https://worker-production-2060.up.railway.app/health", "loop_active"),
        "prime": ("https://nexus-prime-bot-production.up.railway.app/health", "status"),
        "axiom": ("https://axiom-production-334c.up.railway.app/health", "status"),
    }
    
    results = []
    for service, (url, field) in checks.items():
        try:
            result = subprocess.run(
                ["curl", "-s", "-m", "5", url],
                capture_output=True,
                text=True
            )
            data = json.loads(result.stdout)
            
            # Check if field exists and is truthy
            val = data.get(field, data.get("status"))
            status = "PASS" if val and val != "unhealthy" else "FAIL"
            results.append(f"{service.upper()}: {status}")
        except Exception as e:
            results.append(f"{service.upper()}: FAIL ({str(e)[:30]})")
    
    print("\n".join(results))
    all_pass = all("PASS" in r for r in results)
    
    if all_pass:
        print("\n✅ PASSED")
        return 0
    else:
        print("\n❌ FAILED")
        return 1

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    else:
        print("Usage: execution_watchdog.py --selftest")
        sys.exit(1)
