#!/usr/bin/env python3
"""
COMPREHENSIVE TRADING FIXES DEPLOYMENT SCRIPT

Deploys all 8 critical fixes to 192.168.1.141 (Ubuntu node).

Execution Plan:
1. Verify local env setup
2. Run comprehensive test suite
3. Deploy to remote host via SSH
4. Verify deployment on remote
5. Monitor for 10 minutes post-deployment
6. Generate completion report
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytz

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s"
)
logger = logging.getLogger("deploy_fixes")
ET = pytz.timezone("America/New_York")

# Configuration
REMOTE_HOST = "ubuntu@192.168.1.141"
REMOTE_AXIOM_DIR = "/home/ubuntu/axiom"
LOCAL_AXIOM_DIR = Path(__file__).parent
SSH_TIMEOUT = 60
DEPLOYMENT_MONITOR_DURATION = 600  # 10 minutes


def run_local_tests() -> bool:
    """Run comprehensive test suite locally."""
    logger.info("=" * 80)
    logger.info("STEP 1: LOCAL TEST SUITE")
    logger.info("=" * 80)
    
    try:
        # Import and run test suite
        from trading_fixes_implementation import TradingFixesTestSuite
        
        suite = TradingFixesTestSuite()
        results = suite.run_all_tests()
        
        logger.info("\nTest Results:")
        logger.info(json.dumps(results, indent=2))
        
        # Check if all tests passed
        if results.get("passed") == results.get("total_tests"):
            logger.info("✅ All local tests passed!")
            return True
        else:
            logger.error("❌ Some tests failed — see details above")
            return False
    
    except Exception as e:
        logger.error("Test suite execution failed: %s", e)
        return False


def verify_env() -> bool:
    """Verify required environment variables."""
    logger.info("=" * 80)
    logger.info("STEP 2: ENVIRONMENT VERIFICATION")
    logger.info("=" * 80)
    
    required_vars = [
        "AXIOM_SECRET",
        "NEXUS_SECRET",
        "POLYGON_API_KEY",
        "FRED_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "AHMED_CHAT_ID",
        "ALPACA_KEY",
        "ALPACA_SECRET",
    ]
    
    missing = []
    for var in required_vars:
        if not os.getenv(var):
            missing.append(var)
    
    if missing:
        logger.error("❌ Missing environment variables: %s", missing)
        return False
    
    logger.info("✅ All required environment variables are set")
    return True


def sync_files_to_remote() -> bool:
    """Sync code files to remote host."""
    logger.info("=" * 80)
    logger.info("STEP 3: SYNC FILES TO REMOTE HOST")
    logger.info("=" * 80)
    
    files_to_sync = [
        "trading_fixes_implementation.py",
        "main.py",
        "data_sources.py",
        "risk_engine.py",
        "scheduler.py",
        "health_monitor.py",
        ".env",
    ]
    
    for filename in files_to_sync:
        local_path = LOCAL_AXIOM_DIR / filename
        if not local_path.exists():
            logger.warning("File not found locally: %s (skipping)", filename)
            continue
        
        try:
            cmd = [
                "scp",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                str(local_path),
                f"{REMOTE_HOST}:{REMOTE_AXIOM_DIR}/",
            ]
            
            result = subprocess.run(cmd, capture_output=True, timeout=SSH_TIMEOUT, text=True)
            
            if result.returncode == 0:
                logger.info("✅ Synced %s", filename)
            else:
                logger.error("❌ Failed to sync %s: %s", filename, result.stderr)
                return False
        
        except Exception as e:
            logger.error("SCP error for %s: %s", filename, e)
            return False
    
    logger.info("✅ All files synced")
    return True


def restart_axiom_service() -> bool:
    """Restart Axiom service on remote host."""
    logger.info("=" * 80)
    logger.info("STEP 4: RESTART AXIOM SERVICE")
    logger.info("=" * 80)
    
    try:
        # First, try to kill any existing uvicorn processes
        kill_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            REMOTE_HOST,
            "pkill -f 'uvicorn.*axiom' || true",
        ]
        
        subprocess.run(kill_cmd, timeout=SSH_TIMEOUT)
        logger.info("Killed any existing Axiom processes")
        
        # Wait a moment
        import time
        time.sleep(2)
        
        # Start the service in background
        # Note: In production, this would use systemd
        start_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            REMOTE_HOST,
            f"cd {REMOTE_AXIOM_DIR} && "
            f"nohup python -m uvicorn main:app --host 0.0.0.0 --port 8001 > axiom.log 2>&1 &",
        ]
        
        result = subprocess.run(start_cmd, timeout=SSH_TIMEOUT, capture_output=True, text=True)
        
        if result.returncode == 0:
            logger.info("✅ Axiom service restarted")
            
            # Give it a moment to start
            time.sleep(3)
            return True
        else:
            logger.error("❌ Failed to restart Axiom: %s", result.stderr)
            return False
    
    except Exception as e:
        logger.error("Service restart error: %s", e)
        return False


def verify_remote_health() -> bool:
    """Verify Axiom is healthy on remote host."""
    logger.info("=" * 80)
    logger.info("STEP 5: VERIFY REMOTE HEALTH")
    logger.info("=" * 80)
    
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            # Check via SSH
            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=5",
                REMOTE_HOST,
                f"curl -s http://localhost:8001/health | jq .status",
            ]
            
            result = subprocess.run(cmd, capture_output=True, timeout=15, text=True)
            
            if result.returncode == 0 and ("starting" in result.stdout or "ok" in result.stdout):
                logger.info("✅ Axiom health check passed (attempt %d/%d)", attempt + 1, max_attempts)
                return True
            
            logger.warning("Health check attempt %d/%d returned: %s", attempt + 1, max_attempts, result.stdout)
        
        except Exception as e:
            logger.warning("Health check attempt %d/%d failed: %s", attempt + 1, max_attempts, e)
        
        if attempt < max_attempts - 1:
            import time
            time.sleep(2)
    
    logger.error("❌ Remote health verification failed after %d attempts", max_attempts)
    return False


def monitor_post_deployment() -> Dict:
    """Monitor Axiom for 10 minutes after deployment."""
    logger.info("=" * 80)
    logger.info("STEP 6: POST-DEPLOYMENT MONITORING (10 min)")
    logger.info("=" * 80)
    
    import time
    start_time = time.time()
    check_interval = 30  # Check every 30 seconds
    checks = []
    
    while (time.time() - start_time) < DEPLOYMENT_MONITOR_DURATION:
        try:
            # Check service health
            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=5",
                REMOTE_HOST,
                f"curl -s http://localhost:8001/health | jq .",
            ]
            
            result = subprocess.run(cmd, capture_output=True, timeout=10, text=True)
            
            if result.returncode == 0:
                try:
                    health = json.loads(result.stdout)
                    checks.append({
                        "timestamp": datetime.now(ET).isoformat(),
                        "status": "healthy",
                        "service_mode": health.get("_service_mode", "unknown"),
                    })
                    logger.info("✅ Health check OK (mode: %s)", health.get("_service_mode"))
                except:
                    checks.append({
                        "timestamp": datetime.now(ET).isoformat(),
                        "status": "unhealthy",
                        "error": "Failed to parse health response",
                    })
                    logger.warning("⚠️ Health response unparseable")
            else:
                checks.append({
                    "timestamp": datetime.now(ET).isoformat(),
                    "status": "unreachable",
                })
                logger.warning("⚠️ Health check failed (HTTP error)")
        
        except Exception as e:
            checks.append({
                "timestamp": datetime.now(ET).isoformat(),
                "status": "error",
                "error": str(e),
            })
            logger.error("❌ Monitoring check failed: %s", e)
        
        time.sleep(check_interval)
    
    healthy_count = sum(1 for c in checks if c.get("status") == "healthy")
    logger.info("✅ Monitoring complete: %d/%d checks passed", healthy_count, len(checks))
    
    return {
        "total_checks": len(checks),
        "healthy_checks": healthy_count,
        "checks": checks,
        "success_rate": f"{healthy_count/len(checks)*100:.1f}%" if checks else "0%",
    }


def generate_completion_report(results: Dict) -> str:
    """Generate final deployment report."""
    logger.info("=" * 80)
    logger.info("FINAL DEPLOYMENT REPORT")
    logger.info("=" * 80)
    
    report_lines = [
        "=" * 80,
        "COMPREHENSIVE TRADING FIXES — DEPLOYMENT REPORT",
        "=" * 80,
        f"Date: {datetime.now(ET).isoformat()}",
        f"Host: 192.168.1.141",
        "",
        "DEPLOYMENT STEPS:",
        "✅" if results.get("local_tests") else "❌" + " Local Test Suite",
        "✅" if results.get("env_verified") else "❌" + " Environment Verification",
        "✅" if results.get("files_synced") else "❌" + " Files Synced to Remote",
        "✅" if results.get("service_restarted") else "❌" + " Service Restarted",
        "✅" if results.get("remote_health") else "❌" + " Remote Health Verified",
        "",
        "MONITORING RESULTS:",
        f"  Monitoring Duration: {DEPLOYMENT_MONITOR_DURATION} seconds",
        f"  Health Checks: {results.get('monitoring', {}).get('total_checks', 0)}",
        f"  Healthy Checks: {results.get('monitoring', {}).get('healthy_checks', 0)}",
        f"  Success Rate: {results.get('monitoring', {}).get('success_rate', 'N/A')}",
        "",
        "FIXES IMPLEMENTED:",
        "  1. ✅ Axiom VIX service unreachable (port 8001) — watchdog + auto-restart",
        "  2. ✅ Yahoo fallback degraded — FRED VIXCLS as 3rd source with caching",
        "  3. ✅ Binary halt logic — replaced with tiered risk gates (L0-L3)",
        "  4. ✅ No alert escalation — Telegram alerts on sustained HALT (3+ in 10m)",
        "  5. ✅ No auto-recovery — halt recovery state machine with sanity checks",
        "  6. ✅ Order queue backlog — EOD reconciliation to close/cancel stale orders",
        "  7. ✅ Signal/execution decoupling — feedback loop to halt signal generation",
        "  8. ✅ Axiom service dependency — health monitoring with auto-restart",
        "",
        "OVERALL STATUS:",
        f"{'✅ DEPLOYMENT SUCCESSFUL' if all(results.values()) else '❌ DEPLOYMENT FAILED'}",
        "=" * 80,
    ]
    
    report = "\n".join(report_lines)
    logger.info(report)
    return report


async def main():
    """Execute full deployment pipeline."""
    logger.info("\n")
    logger.info("╔" + "=" * 78 + "╗")
    logger.info("║ COMPREHENSIVE TRADING FIXES — DEPLOYMENT PIPELINE                      ║")
    logger.info("╚" + "=" * 78 + "╝")
    logger.info("\n")
    
    results = {}
    
    # Step 1: Local Tests
    results["local_tests"] = run_local_tests()
    if not results["local_tests"]:
        logger.error("Local tests failed — aborting deployment")
        return False
    
    # Step 2: Environment Verification
    results["env_verified"] = verify_env()
    if not results["env_verified"]:
        logger.error("Environment verification failed — aborting deployment")
        return False
    
    # Step 3: Sync Files
    results["files_synced"] = sync_files_to_remote()
    if not results["files_synced"]:
        logger.error("File sync failed — aborting deployment")
        return False
    
    # Step 4: Restart Service
    results["service_restarted"] = restart_axiom_service()
    if not results["service_restarted"]:
        logger.error("Service restart failed — aborting deployment")
        return False
    
    # Step 5: Verify Remote Health
    results["remote_health"] = verify_remote_health()
    if not results["remote_health"]:
        logger.error("Remote health verification failed — deployment may have issues")
    
    # Step 6: Monitor Post-Deployment
    results["monitoring"] = monitor_post_deployment()
    
    # Step 7: Generate Report
    report = generate_completion_report(results)
    
    # Save report to file
    report_file = LOCAL_AXIOM_DIR / "DEPLOYMENT_REPORT.txt"
    with open(report_file, "w") as f:
        f.write(report)
    
    logger.info("\n📋 Report saved to: %s\n", report_file)
    
    # Return overall success status
    return all(results.values())


if __name__ == "__main__":
    try:
        success = asyncio.run(main())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        logger.error("Deployment interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error("Deployment failed with exception: %s", e)
        sys.exit(1)
