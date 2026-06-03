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
    # Restart all — Oracle must be warm before Axiom starts (P1-A fix 2026-05-01)
    echo "Restarting all Nexus services..."
    for label in "${ALL_SERVICES[@]}"; do
        echo "  → $label"
        launchctl kickstart -k "gui/${UID_VAL}/${label}" 2>/dev/null || echo "    (not loaded — trying start)"

        # After Oracle: wait until cache is warm (>=20 tickers) before launching Axiom
        # Prevents VIX_ONLY_FALLBACK race condition on restart
        if [ "$label" = "ai.nexus.oracle" ]; then
            echo "  ⏳ Waiting for Oracle cache to warm (target: >=20 tickers)..."
            WARM=0
            for i in $(seq 1 30); do
                sleep 2
                WARM_COUNT=$(curl -s --connect-timeout 3 http://localhost:8007/health 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cache_warm_tickers',0))" 2>/dev/null || echo 0)
                echo "    Oracle warm tickers: ${WARM_COUNT}"
                if [ "${WARM_COUNT}" -ge 20 ] 2>/dev/null; then
                    echo "  ✅ Oracle warm (${WARM_COUNT} tickers) — proceeding to Axiom"
                    WARM=1
                    break
                fi
            done
            if [ "$WARM" -eq 0 ]; then
                echo "  ⚠️  Oracle warm timeout after 60s — starting Axiom anyway (VIX-only fallback active)"
            fi
        else
            sleep 2
        fi
    done
    echo ""
    echo "Waiting 5s for services to come up..."
    sleep 5
    bash "$(dirname "$0")/nexus-status.sh"
fi
