#!/bin/bash
# Wrapper script for premarket validator — ensures .env is loaded for cron jobs
# Usage: ./scripts/run_premarket_validator.sh
# Cron: 0 9 * * 1-5 cd /Users/ahmedsadek/nexus && bash scripts/run_premarket_validator.sh

set -e

NEXUS_ROOT="/Users/ahmedsadek/nexus"
cd "$NEXUS_ROOT"

# Load environment from .env (required for cron jobs which don't have user shell environment)
if [ -f ".env" ]; then
    export $(grep -v "^#" .env | xargs)
fi

# Run the validator
python3 scripts/premarket_pipeline_validator.py
