#!/usr/bin/env python3
"""
validate_env_startup.py — Validate all required environment variables at service startup

This script is called by each service's startup (before main.py runs) to:
1. Check that all required env vars are set
2. Load from .env file if not already set
3. Alert and fail hard if critical vars are missing
4. Log what was loaded

Usage: python3 validate_env_startup.py <service_name>
Example: python3 validate_env_startup.py alpha-execution
"""

import os
import sys
from pathlib import Path

# Service-specific required variables
REQUIRED_VARS = {
    "alpha-execution": [
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "ALPHA_EXEC_DB_PATH",
        "ALPHA_BUFFER_URL",
    ],
    "prime-execution": [
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
    ],
    "omni": [
        "RAILWAY_WEBHOOK_SECRET",
        "ALPHA_BUFFER_URL",
        "PRIME_BUFFER_URL",
    ],
    "cipher": [
        "ANTHROPIC_API_KEY",
        "ALPHA_BUFFER_URL",
        "PRIME_BUFFER_URL",
    ],
    "sage": [
        "FRED_API_KEY",
        "ALPHA_BUFFER_URL",
        "PRIME_BUFFER_URL",
    ],
    "atlas": [
        "ORATS_API_KEY",
        "ALPHA_BUFFER_URL",
        "PRIME_BUFFER_URL",
    ],
}

def load_from_dotenv(service_name: str) -> None:
    """Load variables from service .env file."""
    env_file = f"/Users/ahmedsadek/nexus/{service_name}/.env"
    if not os.path.exists(env_file):
        return
    
    with open(env_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
                    print(f"  Loaded: {key}=***")

def validate(service_name: str) -> bool:
    """Validate all required vars are set. Return True if all OK."""
    required = REQUIRED_VARS.get(service_name, [])
    if not required:
        print(f"[WARN] No validation rules for {service_name}")
        return True
    
    missing = []
    for var in required:
        if var not in os.environ:
            missing.append(var)
    
    if missing:
        print(f"[CRITICAL] {service_name} startup FAILED — missing variables:")
        for var in missing:
            print(f"  - {var}")
        return False
    
    print(f"[OK] {service_name} env validation PASSED")
    return True

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 validate_env_startup.py <service_name>")
        sys.exit(1)
    
    service = sys.argv[1]
    print(f"[validate_env_startup] Checking {service}...")
    
    # Load from .env
    load_from_dotenv(service)
    
    # Validate
    if not validate(service):
        sys.exit(1)
    
    sys.exit(0)
