#!/bin/bash

# dress_rehearsal.sh — Daily Mock Trading Dress Rehearsal Wrapper
# Runs at 6:30 PM EDT every single day
# Authority: Ahmed Sadek (SOVEREIGN)

set -e

export NEXUS_AUTO_EXECUTE=false
export NEXUS_MOCK_MODE=true
export MOCK_TRADE_PREFIX="MOCK_"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NEXUS_HOME="${SCRIPT_DIR%/scripts}"
LOG_DIR="$NEXUS_HOME/logs/rehearsal"

mkdir -p "$LOG_DIR"

# Run the Python dress rehearsal
python3 "$SCRIPT_DIR/dress_rehearsal.py"

exit $?
