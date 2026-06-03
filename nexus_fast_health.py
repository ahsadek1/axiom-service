#!/usr/bin/env python3
"""
nexus_fast_health.py — Fast Health Check Wrapper

This is a wrapper script created 2026-05-13 to fulfill the cron mandate.
It invokes the actual health monitor (guardian-angel/nexus_health_monitor_v2_source.py)
with single-run mode.

Usage:
    python3 nexus_fast_health.py --once

Author: OMNI 🌐
"""

import sys
import os

# Add shared to path
sys.path.insert(0, "/Users/ahmedsadek/nexus/shared")
sys.path.insert(0, "/Users/ahmedsadek/nexus/guardian-angel")

# Import and run the health monitor
try:
    from nexus_health_monitor_v2_source import run_single_check
    
    # Run a single health check
    exit_code = run_single_check()
    sys.exit(exit_code)
    
except ImportError as e:
    print(f"ERROR: Could not import health monitor: {e}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"ERROR: Health check failed: {e}", file=sys.stderr)
    sys.exit(1)
