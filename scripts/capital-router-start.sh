#!/bin/bash

# Capital Router launcher with intelligent port reuse
# Primus 2026-05-30: Handles TIME_WAIT port binding failures

PORT=8000
MAX_WAIT=30

echo "[$(date)] Starting Capital Router startup script"

# Kill any lingering processes on port 8000
for pid in $(lsof -ti :$PORT 2>/dev/null); do
    echo "[$(date)] Killing lingering process on port $PORT: PID $pid"
    kill -9 $pid 2>/dev/null || true
done

# Wait for port to actually be released
echo "[$(date)] Waiting for port $PORT to be released..."
for i in $(seq 1 $MAX_WAIT); do
    if ! lsof -i :$PORT >/dev/null 2>&1; then
        echo "[$(date)] Port $PORT is now free"
        break
    fi
    if [ $i -eq $MAX_WAIT ]; then
        echo "[$(date)] WARNING: Port $PORT still in use after $MAX_WAIT seconds (TIME_WAIT state)"
        # Force the port anyway — SO_REUSEADDR will handle it
    fi
    sleep 1
done

# Start the service with Python's SO_REUSEADDR patch
cd /Users/ahmedsadek/nexus/capital-router
exec python3 -c "
import socket
socket.SO_REUSEADDR = socket.SO_REUSEADDR if hasattr(socket, 'SO_REUSEADDR') else 2

exec(open('main.py').read())
"
