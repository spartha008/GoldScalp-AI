-- GoldScalp AI Phase 5.3 (additive; research store only): frozen pre-June historical validation of locked candidates.
-- Phase 5.2 tables, studies and results are not touched.

CREATE TABLE validation_studies (
  id TEXT PRIMARY KEY,
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

-- One row per locked configuration x timeframe x month: a separate hypothetical $1,000 account.
CREATE TABLE validation_results (
  study_id TEXT NOT NULL REFERENCES validation_studies(id),
  config_key TEXT NOT NULL,
  timeframe TEXT NOT NULL CHECK (timeframe IN ('M1', 'M5', 'M15')),
  window TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('COMPLETED', 'INCOMPLETE', 'FAILED')),
  reason TEXT,
  metrics TEXT,
  trades TEXT,
  dataset TEXT,
  result_hash TEXT,
  PRIMARY KEY (study_id, config_key, timeframe, window)
);
