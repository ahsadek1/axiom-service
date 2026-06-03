"""
fix_prime_ghost_positions.py

One-shot cleanup script for Prime ghost positions (April 24, 2026 stale records).
These positions were opened AND closed on Alpaca but Prime DB was never updated.
All 7 orders confirmed filled then sold via exit monitor.

Run: python3 fix_prime_ghost_positions.py
"""

import os
import sqlite3
import requests
from datetime import datetime, timezone


def _require(var: str) -> str:
    """Return env var value or raise at startup — never silently use a stale fallback."""
    val = os.getenv(var)
    if not val:
        raise RuntimeError(f"{var} is required but not set. Source .deploy-secrets before running.")
    return val


DB_PATH      = "/Users/ahmedsadek/nexus/data/prime_execution.db"
PRIME_URL    = "http://localhost:8006"
PRIME_SECRET = _require("NEXUS_SECRET")

# Verified data from Alpaca API (buy filled_avg, sell filled_avg, qty, sell filled_at)
POSITIONS_TO_CLOSE = [
    {
        "db_id": 2,
        "ticker": "BKR",
        "buy_avg": 67.03,
        "sell_avg": 67.25,
        "qty": 22.38,
        "closed_at": "2026-04-24T15:25:35+00:00",
        "alpaca_buy_id": "89658556-b608-42d4-991b-887017641a26",
        "alpaca_sell_id": "780b595e-0bc5-436d-ac43-1aef60584a45",
    },
    {
        "db_id": 3,
        "ticker": "CNP",
        "buy_avg": 42.910286,
        "sell_avg": 42.83,
        "qty": 34.96,
        "closed_at": "2026-04-24T15:30:39+00:00",
        "alpaca_buy_id": "5f6f50af-a961-4005-b566-f130766404c8",
        "alpaca_sell_id": "8ca44a01-47c1-48eb-bcf4-50008b65a10b",
    },
    {
        "db_id": 4,
        "ticker": "DLR",
        "buy_avg": 207.18,
        "sell_avg": 206.82,
        "qty": 7.24,
        "closed_at": "2026-04-24T15:30:36+00:00",
        "alpaca_buy_id": "81a4d9eb-b322-4f49-bc4e-47e3238fd9c0",
        "alpaca_sell_id": "f006692c-30f9-4b40-9210-b216f995fc74",
    },
    {
        "db_id": 5,
        "ticker": "DLR",
        "buy_avg": 207.32,
        "sell_avg": 207.09,
        "qty": 7.24,
        "closed_at": "2026-04-24T15:35:36+00:00",
        "alpaca_buy_id": "66fd7a02-006e-4729-8363-a3029c8510c0",
        "alpaca_sell_id": "7816b9f6-ffe9-43f3-8ba2-2d4a79efcf4c",
    },
    {
        "db_id": 6,
        "ticker": "KMI",
        "buy_avg": 31.23,
        "sell_avg": 31.25,
        "qty": 48.03,
        "closed_at": "2026-04-24T15:50:39+00:00",
        "alpaca_buy_id": "983a6eaa-390a-44d4-9d18-8d6fedc95a10",
        "alpaca_sell_id": "584bf326-a67e-4a2d-b0f5-cc08448d71e8",
    },
    {
        "db_id": 7,
        "ticker": "DLR",
        "buy_avg": 205.44,
        "sell_avg": 204.37,
        "qty": 7.29,
        "closed_at": "2026-04-24T16:10:44+00:00",
        "alpaca_buy_id": "63eb1afb-80b2-4604-bcf1-de8b061a0016",
        "alpaca_sell_id": "30a4d32f-5972-4f30-a25f-b6547f68f530",
    },
    {
        "db_id": 8,
        "ticker": "CBRE",
        "buy_avg": 148.71,
        "sell_avg": 148.34,
        "qty": 10.09,
        "closed_at": "2026-04-24T16:25:44+00:00",
        "alpaca_buy_id": "d9038285-943d-4bad-ae38-f365fb0653b0",
        "alpaca_sell_id": "da0f3c69-5729-4378-a57f-b1fabe79351e",
    },
]


def close_positions(conn: sqlite3.Connection) -> None:
    """Update all ghost positions to closed with verified PnL data."""
    cursor = conn.cursor()
    for pos in POSITIONS_TO_CLOSE:
        buy_avg = pos["buy_avg"]
        sell_avg = pos["sell_avg"]
        qty = pos["qty"]
        pnl_pct = (sell_avg - buy_avg) / buy_avg
        pnl_usd = (sell_avg - buy_avg) * qty

        cursor.execute(
            """
            UPDATE positions
            SET status         = 'closed',
                close_reason   = 'exit_monitor_sell_confirmed',
                closed_at      = ?,
                pnl_pct        = ?,
                pnl_usd        = ?
            WHERE id = ? AND status = 'open'
            """,
            (pos["closed_at"], round(pnl_pct, 6), round(pnl_usd, 4), pos["db_id"]),
        )
        rows = cursor.rowcount
        sign = "+" if pnl_usd >= 0 else ""
        print(
            f"  id={pos['db_id']} {pos['ticker']:6} | "
            f"buy={buy_avg:.4f} sell={sell_avg:.4f} qty={qty} | "
            f"pnl={sign}{pnl_pct*100:.3f}% ({sign}${pnl_usd:.2f}) | "
            f"rows_updated={rows}"
        )
    conn.commit()


def verify_clean(conn: sqlite3.Connection) -> int:
    """Return count of still-open positions."""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM positions WHERE status='open'")
    return cursor.fetchone()[0]


def resume_prime() -> bool:
    """Call POST /resume on Prime Execution service."""
    try:
        resp = requests.post(
            f"{PRIME_URL}/resume",
            headers={"X-Nexus-Prime-Secret": PRIME_SECRET},
            timeout=10,
        )
        print(f"  /resume response: {resp.status_code} — {resp.text[:200]}")
        return resp.status_code == 200
    except Exception as e:
        print(f"  /resume failed: {e}")
        return False


def check_prime_health() -> dict:
    """Check Prime health endpoint."""
    try:
        resp = requests.get(f"{PRIME_URL}/health", timeout=10)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def main() -> None:
    print("=" * 60)
    print("PRIME GHOST POSITION CLEANUP")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    # Step 1: Check health before
    print("\n[1] Pre-cleanup Prime health:")
    health_before = check_prime_health()
    print(f"  execution_paused={health_before.get('execution_paused')} | "
          f"open_positions={health_before.get('open_positions')} | "
          f"status={health_before.get('status')}")

    # Step 2: Close positions in DB
    print("\n[2] Closing ghost positions in DB:")
    conn = sqlite3.connect(DB_PATH)
    try:
        close_positions(conn)
    finally:
        conn.close()

    # Step 3: Verify clean
    conn = sqlite3.connect(DB_PATH)
    try:
        remaining = verify_clean(conn)
    finally:
        conn.close()
    print(f"\n[3] Verification: open_positions_remaining={remaining}")
    if remaining != 0:
        print(f"  ERROR: {remaining} positions still open — aborting resume")
        return

    # Step 4: Resume Prime execution
    print("\n[4] Calling POST /resume on Prime Execution:")
    resumed = resume_prime()

    # Step 5: Verify health after
    print("\n[5] Post-cleanup Prime health:")
    health_after = check_prime_health()
    print(f"  execution_paused={health_after.get('execution_paused')} | "
          f"open_positions={health_after.get('open_positions')} | "
          f"status={health_after.get('status')}")

    # Summary
    print("\n" + "=" * 60)
    if resumed and health_after.get("execution_paused") is False:
        print("RESULT: SUCCESS — Prime execution resumed. Clean slate.")
    elif resumed:
        print("RESULT: PARTIAL — /resume called but health still shows paused.")
    else:
        print("RESULT: MANUAL RESUME NEEDED — DB cleaned but /resume failed.")
    print("=" * 60)


if __name__ == "__main__":
    main()
