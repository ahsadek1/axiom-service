-- PERMANENT L4 FIX: Add Equity Position Tracking
-- Deploy immediately (9:32 AM ET)
-- Author: Maximus
-- Status: CRITICAL

-- Create equity positions table (for simple stock positions)
CREATE TABLE IF NOT EXISTS equity_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    qty REAL NOT NULL,
    entry_price REAL,
    current_price REAL,
    entry_time TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    alpaca_verified BOOLEAN DEFAULT 0,
    last_verified_at TEXT
);

-- Create index for fast lookups
CREATE INDEX IF NOT EXISTS idx_equity_symbol ON equity_positions(symbol);
CREATE INDEX IF NOT EXISTS idx_equity_status ON equity_positions(status);

-- Insert current orphaned positions (AEM, NET)
INSERT OR REPLACE INTO equity_positions 
(symbol, qty, entry_price, current_price, entry_time, created_at, updated_at, status, alpaca_verified)
VALUES 
('AEM', 116, 178.57, 178.57, '2026-06-02T09:00:00Z', '2026-06-02T09:30:00Z', '2026-06-02T09:32:00Z', 'active', 1),
('NET', -32, 261.00, 261.00, '2026-06-02T09:05:00Z', '2026-06-02T09:30:00Z', '2026-06-02T09:32:00Z', 'active', 1);

-- Create audit log for L4 operations
CREATE TABLE IF NOT EXISTS l4_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    operation TEXT,
    symbol TEXT,
    status TEXT,
    error_msg TEXT
);

-- Log this fix
INSERT INTO l4_audit_log (timestamp, operation, symbol, status, error_msg)
VALUES 
('2026-06-02T13:32:00Z', 'PERMANENT_FIX', 'AEM', 'INSERTED', NULL),
('2026-06-02T13:32:00Z', 'PERMANENT_FIX', 'NET', 'INSERTED', NULL),
('2026-06-02T13:32:00Z', 'L4_SCHEMA_UPDATE', NULL, 'CREATED', 'equity_positions table created');

-- Verify inserts
SELECT 'VERIFICATION' as step;
SELECT COUNT(*) as equity_positions_count FROM equity_positions WHERE status = 'active';
SELECT symbol, qty, status FROM equity_positions WHERE symbol IN ('AEM', 'NET');
