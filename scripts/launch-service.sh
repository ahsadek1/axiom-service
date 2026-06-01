#!/bin/bash
# Universal service launcher — sources .env before exec
# Usage: launch-service.sh /path/to/service/dir module_name port
SERVICE_DIR="$1"
cd "$SERVICE_DIR" || exit 1
if [ -f ".env" ]; then
  set -a
  source .env
  set +a
fi
# Add nexus directory to Python path for module resolution
export PYTHONPATH=/Users/ahmedsadek/nexus:${PYTHONPATH}
# Use venv Python if available, otherwise system
if [ -f ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="/usr/bin/python3"
fi
$PY -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8008}" --log-level info
EXIT_CODE=$?
# Exit with the same code Uvicorn returned
# Exit 0 = graceful shutdown (normal exit — do NOT restart)
# Exit 1+ = error (restart via LaunchAgent KeepAlive)
exit $EXIT_CODE
