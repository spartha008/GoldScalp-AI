-- Optional MT5 DEMO-account execution (off by default). New tables only: no Phase 1-4 table is altered.

-- 1) Which monitored strategy versions the user linked to the demo account. One row per monitored version.
CREATE TABLE demo_links (
  monitored_id INTEGER PRIMARY KEY REFERENCES monitored_strategies(id),
  enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
  enabled_at TEXT,
  disabled_at TEXT,
  disabled_reason TEXT,
  server TEXT,                 -- the DEMO trade server confirmed when the link was enabled
  account_hash TEXT,           -- sha256 of the demo login (first 16 hex): detects a different account, stores no login
  updated_at TEXT NOT NULL
);

-- 2) One row per SIMULATED validation trade that was mirrored (or refused / rejected) on the demo account.
CREATE TABLE demo_trades (
  id INTEGER PRIMARY KEY,
  live_trade_id INTEGER NOT NULL UNIQUE REFERENCES live_trades(id),
  monitored_id INTEGER NOT NULL REFERENCES monitored_strategies(id),
  status TEXT NOT NULL CHECK (status IN ('open', 'closed', 'rejected', 'refused', 'error')),
  side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
  symbol TEXT,
  volume TEXT NOT NULL,
  requested_price TEXT,
  sl TEXT,
  tp TEXT,
  open_price TEXT,
  opened_at TEXT,
  position_ticket INTEGER,
  close_price TEXT,
  closed_at TEXT,
  close_reason TEXT,
  profit TEXT,
  commission TEXT,
  swap TEXT,
  message TEXT,
  server TEXT,
  events TEXT NOT NULL DEFAULT '[]',   -- every request and terminal reply, in order
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX demo_trades_status ON demo_trades(status);
