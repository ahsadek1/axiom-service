#!/bin/bash
# Guarded OMNI launcher — skips if port 8004 is already in use
if lsof -i :8004 -t > /dev/null 2>&1; then
    echo "OMNI already running on port 8004 — skipping launch"
    exit 0
fi
exec /bin/bash /Users/ahmedsadek/nexus/scripts/launch-service.sh /Users/ahmedsadek/nexus/omni 8004
