#!/bin/bash
# Enhanced service launcher — reads .env and explicitly exports to child process

SERVICE_DIR="$1"
ARG_PORT="$2"

cd "$SERVICE_DIR" || exit 1

# Source .env into this shell
if [ -f ".env" ]; then
  set -a
  source .env
  set +a
fi

# Add nexus directory to Python path for module resolution
export PYTHONPATH=/Users/ahmedsadek/nexus:${PYTHONPATH}

# Determine final port
FINAL_PORT="${ARG_PORT:-${PORT:-8008}}"

# Use venv Python if available, otherwise system
if [ -f ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="/usr/bin/python3"
fi

# Launch with all env vars explicitly passed (NOT backgrounded yet)
# This ensures the child process gets all sourced variables
exec $PY -m uvicorn main:app --host 0.0.0.0 --port "$FINAL_PORT" --log-level info
EXIT_CODE=$?
exit $EXIT_CODE
