"""
seed_bayesian_priors.py — Seed AILS bayesian_rates from backtest historical data.

This is the critical bridge between the backtest populator and the live AILS system.
Without this step, AILS has no priors and returns 0.5 neutral for every ticker.

What this does:
  - Reads all rows from backtest.db → historical_win_rates
  - Writes each as a prior into ails.db → bayesian_rates
    (prior_rate = historical win_rate, prior_n = sample_count, live_n = 0)
  - Idempotent: uses INSERT OR REPLACE — safe to re-run after backtest updates

Run once after any backtest population or re-population.
Usage:
    cd /Users/ahmedsadek/nexus/ails
    source .venv/bin/activate && python seed_bayesian_priors.py
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("ails.seed")

AILS_DB     = os.environ.get("AILS_DB_PATH",    "/Users/ahmedsadek/nexus/data/ails.db")
BACKTEST_DB = os.environ.get("BACKTEST_DB_PATH", "/Users/ahmedsadek/nexus/data/backtest.db")


def seed_priors() -> None:
    """Read all historical win rates and write them as Bayesian priors into AILS."""

    bt  = sqlite3.connect(BACKTEST_DB)
    bt.row_factory = sqlite3.Row
    ails = sqlite3.connect(AILS_DB)

    # Fetch all backtest win rates
    rows = bt.execute(
        "SELECT ticker, strategy, regime, direction, win_rate, sample_count "
        "FROM historical_win_rates"
    ).fetchall()

    log.info("Seeding %d backtest rows into bayesian_rates...", len(rows))

    now = datetime.now(timezone.utc).isoformat()
    seeded = 0
    skipped = 0

    for row in rows:
        ticker       = row["ticker"]
        strategy     = row["strategy"]
        regime       = row["regime"]
        direction    = row["direction"]
        win_rate     = row["win_rate"]
        sample_count = row["sample_count"]

        if sample_count < 5:
            skipped += 1
            continue  # Not enough data to be a meaningful prior

        # Compute confidence label based on sample count
        if sample_count >= 200:
            confidence = "high"
        elif sample_count >= 50:
            confidence = "medium"
        else:
            confidence = "low"

        # INSERT OR REPLACE — safe to re-run. live_n=0 since these are all priors.
        ails.execute(
            """
            INSERT OR REPLACE INTO bayesian_rates
                (ticker, strategy, regime, direction,
                 blended_rate, confidence, prior_n, live_n, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (ticker, strategy, regime, direction,
             round(win_rate, 4), confidence, sample_count, now),
        )
        seeded += 1

    ails.commit()

    # Verify
    total = ails.execute("SELECT COUNT(*) FROM bayesian_rates").fetchone()[0]
    tickers = ails.execute("SELECT COUNT(DISTINCT ticker) FROM bayesian_rates").fetchone()[0]

    log.info("=" * 60)
    log.info("SEEDING COMPLETE")
    log.info("  Rows seeded: %d | Skipped (n<5): %d", seeded, skipped)
    log.info("  bayesian_rates total: %d rows across %d tickers", total, tickers)
    log.info("=" * 60)

    bt.close()
    ails.close()


if __name__ == "__main__":
    seed_priors()
