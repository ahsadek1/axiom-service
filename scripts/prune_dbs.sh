#!/bin/bash
# prune_dbs.sh — Nexus database pruning
# Prevents unbounded growth in high-volume tables.
# Run daily via launchd.

NEXUS_DIR="$HOME/nexus"
echo "$(date): Starting DB pruning..."

python3 << 'PYEOF'
import sqlite3, os
from datetime import datetime, timedelta

nexus = os.path.expanduser("~/nexus/data")

def prune(db_path, table, date_col, days):
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(db_path)
    before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.execute(f"DELETE FROM {table} WHERE {date_col} < ?", (cutoff,))
    conn.commit()
    after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()
    conn2 = sqlite3.connect(db_path)
    conn2.execute("VACUUM")
    conn2.close()
    print(f"  {table}: {before} → {after} (pruned {before-after})")

prune(f"{nexus}/pipeline_sentinel.db", "health_scores", "created_at", 7)
prune(f"{nexus}/oracle.db", "api_calls", "called_at", 30)
prune(f"{nexus}/oracle.db", "failover_events", "failed_at", 30)

# message_bus — check for delivered messages
db = f"{nexus}/message_bus.db"
conn = sqlite3.connect(db)
schema = [c[1] for c in conn.execute("PRAGMA table_info(messages)").fetchall()]
if "delivered" in schema and "created_at" in schema:
    cutoff = (datetime.utcnow() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.execute("DELETE FROM messages WHERE delivered=1 AND created_at < ?", (cutoff,))
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    print(f"  message_bus delivered messages: {before} → {after}")
else:
    conn.close()

print("Pruning complete")
PYEOF
