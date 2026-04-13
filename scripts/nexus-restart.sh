#!/bin/bash
# nexus-restart.sh — Restart all 7 Nexus services
# Usage: bash ~/nexus/scripts/nexus-restart.sh
# Optional: bash ~/nexus/scripts/nexus-restart.sh oracle   (single service)

UID_VAL=$(id -u)

ALL_SERVICES=(
    "ai.nexus.oracle"
    "ai.nexus.axiom"
    "ai.nexus.alpha-buffer"
    "ai.nexus.prime-buffer"
    "ai.nexus.omni"
    "ai.nexus.alpha-execution"
    "ai.nexus.prime-execution"
)

if [ -n "$1" ]; then
    # Single service restart
    label="ai.nexus.$1"
    echo "Restarting $label..."
    launchctl kickstart -k "gui/${UID_VAL}/${label}" 2>/dev/null && echo "  Done." || echo "  Failed — is $label loaded?"
else
    # Restart all
    echo "Restarting all Nexus services..."
    for label in "${ALL_SERVICES[@]}"; do
        echo "  → $label"
        launchctl kickstart -k "gui/${UID_VAL}/${label}" 2>/dev/null || echo "    (not loaded — trying start)"
        sleep 2
    done
    echo ""
    echo "Waiting 5s for services to come up..."
    sleep 5
    bash "$(dirname "$0")/nexus-status.sh"
fi
