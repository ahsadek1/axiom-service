#!/bin/bash
# run_canary.sh — CANARY pre-market pipeline test runner
# Called by launchd at 9:00 AM ET on weekdays

set -e

NEXUS_DIR="/Users/ahmedsadek/nexus"
VENV_PYTHON=""

# Find best available Python with requests/pytz installed
for PY in \
    "$NEXUS_DIR/alpha-buffer/.venv/bin/python3" \
    "$NEXUS_DIR/omni/.venv/bin/python3" \
    "/opt/homebrew/bin/python3" \
    "/usr/bin/python3"
do
    if [ -f "$PY" ]; then
        VENV_PYTHON="$PY"
        break
    fi
done

if [ -z "$VENV_PYTHON" ]; then
    echo "[ERROR] No python3 found" >&2
    exit 1
fi

echo "[$(date)] Starting CANARY pre-market test with $VENV_PYTHON"
exec "$VENV_PYTHON" "$NEXUS_DIR/scripts/canary_test.py"
