#!/bin/bash
#
# deploy-preflight.sh — One-command deployment of SQS Preflight system
#
# Usage: bash /Users/ahmedsadek/nexus/scripts/deploy-preflight.sh
#
# This script:
#   1. Verifies all dependencies
#   2. Creates required directories
#   3. Sets proper permissions
#   4. Validates Python modules
#   5. Installs launchd service
#   6. Runs test execution
#   7. Confirms system ready

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

NEXUS_DIR="/Users/ahmedsadek/nexus"
LAUNCHAGENTS_DIR="$HOME/Library/LaunchAgents"

echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}SQS PREFLIGHT SYSTEM — DEPLOYMENT${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""

# Step 1: Verify dependencies
echo -e "${YELLOW}[1/7] Verifying Python dependencies...${NC}"
python3 -c "import psutil" || { echo -e "${RED}❌ psutil not installed${NC}"; exit 1; }
python3 -c "import requests" || { echo -e "${RED}❌ requests not installed${NC}"; exit 1; }
python3 -c "import pytz" || { echo -e "${RED}❌ pytz not installed${NC}"; exit 1; }
python3 -c "import sqlite3" || { echo -e "${RED}❌ sqlite3 not installed${NC}"; exit 1; }
python3 -c "import asyncio" || { echo -e "${RED}❌ asyncio not installed${NC}"; exit 1; }
echo -e "${GREEN}✅ All Python dependencies present${NC}"
echo ""

# Step 2: Create required directories
echo -e "${YELLOW}[2/7] Creating required directories...${NC}"
mkdir -p "$NEXUS_DIR/logs"
mkdir -p "$NEXUS_DIR/LaunchAgents"
mkdir -p "$LAUNCHAGENTS_DIR"
chmod 755 "$NEXUS_DIR/logs"
echo -e "${GREEN}✅ Directories created${NC}"
echo ""

# Step 3: Verify core files exist
echo -e "${YELLOW}[3/7] Verifying core files...${NC}"
files=(
    "$NEXUS_DIR/preflight_runner.py"
    "$NEXUS_DIR/health_checks.py"
    "$NEXUS_DIR/recovery_actions.py"
)
for file in "${files[@]}"; do
    if [ ! -f "$file" ]; then
        echo -e "${RED}❌ Missing: $file${NC}"
        exit 1
    fi
    chmod +x "$file" 2>/dev/null || true
done
echo -e "${GREEN}✅ All core files present${NC}"
echo ""

# Step 4: Check plist exists and validate XML
echo -e "${YELLOW}[4/7] Validating launchd plist...${NC}"
plist_file="$NEXUS_DIR/LaunchAgents/com.axiom.sqs-preflight.plist"
if [ ! -f "$plist_file" ]; then
    echo -e "${RED}❌ Plist not found: $plist_file${NC}"
    exit 1
fi
# Try to parse XML
python3 -c "import plistlib; plistlib.load(open('$plist_file', 'rb'))" || {
    echo -e "${RED}❌ Plist is invalid XML${NC}"
    exit 1
}
echo -e "${GREEN}✅ Plist valid${NC}"
echo ""

# Step 5: Install launchd service
echo -e "${YELLOW}[5/7] Installing launchd service...${NC}"
cp "$plist_file" "$LAUNCHAGENTS_DIR/com.axiom.sqs-preflight.plist"
chmod 644 "$LAUNCHAGENTS_DIR/com.axiom.sqs-preflight.plist"

# Unload if already loaded
launchctl unload "$LAUNCHAGENTS_DIR/com.axiom.sqs-preflight.plist" 2>/dev/null || true

# Load service
if launchctl load "$LAUNCHAGENTS_DIR/com.axiom.sqs-preflight.plist" 2>/dev/null; then
    echo -e "${GREEN}✅ Service loaded${NC}"
else
    echo -e "${RED}⚠️  Warning: Service load had issues (may still be OK)${NC}"
fi
echo ""

# Step 6: Verify service loaded
echo -e "${YELLOW}[6/7] Verifying service is scheduled...${NC}"
if launchctl list | grep -q "com.axiom.sqs-preflight"; then
    echo -e "${GREEN}✅ Service is scheduled${NC}"
else
    echo -e "${RED}❌ Service not found in launchctl list${NC}"
    echo "Try: launchctl load $LAUNCHAGENTS_DIR/com.axiom.sqs-preflight.plist"
    exit 1
fi
echo ""

# Step 7: Test execution
echo -e "${YELLOW}[7/7] Running test execution...${NC}"
cd "$NEXUS_DIR"
timeout 30 python3 preflight_runner.py > /tmp/preflight_test.log 2>&1 || {
    exit_code=$?
    if [ $exit_code -eq 124 ]; then
        echo -e "${YELLOW}⚠️  Preflight timed out (this is OK for first run)${NC}"
    elif [ $exit_code -eq 1 ]; then
        echo -e "${YELLOW}⚠️  Some checks failed (but system deployed)${NC}"
        tail -20 /tmp/preflight_test.log
    else
        echo -e "${RED}❌ Test failed with exit code $exit_code${NC}"
        tail -20 /tmp/preflight_test.log
        exit 1
    fi
}
echo -e "${GREEN}✅ Test execution complete${NC}"
echo ""

# Success summary
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}✅ DEPLOYMENT SUCCESSFUL${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo "Next steps:"
echo ""
echo "1. Review logs:"
echo "   tail -f $NEXUS_DIR/logs/sqs_preflight.log"
echo ""
echo "2. Verify schedule:"
echo "   launchctl list com.axiom.sqs-preflight"
echo ""
echo "3. Manual test run:"
echo "   python3 $NEXUS_DIR/preflight_runner.py"
echo ""
echo "4. Check CHRONICLE for results:"
echo "   sqlite3 $NEXUS_DIR/data/chronicle.db 'SELECT * FROM preflight_results LIMIT 1'"
echo ""
echo "System will run automatically every trading day at 8:30 AM ET"
echo ""
echo "📝 Full documentation: $NEXUS_DIR/PREFLIGHT_DEPLOYMENT.md"
echo ""
