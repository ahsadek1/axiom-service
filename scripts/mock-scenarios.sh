#!/bin/bash
set -e

export NEXUS_AUTO_EXECUTE=false
export NEXUS_MOCK_MODE=true
export MOCK_TRADE_PREFIX="MOCK_"
DATE=$(date +%Y-%m-%d)
LOGFILE=~/nexus/memory/$DATE.md

log() {
  echo "[$(date +%H:%M:%S)] $1" | tee -a $LOGFILE
}

log "SCENARIO 1: Clean Concordance Baseline"
# Clear buffer
curl -s -X DELETE http://localhost:8002/buffer/clear 2>&1 | tee -a $LOGFILE
log "✅ Buffer cleared"

# Submit baseline candidate
PICK1=$(curl -s -X POST http://localhost:8001/mock/pick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"SPY","option":"call","delta":0.50,"win_pct":55,"tp_sl_ratio":2.0}')
log "Pick submitted: $PICK1"
sleep 2
BUFFER_COUNT=$(curl -s http://localhost:8002/buffer/count | jq '.count')
log "Buffer count: $BUFFER_COUNT (expected: 1)"
if [ "$BUFFER_COUNT" = "1" ]; then
  echo "✅ SCENARIO 1 PASS" >> $LOGFILE
else
  echo "❌ SCENARIO 1 FAIL" >> $LOGFILE
fi

log "─────────────────────────────────────────────"
log "SCENARIO 2: Agent Timeout (P2 Pathway)"
AGENT_PID=$(pgrep -f 'axiom.*agent' | head -1)
if [ ! -z "$AGENT_PID" ]; then
  kill -STOP $AGENT_PID 2>/dev/null || true
  log "Agent process paused: $AGENT_PID"
  sleep 35
  TIMEOUT_DETECTED=$(curl -s http://localhost:8001/metrics | jq '.p2_timeout_count' 2>/dev/null || echo "0")
  kill -CONT $AGENT_PID 2>/dev/null || true
  log "Agent resumed. Timeouts detected: $TIMEOUT_DETECTED"
  if [ "$TIMEOUT_DETECTED" != "0" ]; then
    echo "✅ SCENARIO 2 PASS" >> $LOGFILE
  else
    echo "❌ SCENARIO 2 FAIL" >> $LOGFILE
  fi
else
  echo "⚠️  SCENARIO 2 SKIP (agent not found)" >> $LOGFILE
fi

log "─────────────────────────────────────────────"
log "SCENARIO 3: Conditional Verdict (MUST BLOCK)"
PICK3=$(curl -s -X POST http://localhost:8001/mock/pick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AAPL","option":"call","delta":0.50,"win_pct":45,"tp_sl_ratio":1.5}')
log "Low-edge pick submitted: $PICK3"
sleep 1
REJECTED=$(curl -s http://localhost:8001/metrics | jq '.picks_rejected_conditional' 2>/dev/null || echo "0")
log "Conditional rejects: $REJECTED"
if [ "$REJECTED" != "0" ]; then
  echo "✅ SCENARIO 3 PASS" >> $LOGFILE
else
  echo "❌ SCENARIO 3 FAIL" >> $LOGFILE
fi

log "─────────────────────────────────────────────"
log "SCENARIO 4: VIX Brake Activation (MUST BLOCK)"
curl -s -X POST http://localhost:8001/mock/vix -d '{"level":35.0}' 2>&1 | tee -a $LOGFILE
log "VIX set to 35.0 (brake threshold: 30.0)"
sleep 1
PICK4=$(curl -s -X POST http://localhost:8001/mock/pick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"QQQ","option":"call","delta":0.50,"win_pct":55,"tp_sl_ratio":2.0}')
log "Pick attempt under high VIX: $PICK4"
sleep 1
VIX_BLOCKS=$(curl -s http://localhost:8001/metrics | jq '.picks_rejected_vix' 2>/dev/null || echo "0")
curl -s -X POST http://localhost:8001/mock/vix -d '{"level":20.0}' 2>&1 > /dev/null
log "VIX reset to 20.0. VIX blocks: $VIX_BLOCKS"
if [ "$VIX_BLOCKS" != "0" ]; then
  echo "✅ SCENARIO 4 PASS" >> $LOGFILE
else
  echo "❌ SCENARIO 4 FAIL" >> $LOGFILE
fi

log "─────────────────────────────────────────────"
log "SCENARIO 5: Primary Data Source Unavailable (FALLBACK)"
curl -s -X POST http://localhost:8007/simulate/down 2>&1 | tee -a $LOGFILE
log "Oracle simulated DOWN"
sleep 1
PICK5=$(curl -s -X POST http://localhost:8001/mock/pick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"IWM","option":"call","delta":0.50,"win_pct":55,"tp_sl_ratio":2.0}')
log "Pick submitted with Oracle down: $PICK5"
sleep 2
FALLBACK_USED=$(curl -s http://localhost:8007/metrics | jq '.fallback_count' 2>/dev/null || echo "0")
curl -s -X POST http://localhost:8007/simulate/up 2>&1 > /dev/null
log "Oracle restored. Fallbacks used: $FALLBACK_USED"
if [ "$FALLBACK_USED" != "0" ]; then
  echo "✅ SCENARIO 5 PASS" >> $LOGFILE
else
  echo "❌ SCENARIO 5 FAIL" >> $LOGFILE
fi

log "─────────────────────────────────────────────"
log "SCENARIO 6: Earnings Gate Block (MUST BLOCK)"
curl -s -X POST http://localhost:8001/mock/earnings -d '{"symbol":"SPY"}' 2>&1 | tee -a $LOGFILE
log "SPY earnings scheduled"
sleep 1
PICK_SPY=$(curl -s -X POST http://localhost:8001/mock/pick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"SPY","option":"call","delta":0.50,"win_pct":55,"tp_sl_ratio":2.0}')
log "SPY pick attempt: $PICK_SPY"
sleep 1
EARNINGS_BLOCKS=$(curl -s http://localhost:8001/metrics | jq '.picks_rejected_earnings' 2>/dev/null || echo "0")
curl -s -X POST http://localhost:8001/mock/earnings -d '{"symbol":"SPY","clear":true}' 2>&1 > /dev/null
log "SPY earnings cleared. Earnings blocks: $EARNINGS_BLOCKS"
if [ "$EARNINGS_BLOCKS" != "0" ]; then
  echo "✅ SCENARIO 6 PASS" >> $LOGFILE
else
  echo "❌ SCENARIO 6 FAIL" >> $LOGFILE
fi

log "─────────────────────────────────────────────"
log "SCENARIO 7: DTE Boundary Condition"
PICK_LONG_DTE=$(curl -s -X POST http://localhost:8001/mock/pick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"TSLA","option":"call","delta":0.50,"dte":180,"win_pct":55,"tp_sl_ratio":2.0}')
log "Long DTE pick (180 DTE): $PICK_LONG_DTE"
sleep 1
LONG_DTE_OK=$(curl -s http://localhost:8001/metrics | jq '.picks_accepted_dte' 2>/dev/null || echo "0")
log "Long DTE accepts: $LONG_DTE_OK"

PICK_SHORT_DTE=$(curl -s -X POST http://localhost:8001/mock/pick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"TSLA","option":"call","delta":0.50,"dte":3,"win_pct":55,"tp_sl_ratio":2.0}')
log "Short DTE pick (3 DTE): $PICK_SHORT_DTE"
sleep 1
SHORT_DTE_REJECT=$(curl -s http://localhost:8001/metrics | jq '.picks_rejected_dte' 2>/dev/null || echo "0")
log "Short DTE rejects: $SHORT_DTE_REJECT"
if [ "$LONG_DTE_OK" != "0" ] && [ "$SHORT_DTE_REJECT" != "0" ]; then
  echo "✅ SCENARIO 7 PASS" >> $LOGFILE
else
  echo "❌ SCENARIO 7 FAIL" >> $LOGFILE
fi

log "─────────────────────────────────────────────"
log "SCENARIO 8: Duplicate Order Prevention (MUST BLOCK)"
curl -s -X DELETE http://localhost:8002/buffer/clear > /dev/null
sleep 1
PICK_DUP1=$(curl -s -X POST http://localhost:8001/mock/pick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"NFLX","option":"call","delta":0.50,"win_pct":55,"tp_sl_ratio":2.0}')
log "First pick submitted: NFLX call"
sleep 2
PICK_DUP2=$(curl -s -X POST http://localhost:8001/mock/pick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"NFLX","option":"call","delta":0.50,"win_pct":55,"tp_sl_ratio":2.0}')
log "Duplicate pick submitted: NFLX call"
sleep 1
DUP_REJECTS=$(curl -s http://localhost:8001/metrics | jq '.picks_rejected_duplicate' 2>/dev/null || echo "0")
BUFFER_COUNT2=$(curl -s http://localhost:8002/buffer/count | jq '.count')
log "Duplicate rejects: $DUP_REJECTS, Buffer count: $BUFFER_COUNT2"
if [ "$DUP_REJECTS" != "0" ] && [ "$BUFFER_COUNT2" = "1" ]; then
  echo "✅ SCENARIO 8 PASS" >> $LOGFILE
else
  echo "❌ SCENARIO 8 FAIL" >> $LOGFILE
fi

log "─────────────────────────────────────────────"
log "SCENARIO 9: Position Limit Reached (MUST BLOCK)"
curl -s -X DELETE http://localhost:8002/buffer/clear > /dev/null
curl -s -X POST http://localhost:8001/config/limit -d '{"max_concurrent":2}' > /dev/null
log "Position limit set to 2"
sleep 1
PICK9_1=$(curl -s -X POST http://localhost:8001/mock/pick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"MSFT","option":"call","delta":0.50,"win_pct":55,"tp_sl_ratio":2.0}')
sleep 1
PICK9_2=$(curl -s -X POST http://localhost:8001/mock/pick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"GOOG","option":"call","delta":0.50,"win_pct":55,"tp_sl_ratio":2.0}')
sleep 1
PICK9_3=$(curl -s -X POST http://localhost:8001/mock/pick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AMZN","option":"call","delta":0.50,"win_pct":55,"tp_sl_ratio":2.0}')
log "Three picks submitted (limit=2)"
sleep 2
LIMIT_REJECTS=$(curl -s http://localhost:8001/metrics | jq '.picks_rejected_limit' 2>/dev/null || echo "0")
BUFFER_COUNT3=$(curl -s http://localhost:8002/buffer/count | jq '.count')
log "Limit rejects: $LIMIT_REJECTS, Final buffer count: $BUFFER_COUNT3"
if [ "$LIMIT_REJECTS" != "0" ] && [ "$BUFFER_COUNT3" = "2" ]; then
  echo "✅ SCENARIO 9 PASS" >> $LOGFILE
else
  echo "❌ SCENARIO 9 FAIL" >> $LOGFILE
fi

log "─────────────────────────────────────────────"
log "SCENARIO 10: Complete Exit Lifecycle"
curl -s -X DELETE http://localhost:8005/trades/clear > /dev/null
sleep 1
PICK10=$(curl -s -X POST http://localhost:8001/mock/pick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"SPY","option":"call","delta":0.50,"win_pct":55,"tp_sl_ratio":2.0,"entry_target":2.50}')
log "Entry pick submitted: SPY call target $2.50"
sleep 2

EXEC_RESULT=$(curl -s -X POST http://localhost:8005/mock/execute \
  -H "Content-Type: application/json" \
  -d '{"trade_id":"MOCK_SPY_001","entry_price":2.50}')
log "Mock execution result: $EXEC_RESULT"
sleep 2

EXIT_RESULT=$(curl -s -X POST http://localhost:8005/mock/exit \
  -H "Content-Type: application/json" \
  -d '{"trade_id":"MOCK_SPY_001","exit_price":3.75}')
log "Mock exit result: $EXIT_RESULT"
sleep 1

ACTIVE_TRADES=$(curl -s http://localhost:8005/trades/active | jq '.active' 2>/dev/null || echo "0")
EXITED_TRADES=$(curl -s http://localhost:8005/trades/exited | jq '.exited' 2>/dev/null || echo "0")
log "Active trades: $ACTIVE_TRADES, Exited: $EXITED_TRADES"
if [ "$EXITED_TRADES" != "0" ]; then
  echo "✅ SCENARIO 10 PASS" >> $LOGFILE
else
  echo "❌ SCENARIO 10 FAIL" >> $LOGFILE
fi

log "═══════════════════════════════════════════════"
log "All scenarios completed at $(date)"
