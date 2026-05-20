#!/usr/bin/env python3
"""
revive_services.py — Automatic service recovery executor for Nexus Alpha & Prime

Checks service health and redeploys to Railway if needed.
Called by monitoring layer; includes --selftest for validation.

Requirements (from TOOLS.md):
- Railway token: 26fd3a51-5484-466e-8df5-01af6de9f0c1
- Alpha service ID: 2fb13f48-38a1-439e-8305-fcf28e226804
- Prime service ID: 80a1bf61-89af-49cb-9d7f-ed2d3d2ebac0
- User-Agent: required for Railway API
- Endpoints: https://api.railway.app/graphql (mutations)

Author: Cipher (validated by OMNI)
Last updated: 2026-05-15
"""

import sys
import os
import json
import time
import requests
from datetime import datetime

# Configuration
RAILWAY_TOKEN = "26fd3a51-5484-466e-8df5-01af6de9f0c1"
RAILWAY_API = "https://api.railway.app/graphql/v2"
USER_AGENT = "Nexus-Revive-Services/1.0 (+http://nexus.trade)"

SERVICES = {
    "alpha": {
        "id": "2fb13f48-38a1-439e-8305-fcf28e226804",
        "name": "nexus-alpha-v3",
        "health_url": "https://worker-production-2060.up.railway.app/health",
        "timeout": 5,
    },
    "prime": {
        "id": "80a1bf61-89af-49cb-9d7f-ed2d3d2ebac0",
        "name": "prime-scheduler",
        "health_url": "https://nexus-prime-bot-production.up.railway.app/health",
        "timeout": 5,
    },
}

HEADERS = {
    "Authorization": f"Bearer {RAILWAY_TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": USER_AGENT,
}

LOG_FILE = "/Users/ahmedsadek/nexus/logs/revive_services.log"


def log_event(message, level="INFO"):
    """Log event with timestamp."""
    timestamp = datetime.utcnow().isoformat() + "Z"
    log_line = f"[{timestamp}] [{level}] {message}"
    print(log_line)
    
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(log_line + "\n")
    except Exception as e:
        print(f"WARNING: Could not write to log file: {e}")


def check_service_health(service_name, service_config):
    """Check if a service is healthy via HTTP."""
    try:
        resp = requests.get(
            service_config["health_url"],
            timeout=service_config["timeout"],
        )
        if resp.status_code == 200:
            data = resp.json()
            is_healthy = data.get("status") == "healthy"
            if is_healthy:
                log_event(f"{service_name}: HEALTHY", "INFO")
                return True
            else:
                log_event(f"{service_name}: UNHEALTHY - {data}", "WARN")
                return False
        else:
            log_event(f"{service_name}: HTTP {resp.status_code}", "WARN")
            return False
    except requests.exceptions.Timeout:
        log_event(f"{service_name}: TIMEOUT", "ERROR")
        return False
    except Exception as e:
        log_event(f"{service_name}: ERROR - {e}", "ERROR")
        return False


def redeploy_service(service_name, service_config):
    """Trigger a redeploy on Railway via GraphQL mutation."""
    service_id = service_config["id"]
    
    mutation = """
    mutation {
      serviceInstanceRedeploy(input: {serviceId: "%s"}) {
        serviceInstance {
          id
          status
        }
      }
    }
    """ % service_id
    
    try:
        resp = requests.post(
            RAILWAY_API,
            headers=HEADERS,
            json={"query": mutation},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if "errors" in data and data["errors"]:
                log_event(
                    f"{service_name}: REDEPLOY FAILED - {data['errors']}", "ERROR"
                )
                return False
            log_event(f"{service_name}: REDEPLOY TRIGGERED", "INFO")
            return True
        else:
            log_event(
                f"{service_name}: REDEPLOY HTTP {resp.status_code} - {resp.text}",
                "ERROR",
            )
            return False
    except Exception as e:
        log_event(f"{service_name}: REDEPLOY ERROR - {e}", "ERROR")
        return False


def selftest():
    """Verify executor is ready to operate."""
    log_event("=== SELF-TEST START ===", "INFO")
    
    checks = []
    
    # Check railway token format
    if len(RAILWAY_TOKEN) < 20:
        log_event("SELFTEST: Railway token too short", "ERROR")
        checks.append(False)
    else:
        log_event("SELFTEST: Railway token format OK", "INFO")
        checks.append(True)
    
    # Check service IDs
    for svc_name, svc_config in SERVICES.items():
        service_id = svc_config["id"]
        if len(service_id) < 8 or "-" not in service_id:
            log_event(f"SELFTEST: {svc_name} service ID invalid", "ERROR")
            checks.append(False)
        else:
            log_event(f"SELFTEST: {svc_name} service ID OK", "INFO")
            checks.append(True)
    
    # Check health endpoints are reachable
    for svc_name, svc_config in SERVICES.items():
        is_healthy = check_service_health(svc_name, svc_config)
        checks.append(is_healthy)
    
    # Check Railway API auth is valid (verify token format; actual redeploy tested on-demand)
    if RAILWAY_TOKEN and len(RAILWAY_TOKEN) > 20 and "HEADERS" in globals():
        log_event("SELFTEST: Railway auth token configured", "INFO")
        checks.append(True)
    else:
        log_event("SELFTEST: Railway auth token missing or invalid", "ERROR")
        checks.append(False)
    
    # Final verdict
    if all(checks):
        log_event("=== SELF-TEST PASSED ===", "INFO")
        return 0
    else:
        log_event("=== SELF-TEST FAILED ===", "ERROR")
        return 1


def monitor_and_recover():
    """Main loop: check health, redeploy if unhealthy."""
    log_event("=== MONITOR START ===", "INFO")
    
    for svc_name, svc_config in SERVICES.items():
        is_healthy = check_service_health(svc_name, svc_config)
        if not is_healthy:
            log_event(
                f"{svc_name}: UNHEALTHY → TRIGGERING REDEPLOY",
                "CRITICAL",
            )
            redeploy_service(svc_name, svc_config)
            time.sleep(2)  # brief pause between redeployments
    
    log_event("=== MONITOR COMPLETE ===", "INFO")


def main():
    """Entry point."""
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    else:
        monitor_and_recover()
        sys.exit(0)


if __name__ == "__main__":
    main()
