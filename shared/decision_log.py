"""
decision_log.py — Per-ticker agent decision audit log.

Records every ticker analyzed by a scanning agent (Cipher, Atlas, Sage),
including rejected picks, brain failures, and Oracle misses — not just
the ones that cleared the submission threshold.

Schema is created on first use. Safe to call with any SQLite DB path.

Commercial-grade rationale:
  Without this, post-trade forensics are impossible. When a bad trade goes
  wrong we need to know: which agent scored it, what signals it saw,
  what score it produced, why it was submitted (or not). Silent rejections
  also hide signal gaps (e.g. Sage rejecting 95% of its pool = calibration issue).

Added 2026-04-28 — closes commercial-grade gap: "no agent decision audit log".
"""

import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# Disposition constants
SUBMITTED   = "SUBMITTED"    # passed threshold, sent to buffer
REJECTED    = "REJECTED"     # analyzed but below threshold
BRAIN_FAIL  = "BRAIN_FAIL"   # AI brain returned None / error
ORACLE_MISS = "ORACLE_MISS"  # Oracle context unavailable
HALTED      = "HALTED"       # SOVEREIGN halt was active

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent           TEXT    NOT NULL,
    window_id       TEXT    NOT NULL,
    ticker          TEXT    NOT NULL,
    disposition     TEXT    NOT NULL,  -- SUBMITTED | REJECTED | BRAIN_FAIL | ORACLE_MISS | HALTED
    direction       TEXT,              -- BULLISH | BEARISH | NEUTRAL (null for non-analysis dispositions)
    score           REAL,              -- raw agent score (null for BRAIN_FAIL / ORACLE_MISS)
    threshold       REAL,              -- threshold that was applied at decision time
    reasoning       TEXT,              -- truncated reasoning from AI brain (first 500 chars)
    alpha_submitted INTEGER NOT NULL DEFAULT 0,
    prime_submitted INTEGER NOT NULL DEFAULT 0,
    regime          TEXT,              -- JSON blob of regime snapshot at analysis time
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_agent_date
    ON decisions(agent, created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_ticker_date
    ON decisions(ticker, created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_window
    ON decisions(window_id);
CREATE INDEX IF NOT EXISTS idx_decisions_disposition
    ON decisions(disposition, created_at);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def log_decision(
    db_path: str,
    agent: str,
    window_id: str,
    ticker: str,
    disposition: str,
    direction: Optional[str] = None,
    score: Optional[float] = None,
    threshold: Optional[float] = None,
    reasoning: Optional[str] = None,
    alpha_submitted: bool = False,
    prime_submitted: bool = False,
    regime: Optional[str] = None,
) -> None:
    """
    Persist a single agent decision to the audit log.

    Call this for EVERY ticker processed — submitted, rejected, failed, or missed.
    This is the forensic record of what the agent saw and decided.

    Args:
        db_path:         Path to the agent's SQLite database.
        agent:           Agent name (e.g. "cipher", "atlas", "sage").
        window_id:       Axiom window ID this analysis belongs to.
        ticker:          The ticker symbol analyzed.
        disposition:     One of: SUBMITTED, REJECTED, BRAIN_FAIL, ORACLE_MISS, HALTED.
        direction:       BULLISH / BEARISH / NEUTRAL (None if brain failed or Oracle missed).
        score:           Raw score from AI brain (None if brain failed).
        threshold:       The submission threshold applied (for context on rejections).
        reasoning:       AI reasoning string — truncated to 500 chars for storage.
        alpha_submitted: True if successfully sent to alpha-buffer.
        prime_submitted: True if successfully sent to prime-buffer.
        regime:          JSON string snapshot of the regime at analysis time.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        _ensure_schema(conn)
        conn.execute(
            """INSERT INTO decisions
               (agent, window_id, ticker, disposition, direction, score,
                threshold, reasoning, alpha_submitted, prime_submitted,
                regime, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent, window_id, ticker, disposition, direction,
                score, threshold,
                (reasoning[:500] if reasoning else None),
                int(alpha_submitted), int(prime_submitted),
                regime,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    except Exception as exc:
        log.error("[decision_log] Failed to log decision %s/%s/%s: %s",
                  agent, ticker, disposition, exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def query_decisions(
    db_path: str,
    agent: Optional[str] = None,
    ticker: Optional[str] = None,
    date_prefix: Optional[str] = None,
    disposition: Optional[str] = None,
    limit: int = 100,
) -> list:
    """
    Query the decision audit log with optional filters.

    Args:
        db_path:      Path to the agent's SQLite database.
        agent:        Filter by agent name.
        ticker:       Filter by ticker.
        date_prefix:  Filter by ISO date prefix (e.g. "2026-04-28").
        disposition:  Filter by disposition (SUBMITTED, REJECTED, etc.).
        limit:        Max rows to return.

    Returns:
        List of row dicts with all decision fields.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        _ensure_schema(conn)
        conn.row_factory = sqlite3.Row
        clauses, params = [], []
        if agent:
            clauses.append("agent = ?"); params.append(agent)
        if ticker:
            clauses.append("ticker = ?"); params.append(ticker)
        if date_prefix:
            clauses.append("created_at LIKE ?"); params.append(f"{date_prefix}%")
        if disposition:
            clauses.append("disposition = ?"); params.append(disposition)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM decisions {where} ORDER BY created_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.error("[decision_log] query failed: %s", exc)
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def daily_summary(db_path: str, agent: str, date_prefix: Optional[str] = None) -> dict:
    """
    Return a summary dict for an agent's decisions on a given date.

    Returns counts per disposition, average score for submitted/rejected picks,
    and top 5 submitted tickers by score.

    Useful for the blocker sweep Phase 2 and EOD reports.
    """
    if not date_prefix:
        date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        _ensure_schema(conn)
        # Counts per disposition
        rows = conn.execute(
            """SELECT disposition, COUNT(*) as cnt, AVG(score) as avg_score
               FROM decisions
               WHERE agent = ? AND created_at LIKE ?
               GROUP BY disposition""",
            (agent, f"{date_prefix}%"),
        ).fetchall()
        summary = {r[0]: {"count": r[1], "avg_score": round(r[2], 1) if r[2] else None}
                   for r in rows}
        # Top 5 submitted tickers by score
        top = conn.execute(
            """SELECT ticker, direction, score, reasoning
               FROM decisions
               WHERE agent = ? AND created_at LIKE ? AND disposition = 'SUBMITTED'
               ORDER BY score DESC LIMIT 5""",
            (agent, f"{date_prefix}%"),
        ).fetchall()
        summary["top_submitted"] = [
            {"ticker": r[0], "direction": r[1], "score": r[2]}
            for r in top
        ]
        conn.close()
        return summary
    except Exception as exc:
        log.error("[decision_log] daily_summary failed: %s", exc)
        return {}
