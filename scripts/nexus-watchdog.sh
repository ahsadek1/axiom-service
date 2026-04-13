#!/bin/bash
# nexus-watchdog.sh — Nexus Health Monitor
# Checks all 7 services every 60 seconds via /health endpoint.
# Sends Telegram alert ONCE when a service goes down.
# Sends "RECOVERED" alert when it comes back up.
# State stored in /tmp/nexus-watchdog-state/ (cleared on reboot — intentional).

BOT_TOKEN="7973500599:AAGoTNnJgF0Muvok_FSXeTPHeAUaOKMahk0"
CHAT_ID="8573754783"
STATE_DIR="/tmp/nexus-watchdog-state"
mkdir -p "$STATE_DIR"

declare -A SERVICES
SERVICES["axiom"]="http://localhost:8001/health"
SERVICES["alpha-buffer"]="http://localhost:8002/health"
SERVICES["prime-buffer"]="http://localhost:8003/health"
SERVICES["omni"]="http://localhost:8004/health"
SERVICES["alpha-execution"]="http://localhost:8005/health"
SERVICES["prime-execution"]="http://localhost:8006/health"
SERVICES["oracle"]="http://localhost:8007/ping"

send_telegram() {
    local message="$1"
    curl -s -X POST \
        "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${CHAT_ID}" \
        --data-urlencode "parse_mode=HTML" \
        --data-urlencode "text=${message}" \
        > /dev/null 2>&1
}

check_service() {
    local name="$1"
    local url="$2"
    local state_file="${STATE_DIR}/${name}.down"

    http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null)

    if [ "$http_code" = "200" ]; then
        # Service is up
        if [ -f "$state_file" ]; then
            # Was previously down — send recovery alert
            rm -f "$state_file"
            send_telegram "✅ <b>NEXUS RECOVERED</b>
Service: <b>${name}</b>
Status: Back online
Time: $(date '+%Y-%m-%d %H:%M:%S ET')"
        fi
    else
        # Service is down
        if [ ! -f "$state_file" ]; then
            # First detection — alert Ahmed
            touch "$state_file"
            send_telegram "🚨 <b>NEXUS SERVICE DOWN</b>
Service: <b>${name}</b>
HTTP response: ${http_code:-no response}
Time: $(date '+%Y-%m-%d %H:%M:%S ET')

Check logs:
<code>tail -50 ~/nexus/logs/${name}/stderr.log</code>

Restart:
<code>launchctl kickstart -k gui/$(id -u)/ai.nexus.${name}</code>"
        fi
    fi
}

echo "[$(date)] Nexus watchdog started — monitoring 7 services"

while true; do
    for name in axiom alpha-buffer prime-buffer omni alpha-execution prime-execution oracle; do
        check_service "$name" "${SERVICES[$name]}"
    done
    sleep 60
done
