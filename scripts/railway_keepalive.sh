#!/bin/bash
# H-18: Railway keep-alive — pings Alpha + Prime to prevent cold-start 502 crashes
# Runs every 5 minutes via LaunchAgent (ai.nexus.railway-keepalive)
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# Ping Nexus Alpha
ALPHA_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 \
  "https://worker-production-2060.up.railway.app/health" 2>/dev/null)
echo "[$TIMESTAMP] Alpha keepalive: HTTP $ALPHA_STATUS"

# Ping Nexus Prime
PRIME_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 \
  "https://nexus-prime-bot-production.up.railway.app/health" 2>/dev/null)
echo "[$TIMESTAMP] Prime keepalive: HTTP $PRIME_STATUS"
