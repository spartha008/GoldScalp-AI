-- Manual orders on the MT5 DEMO account from the Dashboard order ticket. New table only: no existing table is altered.
-- One row per request (open, change stop-loss / take-profit, close), written BEFORE anything is sent, with the result.
CREATE TABLE manual_orders (
  id INTEGER PRIMARY KEY,
  action TEXT NOT NULL CHECK (action IN ('open', 'modify', 'close')),
  status TEXT NOT NULL CHECK (status IN ('pending', 'done', 'rejected', 'refused', 'error')),
  side TEXT CHECK (side IN ('buy', 'sell')),
  symbol TEXT,
  volume TEXT,
  sl TEXT,
  tp TEXT,
  position_ticket INTEGER,
  price TEXT,                  -- the fill price the terminal reported
  risk_usd TEXT,               -- estimated loss at the stop-loss when the order was sent (spread included)
  reward_usd TEXT,             -- estimated gain at the take-profit
  server TEXT,
  message TEXT,
  request TEXT,                -- the request sent to the terminal (JSON)
  reply TEXT,                  -- the terminal's reply (JSON)
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX manual_orders_created ON manual_orders(created_at);
