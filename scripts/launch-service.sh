#!/bin/bash
# Universal service launcher — sources .env before exec
# Usage: launch-service.sh /path/to/service/dir [port]
# 
# Port precedence:
# 1. $2 argument (if provided)
# 2. PORT environment variable from .env
# 3. Service-specific env var (AXIOM_PORT, ALPHA_BUFFER_PORT, etc.)
# 4. Default 8008

SERVICE_DIR="$1"
ARG_PORT="$2"

cd "$SERVICE_DIR" || exit 1

if [ -f ".env" ]; then
  set -a
  source .env
  set +a
fi

# Explicitly ensure critical env vars are exported (for subprocess inheritance)
export DEEPSEEK_API_KEY
export POLYGON_API_KEY
export ALPACA_API_KEY
export ALPACA_SECRET_KEY

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

# Try to use startup wrapper if it exists (for .env auto-loading)
if [ -f "$SERVICE_DIR/startup.py" ]; then
  echo "[LAUNCH] Using startup.py wrapper for $SERVICE_DIR" >&2
  $PY "$SERVICE_DIR/startup.py" "$FINAL_PORT"
else
  echo "[LAUNCH] No startup.py found, using uvicorn directly" >&2
  $PY -m uvicorn main:app --host 0.0.0.0 --port "$FINAL_PORT" --log-level info
fi
EXIT_CODE=$?
# Exit with the same code Uvicorn returned
# Exit 0 = graceful shutdown (normal exit — do NOT restart)
# Exit 1+ = error (restart via LaunchAgent KeepAlive)
exit $EXIT_CODE
