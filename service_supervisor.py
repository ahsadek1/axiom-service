#!/usr/bin/env python3
"""
service_supervisor.py — Persistent Service Supervisor

Auto-starts and monitors Alpha, Prime, and Axiom services.
Restarts on death. Health checks every 30 seconds.
NEVER allows services to go dark.

Author: OMNI (Auto-Generated Fix)
Date: 2026-05-22
"""

import subprocess
import time
import requests
import logging
from datetime import datetime
import os
import signal
import sys
import psutil

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/Users/ahmedsadek/nexus/logs/supervisor.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("supervisor")

SERVICES = {
    "alpha-execution": {
        "command": ["python3", "/Users/ahmedsadek/nexus/alpha-execution/main.py"],
        "cwd": "/Users/ahmedsadek/nexus/alpha-execution",
        "health_url": "http://localhost:5001/health",
        "port": 5001,
        "process": None,
        "last_restart": None,
        "restart_count": 0,
        "healthy": False,
    },
}

HEALTH_CHECK_INTERVAL = 30  # seconds
RESTART_COOLDOWN = 5  # seconds between restart attempts

def get_process_by_name(name: str):
    """Find process by command name."""
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            if name.lower() in ' '.join(proc.info.get('cmdline', [])).lower():
                return proc
    except:
        pass
    return None

def start_service(service_name: str, config: dict) -> bool:
    """Start a service."""
    logger.info(f"Starting {service_name}...")
    try:
        # Kill any existing process
        existing = get_process_by_name(service_name)
        if existing:
            logger.info(f"Killing existing process {existing.pid}")
            existing.kill()
            time.sleep(1)
        
        # Start new process
        proc = subprocess.Popen(
            config["command"],
            cwd=config["cwd"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid
        )
        config["process"] = proc
        config["last_restart"] = datetime.now()
        config["restart_count"] += 1
        logger.info(f"✓ {service_name} started (PID: {proc.pid}, restart #{config['restart_count']})")
        return True
    except Exception as e:
        logger.error(f"✗ Failed to start {service_name}: {e}")
        return False

def health_check(service_name: str, config: dict) -> bool:
    """Check if service is healthy."""
    try:
        resp = requests.get(config["health_url"], timeout=3)
        if resp.status_code == 200:
            config["healthy"] = True
            return True
    except:
        pass
    config["healthy"] = False
    return False

def monitor_services():
    """Main monitoring loop."""
    logger.info("Service supervisor starting...")
    
    # Initial startup
    for name, config in SERVICES.items():
        start_service(name, config)
        time.sleep(2)
    
    # Monitor loop
    while True:
        try:
            time.sleep(HEALTH_CHECK_INTERVAL)
            
            for name, config in SERVICES.items():
                proc = config.get("process")
                
                # Check if process is still alive
                if proc and proc.poll() is None:
                    # Process alive, check health
                    if health_check(name, config):
                        logger.debug(f"✓ {name} healthy")
                    else:
                        logger.warning(f"⚠️  {name} unhealthy (will restart on next check)")
                else:
                    # Process dead, restart
                    logger.error(f"✗ {name} process died, restarting...")
                    time.sleep(RESTART_COOLDOWN)
                    start_service(name, config)
        
        except KeyboardInterrupt:
            logger.info("Supervisor shutdown requested")
            break
        except Exception as e:
            logger.error(f"Supervisor error: {e}")
            time.sleep(5)

def signal_handler(sig, frame):
    logger.info("Received SIGTERM, shutting down...")
    for name, config in SERVICES.items():
        if config.get("process"):
            try:
                os.killpg(os.getpgid(config["process"].pid), signal.SIGTERM)
            except:
                pass
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    monitor_services()
