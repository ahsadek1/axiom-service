#!/bin/bash
# railway-keepalive.sh — Prevents Railway cold-start 502 errors
# Runs via launchd every 5 minutes during market hours
WORKER_URL="https://worker-production-2060.up.railway.app"
PRIME_URL="https://nexus-prime-bot-production.up.railway.app"

# Ping worker
curl -s --max-time 10 "$WORKER_URL/health" > /dev/null 2>&1
# Ping prime
curl -s --max-time 10 "$PRIME_URL/health" > /dev/null 2>&1

echo "[$(date)] Keep-alive ping sent to Railway worker + prime"
