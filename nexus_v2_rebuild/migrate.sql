-- NEXUS V2 Migration Script
-- ADDITIVE ONLY — preserves all existing data
-- Run against: /Users/ahmedsadek/nexus/data/alpha_execution.db
-- Date: 2026-05-02

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ── New V2 tables (CREATE IF NOT EXISTS — safe to re-run) ─────────────────────

-- active_positions: V2 replacement for positions table
-- Includes DB trigger for position cap enforcement
-- Existing 'positions' table is preserved untouched

CREATE TABLE IF NOT EXISTS active_positions_v2 (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT    NOT NULL,
    arena               TEXT    NOT NULL DEFAULT 'alpha' CHECK (arena IN ('alpha','prime')),
    client_order_id     TEXT    NOT NULL UNIQUE,
    alpaca_order_id     TEXT,
    status              TEXT    NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','open','closed','cancelled')),
    strategy            TEXT    NOT NULL DEFAULT 'bull_put_spread',
    direction           TEXT    NOT NULL DEFAULT 'bullish' CHECK (direction IN ('bullish','bearish')),
    max_risk_usd        REAL    NOT NULL DEFAULT 1000.0 CHECK (max_risk_usd > 0 AND max_risk_usd <= 1000),
    allocated_usd       REAL    NOT NULL DEFAULT 750.0 CHECK (allocated_usd > 0),
    dte_at_entry        INTEGER NOT NULL DEFAULT 35 CHECK (dte_at_entry BETWEEN 21 AND 60),
    entry_price         REAL,
    exit_price          REAL,
    pnl_usd             REAL    DEFAULT 0,
    entry_time          TEXT    NOT NULL DEFAULT (datetime('now')),
    exit_time           TEXT,
    expiry              TEXT    NOT NULL DEFAULT '',
    pathway             TEXT,
    window_id           TEXT,
    synthesis_id        TEXT,
    notes               TEXT
);

-- Position cap trigger: max 3 concurrent positions
CREATE TRIGGER IF NOT EXISTS enforce_position_cap_v2
BEFORE INSERT ON active_positions_v2
WHEN (
    SELECT COUNT(*) FROM active_positions_v2
    WHERE status IN ('pending','open')
) >= 3
BEGIN
    SELECT RAISE(ABORT, 'POSITION_CAP_V2: max 3 concurrent positions');
END;

-- processed_picks: replaces in-memory _picks_today / _processed_ids
-- UNIQUE constraint = survives restarts, no double-processing
CREATE TABLE IF NOT EXISTS processed_picks (
    pick_id         TEXT    PRIMARY KEY,
    ticker          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    window_id       TEXT    NOT NULL,
    arena           TEXT    NOT NULL DEFAULT 'alpha',
    verdict         TEXT    NOT NULL CHECK (verdict IN ('GO','NO_GO','SKIP','ERROR')),
    pathway         TEXT,
    score           REAL,
    processed_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    processed_by    TEXT    NOT NULL DEFAULT 'omni',
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_processed_picks_date
ON processed_picks (substr(window_id, 1, 10));

CREATE INDEX IF NOT EXISTS idx_processed_picks_ticker_date
ON processed_picks (ticker, substr(window_id, 1, 10));

-- capital_ledger: atomic capital accounting
-- BEGIN IMMEDIATE transactions prevent concurrent over-allocation
CREATE TABLE IF NOT EXISTS capital_ledger (
    arena           TEXT    PRIMARY KEY,
    total_usd       REAL    NOT NULL CHECK (total_usd > 0),
    allocated_usd   REAL    NOT NULL DEFAULT 0 CHECK (allocated_usd >= 0),
    realized_pnl    REAL    NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (allocated_usd <= total_usd)
);

INSERT OR IGNORE INTO capital_ledger (arena, total_usd) VALUES ('alpha', 25000.0);
INSERT OR IGNORE INTO capital_ledger (arena, total_usd) VALUES ('prime', 25000.0);

-- scan_cycles: audit trail of every scan cycle
-- Exposes skip counts — no more silent dry cycles
CREATE TABLE IF NOT EXISTS scan_cycles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id       TEXT    NOT NULL,
    started_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT,
    pool_size       INTEGER NOT NULL DEFAULT 0,
    verdicts_go     INTEGER NOT NULL DEFAULT 0,
    verdicts_nogo   INTEGER NOT NULL DEFAULT 0,
    skips_total     INTEGER NOT NULL DEFAULT 0,
    skip_reasons    TEXT,
    data_degraded   INTEGER NOT NULL DEFAULT 0,
    degraded_sources TEXT,
    uniform_score_flag INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);

-- preflight_results: hard gate log
CREATE TABLE IF NOT EXISTS preflight_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date    TEXT    NOT NULL,
    passed          INTEGER NOT NULL,
    failures        TEXT,
    run_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    duration_ms     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_preflight_date
ON preflight_results (trading_date);

-- data_source_health: feeds /health degraded_sources
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

-- market_state_log: VIX history
CREATE TABLE IF NOT EXISTS market_state_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vix             REAL,
    regime          TEXT,
    scanning_allowed INTEGER NOT NULL,
    source          TEXT    NOT NULL,
    recorded_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- system_flags already exists — add V2 pause flag if not present
INSERT OR IGNORE INTO system_flags (key, value, updated_at)
VALUES ('v2_paused', '0', datetime('now'));
