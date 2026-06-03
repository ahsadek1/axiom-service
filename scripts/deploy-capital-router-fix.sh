#!/bin/bash
# Deploy Capital Router FIX#1, FIX#2, FIX#3 (2026-06-02 09:35 AM)

set -e

echo "=== GENESIS Capital Router Fix Deployment ===" 
echo "$(date '+%Y-%m-%d %H:%M:%S') ET"
echo ""

# Verify code changes
echo "[1/4] Verifying code syntax..."
cd /Users/ahmedsadek/nexus/capital-router
python3 -m py_compile main.py
if [ $? -eq 0 ]; then
    echo "✅ Syntax check passed"
else
    echo "❌ Syntax error detected"
    exit 1
fi
echo ""

# Backup current database state
echo "[2/4] Backing up capital_router.db and nexus_state.json..."
cp /Users/ahmedsadek/nexus/data/capital_router.db /Users/ahmedsadek/nexus/data/backups/capital_router_pre-fix_$(date +%Y%m%d_%H%M%S).db
cp /Users/ahmedsadek/nexus/data/nexus_state.json /Users/ahmedsadek/nexus/data/backups/nexus_state_pre-fix_$(date +%Y%m%d_%H%M%S).json
echo "✅ Backups created"
echo ""

# Restart Capital Router service
echo "[3/4] Restarting Capital Router service..."
launchctl unload ~/Library/LaunchAgents/ai.nexus.capital-router.plist
sleep 2
launchctl load ~/Library/LaunchAgents/ai.nexus.capital-router.plist
echo "✅ Service restarted"
echo ""

# Wait for service to stabilize + verify startup logs
echo "[4/4] Verifying startup sync logs..."
sleep 3
tail -20 /Users/ahmedsadek/nexus/logs/capital-router.log | grep -E "STARTUP_SYNC|Alpaca credentials"

echo ""
echo "✅ Deployment complete. Capital Router FIX#1/2/3 live."
echo ""
echo "Summary of changes:"
echo "  FIX#1: Synchronous startup sync (prevents concurrent request race)"
echo "  FIX#2: Alpaca credential validation (detects missing fallback early)"
echo "  FIX#3: Pessimistic lock expiry (prevents orphan allocations)"
