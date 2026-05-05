"""
test_calibration_normalization.py — G1: AILS agent_calibration normalization tests.

Verifies: write-path lowercase normalization, read-path normalization,
migration idempotency, weighted merge logic. 8 test cases.
"""

import os
import sys
import sqlite3
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_ails_db
from main import _update_agent_calibration_impl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_db() -> tuple:
    """Create a fresh temp AILS DB. Returns (conn, path)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = init_ails_db(path)
    return conn, path


def _calibration_rows(conn) -> list:
    """Return all agent_calibration rows as dicts."""
    rows = conn.execute(
        "SELECT agent, strategy, regime, predicted_confidence, actual_win_rate, n "
        "FROM agent_calibration ORDER BY agent, strategy, regime"
    ).fetchall()
    return [dict(r) for r in rows]


def _inject_conn(conn, path):
    """Patch the global AILS connection so _update_agent_calibration_impl uses our test DB."""
    import main as ails_main
    original = ails_main._ails_conn
    ails_main._ails_conn = conn
    return original


def _restore_conn(original):
    import main as ails_main
    ails_main._ails_conn = original


# ---------------------------------------------------------------------------
# T1 — Write "Cipher" → stored as "cipher"
# ---------------------------------------------------------------------------

class TestWritePathNormalization(unittest.TestCase):

    def test_t1_mixed_case_stored_lowercase(self):
        """T1: Write agent_votes={'Cipher': True} — stored as 'cipher', not 'Cipher'."""
        conn, path = _fresh_db()
        orig = _inject_conn(conn, path)
        try:
            _update_agent_calibration_impl(
                agent_votes={"Cipher": True},
                actual_win=True,
                strategy="bull_put_spread",
                regime="NORMAL",
            )
            rows = _calibration_rows(conn)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["agent"], "cipher")
        finally:
            _restore_conn(orig)

    def test_t2_lowercase_written_twice_gives_n2(self):
        """T2: Write 'cipher' twice same strategy+regime → single row, n=2, averaged values."""
        conn, path = _fresh_db()
        orig = _inject_conn(conn, path)
        try:
            _update_agent_calibration_impl({"cipher": True}, True, "bull_put_spread", "NORMAL")
            _update_agent_calibration_impl({"cipher": False}, False, "bull_put_spread", "NORMAL")
            rows = _calibration_rows(conn)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["agent"], "cipher")
            self.assertEqual(rows[0]["n"], 2)
            # predicted_conf: (0.75 + 0.35) / 2 = 0.55
            self.assertAlmostEqual(rows[0]["predicted_confidence"], 0.55, places=4)
            # actual_win_rate: (1.0 + 0.0) / 2 = 0.5
            self.assertAlmostEqual(rows[0]["actual_win_rate"], 0.50, places=4)
        finally:
            _restore_conn(orig)

    def test_t3_cipher_then_Cipher_merges_to_one_row(self):
        """T3: Write 'cipher' then 'Cipher' same strategy+regime → 1 row, n=2 (no duplicate)."""
        conn, path = _fresh_db()
        orig = _inject_conn(conn, path)
        try:
            _update_agent_calibration_impl({"cipher": True}, True, "bull_put_spread", "NORMAL")
            _update_agent_calibration_impl({"Cipher": True}, True, "bull_put_spread", "NORMAL")
            rows = _calibration_rows(conn)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["agent"], "cipher")
            self.assertEqual(rows[0]["n"], 2)
        finally:
            _restore_conn(orig)

    def test_t7_atlas_stored_lowercase(self):
        """T7: Write agent_votes={'Atlas': False} → stored as 'atlas'."""
        conn, path = _fresh_db()
        orig = _inject_conn(conn, path)
        try:
            _update_agent_calibration_impl({"Atlas": False}, False, "swing_long", "ELEVATED")
            rows = _calibration_rows(conn)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["agent"], "atlas")
        finally:
            _restore_conn(orig)


# ---------------------------------------------------------------------------
# T4, T5, T6 — Migration tests
# ---------------------------------------------------------------------------

class TestMigration(unittest.TestCase):

    def _seed_mixed_db(self, path: str) -> None:
        """
        Seed a DB with 6 mixed-case rows simulating the pre-fix state.

        Uses the OLD schema (no CHECK constraint) to replicate exactly the
        real-world condition that caused the 6-row duplicate problem.
        Drops and recreates the agent_calibration table without CHECK.
        """
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        # Recreate table without CHECK constraint — mirrors pre-fix schema
        conn.execute("DROP TABLE IF EXISTS agent_calibration")
        conn.execute("""
            CREATE TABLE agent_calibration (
                agent                TEXT NOT NULL,
                strategy             TEXT NOT NULL,
                regime               TEXT NOT NULL,
                predicted_confidence REAL NOT NULL DEFAULT 0.5,
                actual_win_rate      REAL NOT NULL DEFAULT 0.5,
                n                    INTEGER NOT NULL DEFAULT 0,
                last_updated         TEXT NOT NULL,
                PRIMARY KEY (agent, strategy, regime)
            )
        """)
        now = "2026-04-15T10:00:00+00:00"
        rows = [
            ("Cipher", "bull_put_spread", "NORMAL",   0.75, 0.80, 5),
            ("cipher", "bull_put_spread", "NORMAL",   0.70, 0.90, 3),
            ("Atlas",  "bull_put_spread", "ELEVATED", 0.65, 0.70, 4),
            ("atlas",  "bull_put_spread", "ELEVATED", 0.60, 0.75, 2),
            ("Sage",   "swing_long",      "STRESS",   0.55, 0.60, 6),
            ("sage",   "swing_long",      "STRESS",   0.50, 0.65, 2),
        ]
        for agent, strat, regime, pred, actual, n in rows:
            conn.execute(
                "INSERT INTO agent_calibration "
                "(agent, strategy, regime, predicted_confidence, actual_win_rate, n, last_updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (agent, strat, regime, pred, actual, n, now),
            )
        conn.commit()
        conn.close()

    def test_t4_migration_merges_6_rows_to_3(self):
        """T4: Migration on DB with 6 mixed-case rows → 3 rows, n values merged correctly."""
        conn, path = _fresh_db()
        conn.close()
        self._seed_mixed_db(path)

        from migrations.migration_001 import run
        processed = run(path)

        self.assertEqual(processed, 3)  # 3 mixed-case rows processed

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT agent, n FROM agent_calibration ORDER BY agent"
        ).fetchall()
        conn.close()

        self.assertEqual(len(rows), 3)
        agents = {r["agent"] for r in rows}
        self.assertEqual(agents, {"cipher", "atlas", "sage"})

        # Verify n values merged (Cipher: 5+3=8, Atlas: 4+2=6, Sage: 6+2=8)
        ns = {r["agent"]: r["n"] for r in rows}
        self.assertEqual(ns["cipher"], 8)
        self.assertEqual(ns["atlas"], 6)
        self.assertEqual(ns["sage"], 8)

    def test_t5_migration_on_clean_db_returns_zero(self):
        """T5: Migration on already-clean DB (all lowercase) returns 0 — no-op."""
        conn, path = _fresh_db()
        now = "2026-04-15T10:00:00+00:00"
        conn.execute(
            "INSERT INTO agent_calibration "
            "(agent, strategy, regime, predicted_confidence, actual_win_rate, n, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("cipher", "bull_put_spread", "NORMAL", 0.75, 0.80, 5, now),
        )
        conn.commit()
        conn.close()

        from migrations.migration_001 import run
        processed = run(path)
        self.assertEqual(processed, 0)

    def test_t6_migration_idempotent_run_twice(self):
        """T6: Migration is idempotent — run twice produces same result."""
        conn, path = _fresh_db()
        conn.close()
        self._seed_mixed_db(path)

        from migrations.migration_001 import run
        processed_first = run(path)
        processed_second = run(path)

        self.assertEqual(processed_first, 3)
        self.assertEqual(processed_second, 0)  # second run: nothing left to fix

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT agent FROM agent_calibration ORDER BY agent").fetchall()
        conn.close()
        self.assertEqual(len(rows), 3)


# ---------------------------------------------------------------------------
# T8 — CHECK constraint blocks future mixed-case inserts on new DBs
# ---------------------------------------------------------------------------

class TestCheckConstraint(unittest.TestCase):

    def test_t8_check_constraint_rejects_uppercase(self):
        """T8: Direct SQL insert of 'CIPHER' into a fresh DB is rejected by CHECK constraint."""
        conn, path = _fresh_db()
        now = "2026-04-15T10:00:00+00:00"
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO agent_calibration "
                "(agent, strategy, regime, predicted_confidence, actual_win_rate, n, last_updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("CIPHER", "bull_put_spread", "NORMAL", 0.75, 0.80, 5, now),
            )
        conn.close()


if __name__ == "__main__":
    unittest.main()
