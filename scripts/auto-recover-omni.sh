#!/bin/bash
# Auto-recovery script for OMNI — detects when API key is fixed and restarts
# Usage: Run in background. Will check every 30s for API key health.
#
# Logic:
# 1. Check OMNI /health endpoint
# 2. If "standby" → check Anthropic API key
# 3. If Anthropic key now works (HTTP 200/400 instead of 401) → restart OMNI
# 4. Wait for OMNI to come out of STANDBY
# 5. Log success and exit

set -e

LOG_FILE="/Users/ahmedsadek/nexus/logs/omni/auto-recovery.log"
OMNI_HEALTH_URL="http://localhost:8004/health"
ANTHROPIC_TEST_URL="https://api.anthropic.com/v1/models"

log_msg() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

check_anthropic_key() {
    # Load current ANTHROPIC_API_KEY from .env
    ANTHROPIC_KEY=$(grep "^ANTHROPIC_API_KEY=" /Users/ahmedsadek/nexus/.env | cut -d= -f2)
    
    if [ -z "$ANTHROPIC_KEY" ]; then
        echo "INVALID"
        return
    fi
    
    # Test key with a simple API call
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "x-api-key: $ANTHROPIC_KEY" \
        -H "content-type: application/json" \
        "$ANTHROPIC_TEST_URL" 2>/dev/null)
    
    # 401 = invalid key | 400/403/other = key is valid (other error)
    if [ "$HTTP_CODE" = "401" ]; then
        echo "INVALID"
    else
        echo "VALID"
    fi
}

main() {
    log_msg "OMNI Auto-Recovery: Started"
    log_msg "Monitoring interval: 30s | OMNI health check every iteration"
    
    CHECKS=0
    MAX_CHECKS=$((60 * 20))  # 20 minutes = 1200 checks @ 30s intervals
    
    while [ $CHECKS -lt $MAX_CHECKS ]; do
        CHECKS=$((CHECKS + 1))
        
        # Check OMNI health
        OMNI_RESPONSE=$(curl -s -m 5 "$OMNI_HEALTH_URL" 2>/dev/null || echo "{}")
        OMNI_STATUS=$(echo "$OMNI_RESPONSE" | jq -r '.status // "unknown"' 2>/dev/null || echo "unknown")
        
        if [ "$OMNI_STATUS" != "standby" ]; then
            # OMNI is already healthy or in a good state
            log_msg "✅ OMNI status: $OMNI_STATUS (recovery complete)"
            exit 0
        fi
        
        # OMNI is in STANDBY — check if API key is now valid
        ANTHROPIC_STATUS=$(check_anthropic_key)
        
        if [ "$ANTHROPIC_STATUS" = "VALID" ]; then
            log_msg "🔄 Anthropic API key is now VALID — restarting OMNI"
            
            # Kill current OMNI process
            pkill -f "omni/main" 2>/dev/null || true
            sleep 2
            
            # Restart via LaunchAgent guard script
            /bin/bash /Users/ahmedsadek/nexus/scripts/launch-omni-guarded.sh
            
            # Wait for OMNI to start
            sleep 10
            
            # Check health one more time
            OMNI_RESPONSE=$(curl -s -m 5 "$OMNI_HEALTH_URL" 2>/dev/null || echo "{}")
            OMNI_STATUS=$(echo "$OMNI_RESPONSE" | jq -r '.status // "unknown"' 2>/dev/null || echo "unknown")
            
            if [ "$OMNI_STATUS" != "standby" ]; then
                log_msg "✅ OMNI recovered — status: $OMNI_STATUS"
                exit 0
            else
                log_msg "⚠️  OMNI still in STANDBY after restart — will continue checking"
            fi
        else
            log_msg "⏳ Check #$CHECKS: Anthropic key still invalid — waiting for update (next check in 30s)"
        fi
        
        sleep 30
    done
    
    log_msg "❌ Auto-recovery timeout after 20 minutes — manual intervention may be needed"
    exit 1
}

main
