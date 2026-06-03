#!/bin/bash
set -e

export NEXUS_AUTO_EXECUTE=false
export NEXUS_MOCK_MODE=true
export MOCK_TRADE_PREFIX="MOCK_"

TIMESTAMP=$(date '+%Y-%m-%d_%H-%M-%S')
LOG_DIR="~/nexus/logs/rehearsal"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${TIMESTAMP}_rehearsal.log"

ISSUES_LOG="$LOG_DIR/${TIMESTAMP}_issues.md"
RESULTS_LOG="$LOG_DIR/${TIMESTAMP}_results.json"

# Initialize logs
echo "🧪 MOCK TRADING DRESS REHEARSAL — $TIMESTAMP" > "$LOG_FILE"
echo "{\"timestamp\":\"$TIMESTAMP\",\"scenarios\":{}}" > "$RESULTS_LOG"

echo "Rehearsal logging initialized: $LOG_FILE"
echo "Issues log: $ISSUES_LOG"
echo "Results: $RESULTS_LOG"
