-- GoldScalp AI schema v1. Never edit after release; add 0002_*.sql instead.

-- One series per data origin. Live provider data and each CSV source are kept
-- apart so provenance is never mixed.
CREATE TABLE series (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  symbol TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('live', 'historical')),
  source_id TEXT NOT NULL,
  source_symbol TEXT NOT NULL,
  candle_side TEXT NOT NULL CHECK (candle_side IN ('bid', 'ask', 'mid', 'last', 'unknown')),
  price_decimals INTEGER NOT NULL CHECK (price_decimals BETWEEN 0 AND 8),
  description TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE data_imports (
  id INTEGER PRIMARY KEY,
  series_id INTEGER NOT NULL REFERENCES series(id),
  tf TEXT NOT NULL CHECK (tf IN ('M1', 'M5', 'M15', 'H1')),
  origin TEXT NOT NULL,
  dialect TEXT,
  file_name TEXT,
  content_sha256 TEXT NOT NULL,
  time_basis TEXT NOT NULL,
  rows_read INTEGER NOT NULL,
  rows_stored INTEGER NOT NULL,
  rows_rejected INTEGER NOT NULL,
  duplicates_dropped INTEGER NOT NULL,
  first_ts INTEGER,
  last_ts INTEGER,
  report_json TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  UNIQUE (series_id, tf, content_sha256)
);

-- Prices are stored as integers scaled by 10^series.price_decimals.
CREATE TABLE candles (
  series_id INTEGER NOT NULL REFERENCES series(id),
  tf TEXT NOT NULL CHECK (tf IN ('M1', 'M5', 'M15', 'H1')),
  ts INTEGER NOT NULL,
  o INTEGER NOT NULL,
  h INTEGER NOT NULL,
  l INTEGER NOT NULL,
  c INTEGER NOT NULL,
  tick_volume INTEGER CHECK (tick_volume IS NULL OR tick_volume >= 0),
  spread INTEGER CHECK (spread IS NULL OR spread >= 0),
  closed INTEGER NOT NULL CHECK (closed IN (0, 1)),
  origin TEXT NOT NULL,
  import_id INTEGER REFERENCES data_imports(id),
  updated_at TEXT NOT NULL,
  CHECK (l > 0 AND l <= o AND l <= c AND h >= o AND h >= c),
  PRIMARY KEY (series_id, tf, ts)
) WITHOUT ROWID;

CREATE TABLE instrument_specs (
  id INTEGER PRIMARY KEY,
  fingerprint TEXT NOT NULL UNIQUE,
  spec_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE paper_accounts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  currency TEXT NOT NULL CHECK (currency = 'USD'),
  starting_balance TEXT NOT NULL,
  leverage INTEGER NOT NULL CHECK (leverage >= 1),
  instrument_spec_id INTEGER NOT NULL REFERENCES instrument_specs(id),
  cost_model_json TEXT NOT NULL,
  max_quote_age_seconds INTEGER NOT NULL CHECK (max_quote_age_seconds > 0),
  allow_delayed_fills INTEGER NOT NULL CHECK (allow_delayed_fills IN (0, 1)),
  active INTEGER NOT NULL CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL
);

CREATE TABLE paper_positions (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES paper_accounts(id),
  side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
  lots TEXT NOT NULL,
  ounces TEXT NOT NULL,
  open_price TEXT NOT NULL,
  open_time TEXT NOT NULL,
  open_quote_time TEXT NOT NULL,
  open_price_state TEXT NOT NULL,
  open_spread TEXT NOT NULL,
  open_spread_source TEXT NOT NULL,
  open_slippage TEXT NOT NULL,
  open_commission TEXT NOT NULL,
  margin TEXT NOT NULL,
  stop_loss TEXT,
  take_profit TEXT,
  status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
  close_price TEXT,
  close_time TEXT,
  close_quote_time TEXT,
  close_price_state TEXT,
  close_commission TEXT,
  close_reason TEXT CHECK (close_reason IS NULL OR close_reason IN ('manual', 'stop_loss', 'take_profit')),
  realized_gross TEXT,
  realized_net TEXT
);
CREATE INDEX paper_positions_account_status ON paper_positions(account_id, status);

-- Every balance change is a ledger row; balance = SUM(amount).
CREATE TABLE account_ledger (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES paper_accounts(id),
  ts TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('deposit', 'commission', 'realized_pnl')),
  amount TEXT NOT NULL,
  balance_after TEXT NOT NULL,
  position_id INTEGER REFERENCES paper_positions(id),
  note TEXT NOT NULL
);
CREATE INDEX account_ledger_account ON account_ledger(account_id, id);

CREATE TABLE equity_snapshots (
  account_id INTEGER NOT NULL REFERENCES paper_accounts(id),
  ts TEXT NOT NULL,
  balance TEXT NOT NULL,
  equity TEXT NOT NULL,
  used_margin TEXT NOT NULL,
  free_margin TEXT NOT NULL,
  open_positions INTEGER NOT NULL,
  price_state TEXT NOT NULL,
  PRIMARY KEY (account_id, ts)
);
