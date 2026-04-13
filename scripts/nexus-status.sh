#!/bin/bash
# nexus-status.sh — Quick health check for all 7 Nexus services
# Usage: bash ~/nexus/scripts/nexus-status.sh

SERVICES=(
    "Axiom       8001 http://localhost:8001/health"
    "AlphaBuffer 8002 http://localhost:8002/health"
    "PrimeBuffer 8003 http://localhost:8003/health"
    "OMNI        8004 http://localhost:8004/health"
    "AlphaExec   8005 http://localhost:8005/health"
    "PrimeExec   8006 http://localhost:8006/health"
    "ORACLE      8007 http://localhost:8007/ping"
)

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  NEXUS STATUS — $(date '+%Y-%m-%d %H:%M:%S')"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

all_ok=true
for entry in "${SERVICES[@]}"; do
    name=$(echo "$entry" | awk '{print $1}')
    port=$(echo "$entry" | awk '{print $2}')
    url=$(echo "$entry"  | awk '{print $3}')

    response=$(curl -s --max-time 3 "$url" 2>/dev/null)
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "$url" 2>/dev/null)

    if [ "$code" = "200" ]; then
        # Extract regime or pool_size if available
        extra=""
        if echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('regime','') or d.get('pool_size','') or '')" 2>/dev/null | grep -q .; then
            extra=" $(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); v=d.get('regime','') or d.get('pool_size',''); print('| '+str(v))" 2>/dev/null)"
        fi
        printf "  ✅  %-13s (:%s)%s\n" "$name" "$port" "$extra"
    else
        printf "  ❌  %-13s (:%s) — HTTP %s\n" "$name" "$port" "${code:-timeout}"
        all_ok=false
    fi
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if $all_ok; then
    echo "  All services healthy"
else
    echo "  ⚠️  One or more services are down"
    echo "  Check: tail -50 ~/nexus/logs/<service>/stderr.log"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
