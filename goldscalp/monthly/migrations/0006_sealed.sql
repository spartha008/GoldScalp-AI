-- GoldScalp AI Phase 6 H6.4 (additive; research store only): its own run history. One development (debugging) run and
-- the one sealed validation run; every attempt is kept. Earlier tables untouched.

CREATE TABLE sealed_runs (
  id TEXT PRIMARY KEY,
  hypothesis TEXT NOT NULL,
  purpose TEXT NOT NULL CHECK (purpose IN ('development', 'sealed')),
  status TEXT NOT NULL CHECK (status IN ('READY', 'RUNNING', 'COMPLETED', 'FAILED')),
  params TEXT NOT NULL,
  configs TEXT NOT NULL,
  analysis TEXT,
  phase TEXT,
  progress INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  app_version TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE sealed_results (
  run_id TEXT NOT NULL REFERENCES sealed_runs(id),
  config_key TEXT NOT NULL,
  timeframe TEXT NOT NULL CHECK (timeframe IN ('M1', 'M5', 'M15')),
  window TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('COMPLETED', 'INCOMPLETE', 'FAILED')),
  reason TEXT,
  metrics TEXT,
  trades TEXT,
  dataset TEXT,
  result_hash TEXT,
  PRIMARY KEY (run_id, config_key, timeframe, window)
);
