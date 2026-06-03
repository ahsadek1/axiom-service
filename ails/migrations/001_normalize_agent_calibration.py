"""
Migration 001: Normalize agent_calibration.agent to lowercase.

Merges duplicate rows (e.g. 'Cipher' + 'cipher') using weighted rolling average
of predicted_confidence and actual_win_rate. Renames rows with no lowercase twin.

Idempotent — safe to run multiple times. Returns count of rows processed.
"""

import os
import sqlite3
from typing import Optional


DB_PATH = os.getenv("AILS_DB_PATH", "/Users/ahmedsadek/nexus/data/ails.db")


def run(db_path: Optional[str] = None) -> int:
    """
    Normalize all agent_calibration rows to lowercase agent names.

    For each mixed-case row:
      - If a lowercase twin exists: merge via weighted rolling average, delete mixed-case row.
      - If no twin exists: rename in-place to lowercase.

    Args:
        db_path: Override DB path (used in tests). Defaults to AILS_DB_PATH env var.

    Returns:
        Number of mixed-case rows processed (renamed or merged).
    """
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    mixed = cur.execute(
        "SELECT * FROM agent_calibration WHERE agent != LOWER(agent)"
    ).fetchall()

    processed = 0
    for row in mixed:
        normalized = row["agent"].lower()
        existing_lower = cur.execute(
            "SELECT * FROM agent_calibration WHERE agent=? AND strategy=? AND regime=?",
            (normalized, row["strategy"], row["regime"]),
        ).fetchone()

        if existing_lower:
            # Merge: weighted rolling average of both rows
            total_n = existing_lower["n"] + row["n"]
            merged_pred = (
                existing_lower["predicted_confidence"] * existing_lower["n"]
                + row["predicted_confidence"] * row["n"]
            ) / total_n
            merged_actual = (
                existing_lower["actual_win_rate"] * existing_lower["n"]
                + row["actual_win_rate"] * row["n"]
            ) / total_n
            cur.execute(
                "UPDATE agent_calibration "
                "SET predicted_confidence=?, actual_win_rate=?, n=?, last_updated=? "
                "WHERE agent=? AND strategy=? AND regime=?",
                (
                    merged_pred, merged_actual, total_n, row["last_updated"],
                    normalized, row["strategy"], row["regime"],
                ),
            )
            cur.execute(
                "DELETE FROM agent_calibration WHERE agent=? AND strategy=? AND regime=?",
                (row["agent"], row["strategy"], row["regime"]),
            )
        else:
            # No lowercase twin — rename in-place
            cur.execute(
                "UPDATE agent_calibration SET agent=? "
                "WHERE agent=? AND strategy=? AND regime=?",
                (normalized, row["agent"], row["strategy"], row["regime"]),
            )
        processed += 1

    conn.commit()
    conn.close()
    print(f"Migration 001 complete. Processed {processed} mixed-case rows.")
    return processed


if __name__ == "__main__":
    run()
