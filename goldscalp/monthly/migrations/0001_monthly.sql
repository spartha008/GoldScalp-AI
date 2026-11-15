-- GoldScalp AI Phase 5: the ISOLATED monthly research store (var/monthly/monthly.sqlite3).
-- It is never the application database: Phase 5 only reads var/goldscalp.sqlite3 (read-only connections).

CREATE TABLE evaluations (
  id TEXT PRIMARY KEY,
  month TEXT NOT NULL,
  range_start TEXT NOT NULL,
  range_end TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('NOT STARTED', 'RESEARCHING', 'READY', 'RUNNING', 'COMPLETED', 'INCOMPLETE',
                                         'FAILED')),
  research_done INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  params TEXT NOT NULL,
  availability TEXT NOT NULL,
  frozen TEXT,
  research TEXT,
  phase TEXT,
  progress INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  app_version TEXT NOT NULL,
  error TEXT,
  finished_at TEXT
);

CREATE TABLE candidates (
  id TEXT PRIMARY KEY,
  evaluation_id TEXT NOT NULL REFERENCES evaluations(id),
  key TEXT NOT NULL,
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  concept TEXT NOT NULL,
  config TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  documentation TEXT NOT NULL,
  research TEXT NOT NULL,
  frozen_at TEXT NOT NULL,
  UNIQUE (evaluation_id, key)
);

CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  evaluation_id TEXT NOT NULL REFERENCES evaluations(id),
  strategy_key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('existing', 'candidate')),
  timeframe TEXT NOT NULL CHECK (timeframe IN ('M1', 'M5', 'M15')),
  status TEXT NOT NULL CHECK (status IN ('NOT STARTED', 'RUNNING', 'COMPLETED', 'INCOMPLETE', 'FAILED')),
  config TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  assumptions TEXT NOT NULL,
  dataset TEXT,
  result TEXT,
  result_hash TEXT,
  reason TEXT,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE (evaluation_id, strategy_key, timeframe)
);
CREATE INDEX runs_evaluation ON runs(evaluation_id);

-- Imported history (CSV) for months the application database does not cover. Same schema as the application's
-- candle tables, so the existing importer and loaders work unchanged; provenance per import.
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
