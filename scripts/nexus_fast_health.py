#!/usr/bin/env python3
"""
OMNI Railway Health Monitor — Fast Health Check (10-minute intervals)
Monitors Railway service availability for Nexus Alpha & Prime
Reports critical failures to Ahmed DM only
"""

import subprocess
import sys
import json
import os
from datetime import datetime
from pathlib import Path

# Railway service IDs from TOOLS.md
SERVICES = {
    "alpha": "2fb13f48-38a1-439e-8305-fcf28e226804",
    "prime": "80a1bf61-89af-49cb-9d7f-ed2d3d2ebac0"
}

RAILWAY_TOKEN = "26fd3a51-5484-466e-8df5-01af6de9f0c1"

def check_railway_service(service_id, service_name):
    """Check if a Railway service is running"""
    cmd = [
        "curl", "-s",
        f"https://api.railway.app/graphql",
        "-H", f"Authorization: Bearer {RAILWAY_TOKEN}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({
            "query": f"""
            query {{
              project(id: "{service_id}") {{
                services {{
                  edges {{
                    node {{
                      id
                      name
                      isActive
                    }}
                  }}
                }}
              }}
            }}
            """
        })
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        response = json.loads(result.stdout)
        
        if "data" in response and response["data"]:
            is_active = response["data"].get("project", {}).get("services", {}).get("edges", [])
            if is_active:
                return True, "OK"
            else:
                return False, "Service not found"
        else:
            return False, str(response.get("errors", ["Unknown error"]))
    except Exception as e:
        return False, str(e)

def check_service_health(service_id, service_name):
    """Check service via /health endpoint"""
    try:
        result = subprocess.run(
            ["curl", "-s", "-f", "-m", "5", f"https://{service_name}.up.railway.app/health"],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0, result.stdout or result.stderr
    except Exception as e:
        return False, str(e)

def main():
    print(f"[{datetime.now().isoformat()}] OMNI Railway Health Check --once")
    
    failed = []
    results = {}
    
    for service_name, service_id in SERVICES.items():
        print(f"  Checking {service_name}...", end=" ", flush=True)
        
        is_healthy, msg = check_service_health(service_id, service_name)
        
        if is_healthy:
            print("✅ OK")
            results[service_name] = "healthy"
        else:
            print(f"❌ DOWN ({msg[:50]})")
            results[service_name] = "down"
            failed.append(service_name)
    
    # Summary
    print("\n" + "="*50)
    print(f"Health Summary: {len(SERVICES)-len(failed)}/{len(SERVICES)} services running")
    print("="*50)
    
    if failed:
        print(f"\n🚨 CRITICAL: {len(failed)} service(s) DOWN: {', '.join(failed)}")
        print("\nTriggering auto-redeploy...")
        
        # Trigger redeploy for failed services
        for svc in failed:
            redeploy_service(svc)
        
        # Report to Ahmed DM
        try:
            from telegram import notify_critical
            notify_critical(f"🚨 NEXUS CRITICAL: {len(failed)} Railway service(s) DOWN\n{', '.join(failed)}")
        except:
            print("⚠️  Could not send alert to Ahmed DM")
        
        return 1
    
    print("\n✅ All systems nominal")
    return 0

def redeploy_service(service_name):
    """Trigger redeploy via revive_services.py"""
    try:
        revive_path = Path("/Users/ahmedsadek/nexus/scripts/revive_services.py")
        if revive_path.exists():
            subprocess.run(
                ["python3", str(revive_path), service_name],
                timeout=60,
                capture_output=True
            )
            print(f"  → Redeploy triggered for {service_name}")
    except Exception as e:
        print(f"  ⚠️  Redeploy failed for {service_name}: {e}")

if __name__ == "__main__":
    sys.exit(main())
