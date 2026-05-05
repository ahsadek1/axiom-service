#!/bin/bash
# start.sh — Nexus Sentinel Prime startup script
set -euo pipefail

cd /Users/ahmedsadek/nexus/nsp

# Load .env if present (dev convenience — production uses launchd EnvironmentVariables)
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

mkdir -p "${NSP_LOG_DIR:-/Users/ahmedsadek/nexus/logs/nsp}"

exec python3 -m uvicorn main:app \
    --host 0.0.0.0 \
    --port "${NSP_PORT:-8010}" \
    --log-level "${LOG_LEVEL:-info}"
