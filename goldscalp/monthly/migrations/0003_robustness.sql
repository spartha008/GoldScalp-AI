-- GoldScalp AI Phase 5.2 (additive; research store only): multi-month strategy redesign and robustness research.
-- Existing evaluations, studies, runs and candles are not touched.

CREATE TABLE robustness_studies (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('READY', 'RUNNING', 'COMPLETED', 'FAILED')),
  params TEXT NOT NULL,
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

-- Every pre-declared configuration (benchmarks, exit experiments, redesign variants) with its exact rules.
CREATE TABLE robustness_configs (
  study_id TEXT NOT NULL REFERENCES robustness_studies(id),
  key TEXT NOT NULL,
  config TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  PRIMARY KEY (study_id, key)
);

-- One row per configuration x run timeframe x window: a separate hypothetical $1,000 account.
CREATE TABLE robustness_results (
  study_id TEXT NOT NULL REFERENCES robustness_studies(id),
  config_key TEXT NOT NULL,
  timeframe TEXT NOT NULL CHECK (timeframe IN ('M1', 'M5', 'M15')),
  window TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('COMPLETED', 'INCOMPLETE', 'FAILED')),
  reason TEXT,
  metrics TEXT,
  trades TEXT,
  dataset TEXT,
  result_hash TEXT,
  diagnostics TEXT,
  PRIMARY KEY (study_id, config_key, timeframe, window)
);

-- Development-chosen redesigned versions (Example A v3, B v3, C v5, R1 v2 ... R9 v2). RESEARCH ONLY: never written to
-- the application's strategies; adopting one is a separate, explicit user decision.
CREATE TABLE research_versions (
  id TEXT PRIMARY KEY,
  study_id TEXT NOT NULL REFERENCES robustness_studies(id),
  strategy TEXT NOT NULL,
  name TEXT NOT NULL,
  config TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  chosen_variant TEXT NOT NULL,
  verdict TEXT NOT NULL,
  evidence TEXT NOT NULL,
  created_at TEXT NOT NULL
);
