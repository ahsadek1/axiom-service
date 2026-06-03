-- GATE 9 Position Recording Schema
-- SQLite table for tracking submitted orders, fills, and positions

CREATE TABLE IF NOT EXISTS positions (
  order_id TEXT PRIMARY KEY,
  client_order_id TEXT UNIQUE,
  underlying TEXT NOT NULL,
  strategy TEXT NOT NULL,
  legs_json TEXT,  -- JSON array of leg objects
  side TEXT,       -- 'buy' or 'sell' (for single-leg orders)
  qty INT NOT NULL,
  fill_qty INT DEFAULT 0,
  status TEXT DEFAULT 'PENDING',  -- PENDING, FILLED, PARTIAL, FAILED
  entry_price REAL,
  entry_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  fill_price REAL,
  fill_time TIMESTAMP,
  macro_regime TEXT,
  risk_limit REAL,
  alpaca_order_id TEXT,  -- Alpaca-assigned order ID
  verdict TEXT,          -- PASS, CONDITIONAL, FAIL
  notes TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Index for fast lookups by order_id (already PK)
-- Index for fast lookups by client_order_id (already UNIQUE)
-- Index for status queries
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

-- Index for underlying + status (common query pattern)
CREATE INDEX IF NOT EXISTS idx_positions_underlying_status ON positions(underlying, status);

-- Index for timestamp queries (monitoring recent orders)
CREATE INDEX IF NOT EXISTS idx_positions_created_at ON positions(created_at DESC);
