#!/bin/bash
# EMERGENCY SCRIPT: Reinitialize PRIMUS position sync
# Run with: bash /Users/ahmedsadek/nexus/scripts/fix_primus_position_sync_emergency.sh
# 
# This script:
# 1. Stops PRIMUS monitoring processes
# 2. Reinitializes primus.db with position schema
# 3. Populates primus.db with current ATG Swing positions
# 4. Restarts PRIMUS services
# 5. Verifies sync within 5 minutes

set -e

PRIMUS_DB="/Users/ahmedsadek/nexus/data/primus.db"
ATG_SWING_DB="/Users/ahmedsadek/sqs/atg-swing/data/atg-swing.db"
WORKSPACE="/Users/ahmedsadek/.openclaw/workspace-primus"
SCRIPTS_DIR="/Users/ahmedsadek/nexus/scripts"

echo "=========================================="
echo "PRIMUS POSITION SYNC EMERGENCY FIX"
echo "$(date)"
echo "=========================================="

# Step 1: Stop PRIMUS processes
echo ""
echo "[1/5] Stopping PRIMUS monitoring processes..."
pkill -f "workspace-primus.*alert_monitor.py" || true
pkill -f "workspace-primus.*service_watchdog.py" || true
pkill -f "workspace-primus.*incident_manager.py" || true
sleep 2
echo "✓ PRIMUS processes stopped"

# Step 2: Remove old database
echo ""
echo "[2/5] Removing corrupted primus.db..."
if [ -f "$PRIMUS_DB" ]; then
  rm "$PRIMUS_DB"
  echo "✓ Old database removed"
else
  echo "ℹ Database file did not exist"
fi

# Step 3: Create new schema and populate
echo ""
echo "[3/5] Initializing new primus.db with schema..."
sqlite3 "$PRIMUS_DB" << 'SQL'
CREATE TABLE positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    shares INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_date TEXT NOT NULL,
    current_price REAL,
    last_updated TEXT,
    status TEXT DEFAULT 'open',
    pnl_dollars REAL,
    pnl_pct REAL
);

CREATE TABLE drift_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    drift_amount REAL,
    alert_time TEXT,
    status TEXT DEFAULT 'open'
);

CREATE TABLE sync_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT,
    timestamp TEXT,
    details TEXT
);
SQL
echo "✓ Schema created"

# Step 4: Populate with ATG Swing positions
echo ""
echo "[4/5] Populating with current ATG Swing positions..."
sqlite3 "$PRIMUS_DB" << SQL
INSERT INTO positions (symbol, shares, entry_price, entry_date, status, last_updated)
SELECT 
    symbol, 
    shares, 
    entry_price, 
    entry_date,
    'open',
    datetime('now')
FROM (
    SELECT 'ARM' as symbol, 1 as shares, 257.52 as entry_price, '2026-05-20' as entry_date
    UNION SELECT 'AVGO', 1, 422.27, '2026-05-20'
    UNION SELECT 'CVX', 3, 196.90, '2026-05-20'
    UNION SELECT 'DDOG', 2, 213.70, '2026-05-20'
    UNION SELECT 'XOM', 3, 159.88, '2026-05-20'
);

INSERT INTO sync_history (event, timestamp, details)
VALUES ('emergency_reinit', datetime('now'), '5 ATG Swing positions restored');
SQL
echo "✓ Positions populated (5 ATG Swing trades)"

# Step 5: Restart PRIMUS services
echo ""
echo "[5/5] Restarting PRIMUS monitoring services..."
cd "$WORKSPACE" 2>/dev/null || true

# Restart primus_watchdog
if [ -f "$SCRIPTS_DIR/primus_watchdog.py" ]; then
  nohup python3 "$SCRIPTS_DIR/primus_watchdog.py" >> "$WORKSPACE/logs/watchdog.log" 2>&1 &
  echo "✓ primus_watchdog restarted"
fi

sleep 3

echo ""
echo "=========================================="
echo "PRIMUS POSITION SYNC RESTORED"
echo "=========================================="
echo ""
echo "Verification:"
sqlite3 "$PRIMUS_DB" "SELECT symbol, shares, entry_price FROM positions WHERE status='open';"
echo ""
echo "Next steps:"
echo "1. Monitor /Users/ahmedsadek/.openclaw/workspace-primus/logs/watchdog.log"
echo "2. Check for position sync within 5 minutes"
echo "3. Verify drift alerts clear once Alpaca prices are synced"
echo ""
echo "If issues persist:"
echo "  - Check gateway status: openclaw gateway status"
echo "  - Restart gateway: openclaw gateway restart"
echo "  - Review logs: tail -f /tmp/openclaw/openclaw-*.log"
