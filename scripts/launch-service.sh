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
exec .venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8008}" --log-level info
