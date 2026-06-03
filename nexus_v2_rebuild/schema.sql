-- NEXUS IDEAL BUILD — Database Schema
-- Philosophy: Safety properties enforced by DB, not application code.
-- Authored: 2026-05-02 | Cipher spec + OMNI adversarial review
-- ============================================================

PRAGMA journal_mode=WAL;       -- concurrent reads + crash-safe writes
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;     -- safe + fast

-- ── Active Positions ─────────────────────────────────────────────────────────
-- The DB trigger enforces the 3-position cap.
-- Application code cannot bypass it — even a bug cannot open a 4th position.
-- Alpaca API is ground truth for combined cap (checked pre-INSERT by application).
-- This trigger is the local safety net.

CREATE TABLE IF NOT EXISTS active_positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT    NOT NULL,
    arena               TEXT    NOT NULL CHECK (arena IN ('alpha','prime')),
    client_order_id     TEXT    NOT NULL UNIQUE,          -- idempotency key
    alpaca_order_id     TEXT,                              -- filled after Alpaca confirms
    status              TEXT    NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','open','closed','cancelled')),
    strategy            TEXT    NOT NULL,
    direction           TEXT    NOT NULL CHECK (direction IN ('bullish','bearish')),
    max_risk_usd        REAL    NOT NULL CHECK (max_risk_usd > 0 AND max_risk_usd <= 1000),
    allocated_usd       REAL    NOT NULL CHECK (allocated_usd > 0),
    dte_at_entry        INTEGER NOT NULL CHECK (dte_at_entry BETWEEN 21 AND 60),
    entry_price         REAL,
    exit_price          REAL,
    pnl_usd             REAL    DEFAULT 0,
    entry_time          TEXT    NOT NULL DEFAULT (datetime('now')),
    exit_time           TEXT,
    expiry              TEXT    NOT NULL,
    pathway             TEXT,
    window_id           TEXT,
    synthesis_id        TEXT,
    notes               TEXT
);

-- Cap trigger: max 3 concurrent positions across the local DB.
-- Combined cap vs Railway enforced by Alpaca live query in application (C1 fix).
CREATE TRIGGER IF NOT EXISTS enforce_position_cap
BEFORE INSERT ON active_positions
WHEN (
    SELECT COUNT(*) FROM active_positions
    WHERE status IN ('pending','open')
) >= 3
BEGIN
    SELECT RAISE(ABORT, 'POSITION_CAP: max 3 concurrent positions — DB trigger');
END;

-- ── Capital Ledger ────────────────────────────────────────────────────────────
-- Atomic capital accounting. No CapitalPoolManager class needed.
-- All updates use BEGIN IMMEDIATE to prevent concurrent over-allocation.

CREATE TABLE IF NOT EXISTS capital_ledger (
    arena           TEXT    PRIMARY KEY,
    total_usd       REAL    NOT NULL CHECK (total_usd > 0),
    allocated_usd   REAL    NOT NULL DEFAULT 0 CHECK (allocated_usd >= 0),
    realized_pnl    REAL    NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (allocated_usd <= total_usd)
);

-- Seed initial capital pools
INSERT OR IGNORE INTO capital_ledger (arena, total_usd) VALUES ('alpha', 25000.0);
INSERT OR IGNORE INTO capital_ledger (arena, total_usd) VALUES ('prime', 25000.0);

-- ── Processed Picks ───────────────────────────────────────────────────────────
-- Replaces in-memory _picks_today and _processed_ids.
-- Survives process restarts. UNIQUE constraint prevents double-processing.
-- Railway writes submission records. OMNI local writes synthesis+execution records.

CREATE TABLE IF NOT EXISTS processed_picks (
    pick_id         TEXT    PRIMARY KEY,   -- deterministic: {date}-{ticker}-{window_id}
    ticker          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    window_id       TEXT    NOT NULL,
    arena           TEXT    NOT NULL,
    verdict         TEXT    NOT NULL CHECK (verdict IN ('GO','NO_GO','SKIP','ERROR')),
    pathway         TEXT,
    score           REAL,
    processed_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    processed_by    TEXT    NOT NULL DEFAULT 'omni',
    notes           TEXT
);

-- Index for daily lookups
CREATE INDEX IF NOT EXISTS idx_processed_picks_date
ON processed_picks (substr(window_id, 1, 10));

CREATE INDEX IF NOT EXISTS idx_processed_picks_ticker_date
ON processed_picks (ticker, substr(window_id, 1, 10));

-- ── Scan Cycles ───────────────────────────────────────────────────────────────
-- Audit trail of every scan cycle.
-- Exposes skip counts to /health — catches silent data failures (C2 fix).

CREATE TABLE IF NOT EXISTS scan_cycles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id       TEXT    NOT NULL,
    started_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT,
    pool_size       INTEGER NOT NULL DEFAULT 0,
    verdicts_go     INTEGER NOT NULL DEFAULT 0,
    verdicts_nogo   INTEGER NOT NULL DEFAULT 0,
    skips_total     INTEGER NOT NULL DEFAULT 0,
    skip_reasons    TEXT,           -- JSON: {"no_volatility_data": 3, "axiom_unavailable": 1}
    data_degraded   INTEGER NOT NULL DEFAULT 0,  -- boolean: 1 if fallbacks active
    degraded_sources TEXT,          -- JSON: ["orats"]
    uniform_score_flag INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);

-- ── Market State Log ──────────────────────────────────────────────────────────
-- Persists VIX readings so staleness can be detected after restart.

CREATE TABLE IF NOT EXISTS market_state_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vix             REAL,
    regime          TEXT,
    scanning_allowed INTEGER NOT NULL,
    source          TEXT    NOT NULL,   -- "polygon" | "fred_fallback" | "unavailable"
    recorded_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ── Preflight Results ─────────────────────────────────────────────────────────
-- Hard gate log. Trading does not open until preflight passes.

CREATE TABLE IF NOT EXISTS preflight_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date    TEXT    NOT NULL,
    passed          INTEGER NOT NULL,   -- boolean
    failures        TEXT,               -- JSON array of failure strings
    run_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    duration_ms     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_preflight_date
ON preflight_results (trading_date);

-- ── Data Source Health ────────────────────────────────────────────────────────
-- Tracks each external dependency status. Feeds /health degraded_sources field.

CREATE TABLE IF NOT EXISTS data_source_health (
    source          TEXT    PRIMARY KEY,
    status          TEXT    NOT NULL CHECK (status IN ('OK','DEGRADED','UNAVAILABLE')),
    last_success_at TEXT,
    last_failure_at TEXT,
    failure_reason  TEXT,
    fallback_active INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO data_source_health (source, status) VALUES
    ('orats',   'OK'),
    ('polygon', 'OK'),
    ('alpaca',  'OK'),
    ('axiom',   'OK'),
    ('fred',    'OK');
