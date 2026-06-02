#!/bin/bash
# Guarded OMNI launcher — respawns if port 8004 is stale or unresponsive
# 
# Logic: Check if a process is ACTUALLY listening and responding to /health
# If port exists but service is dead/stuck → kill it and restart
# If port doesn't exist → start fresh
# If port exists AND service responds → skip (already healthy)

PORT=8004
HEALTH_URL="http://localhost:$PORT/health"
TIMEOUT=5

# Check if port has a listening process
if lsof -i :$PORT -t > /dev/null 2>&1; then
    # Port is bound. Test if service is actually responding.
    HEALTH_RESPONSE=$(curl -s -m $TIMEOUT "$HEALTH_URL" 2>/dev/null)
    
    if [ $? -eq 0 ] && [ -n "$HEALTH_RESPONSE" ]; then
        # Service is responding — already healthy
        echo "OMNI already running and healthy on port $PORT — skipping launch"
        exit 0
    else
        # Port is bound but service is not responding — likely crashed or hung
        echo "OMNI port $PORT is bound but service unresponsive — killing stale process"
        # Kill the stale process and its children
        pkill -9 -f "omni/main" 2>/dev/null || true
        sleep 1
        # Fall through to launch fresh
    fi
else
    # Port is free — normal case
    echo "OMNI port $PORT is free — launching fresh"
fi

# Launch OMNI
exec /bin/bash /Users/ahmedsadek/nexus/scripts/launch-service.sh /Users/ahmedsadek/nexus/omni 8004
