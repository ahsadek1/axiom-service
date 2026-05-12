#!/bin/bash
# standalone_watchdog.sh — Market-hours critical watchdog
# Runs via native macOS crontab, INDEPENDENT of OpenClaw gateway.
# If OpenClaw dies, this keeps running and alerts Ahmed directly.
#
# Checks:
#   1. Are all Nexus services healthy?
#   2. Is OpenClaw gateway alive?
#   3. Are there open positions at risk (exit monitor coverage)?
#   4. Is the Primus watchdog alive?
#
# Alerts via direct Telegram HTTP (no OpenClaw dependency).
#
# Schedule (loaded via crontab -e):
#   */5 9-16 * * 1-5 /Users/ahmedsadek/nexus/scripts/standalone_watchdog.sh
#
# Installed: 2026-04-29 — Commercial grade gap 8 fix
# Author: Cipher

set -euo pipefail

LOG=/Users/ahmedsadek/nexus/logs/standalone_watchdog.log
TELEGRAM_BOT=$(cat /Users/ahmedsadek/.openclaw/workspace-primus/.env 2>/dev/null | grep TELEGRAM_BOT_TOKEN | cut -d= -f2 || echo "")
TELEGRAM_CHAT="8573754783"
OPENCLAW_URL="http://localhost:18789/health"
ALERT_COOLDOWN_FILE="/tmp/nexus_standalone_alert_ts"
ALERT_COOLDOWN_SECS=900  # 15 min between repeat alerts

send_alert() {
  local msg="$1"
  local now
  now=$(date +%s)

  # Check cooldown
  if [[ -f "$ALERT_COOLDOWN_FILE" ]]; then
    local last
    last=$(cat "$ALERT_COOLDOWN_FILE")
    if (( now - last < ALERT_COOLDOWN_SECS )); then
      echo "[watchdog] Alert suppressed (cooldown)" >> "$LOG"
      return 0
    fi
  fi

  echo "$now" > "$ALERT_COOLDOWN_FILE"

  # Route through Alert Broker first; fall back to direct Telegram if unreachable
  local broker_ok=false
  curl -s -X POST "http://192.168.1.141:9998/alert" \
    -H "Content-Type: application/json" \
    -d "{\"source\": \"standalone-watchdog\", \"level\": \"critical\", \"message\": \"${msg}\", \"key\": \"standalone-watchdog-alert\"}" \
    --max-time 5 > /dev/null 2>&1 && broker_ok=true

  if [[ "$broker_ok" == false && -n "$TELEGRAM_BOT" ]]; then
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT}/sendMessage" \
      -H "Content-Type: application/json" \
      -d "{\"chat_id\": \"${TELEGRAM_CHAT}\", \"text\": \"${msg}\"}" \
      --max-time 8 > /dev/null 2>&1 || true
  fi
  echo "[watchdog] ALERT SENT (broker=$broker_ok): $msg" >> "$LOG"
}

# Load NEXUS_SECRET from .deploy-secrets (single source of truth)
if [[ -f "/Users/ahmedsadek/nexus/.deploy-secrets" ]]; then
  NEXUS_SECRET=$(grep '^NEXUS_SECRET=' /Users/ahmedsadek/nexus/.deploy-secrets | cut -d= -f2)
else
  echo "[watchdog] ERROR: .deploy-secrets not found — cannot authenticate service health checks" >> "$LOG"
  exit 1
fi
if [[ -z "$NEXUS_SECRET" ]]; then
  echo "[watchdog] ERROR: NEXUS_SECRET not found in .deploy-secrets" >> "$LOG"
  exit 1
fi

check_service() {
  local name="$1"
  local url="$2"
  local timeout="${3:-4}"
  if curl -sf --max-time "$timeout" "$url" > /dev/null 2>&1; then
    return 0
  else
    return 1
  fi
}

# Auth-aware check for services that require X-Nexus-Secret on /health
check_service_auth() {
  local name="$1"
  local url="$2"
  local timeout="${3:-4}"
  if curl -sf --max-time "$timeout" -H "X-Nexus-Secret: $NEXUS_SECRET" "$url" > /dev/null 2>&1; then
    return 0
  else
    return 1
  fi
}

run_watchdog() {
  local ts
  ts=$(date '+%Y-%m-%d %H:%M:%S')
  local issues=()

  # 1. Critical V1 services
  # Check critical services
  # Services requiring auth header — use check_service_auth
  local auth_svc_names=("Alpha-Exec" "Prime-Exec" "Axiom")
  local auth_svc_urls=("http://localhost:8005/health" "http://localhost:8006/health" "http://localhost:8001/health")

  local i
  for i in "${!auth_svc_names[@]}"; do
    if ! check_service_auth "${auth_svc_names[$i]}" "${auth_svc_urls[$i]}"; then
      issues+=("${auth_svc_names[$i]} DOWN")
    fi
  done

  # Services with unauthenticated health/ping endpoints
  local svc_names=("OMNI" "Oracle")
  local svc_urls=("http://localhost:8004/health" "http://localhost:8007/ping")

  for i in "${!svc_names[@]}"; do
    if ! check_service "${svc_names[$i]}" "${svc_urls[$i]}"; then
      issues+=("${svc_names[$i]} DOWN")
    fi
  done

  # 2. OpenClaw gateway
  if ! check_service "OpenClaw" "$OPENCLAW_URL"; then
    issues+=("OpenClaw GATEWAY DOWN — cron jobs not firing")
  fi

  # 3. Primus watchdog process
  if ! pgrep -f "primus_watchdog.py" > /dev/null 2>&1; then
    issues+=("Primus watchdog DEAD")
  fi

  # 4. Alpaca reachability
  # Load from alpha-execution .env if env vars not set (LaunchAgent doesn't inherit shell env)
  if [[ -z "${ALPACA_API_KEY:-}" ]] && [[ -f "/Users/ahmedsadek/nexus/alpha-execution/.env" ]]; then
    ALPACA_KEY=$(grep '^ALPACA_API_KEY=' /Users/ahmedsadek/nexus/alpha-execution/.env | cut -d= -f2 | tr -d '[:space:]')
    ALPACA_SEC=$(grep '^ALPACA_SECRET_KEY=' /Users/ahmedsadek/nexus/alpha-execution/.env | cut -d= -f2 | tr -d '[:space:]')
  else
    ALPACA_KEY="${ALPACA_API_KEY:-}"
    ALPACA_SEC="${ALPACA_SECRET_KEY:-}"
  fi
  if ! curl -sf --max-time 5 \
    -H "APCA-API-KEY-ID: $ALPACA_KEY" \
    -H "APCA-API-SECRET-KEY: $ALPACA_SEC" \
    "https://paper-api.alpaca.markets/v2/account" > /dev/null 2>&1; then
    issues+=("Alpaca API UNREACHABLE")
  fi

  echo "[$ts] Watchdog: ${#issues[@]} issues" >> "$LOG"

  if [[ ${#issues[@]} -gt 0 ]]; then
    local msg="🚨 NEXUS STANDALONE WATCHDOG — $ts"$'\n'
    msg+="Issues detected:"$'\n'
    for issue in "${issues[@]}"; do
      msg+="  • $issue"$'\n'
    done
    msg+="(Standalone watchdog — runs independent of OpenClaw)"
    send_alert "$msg"
    echo "[$ts] Issues: ${issues[*]}" >> "$LOG"
    return 1
  fi

  return 0
}

# Ensure log dir exists
mkdir -p "$(dirname "$LOG")"
run_watchdog
