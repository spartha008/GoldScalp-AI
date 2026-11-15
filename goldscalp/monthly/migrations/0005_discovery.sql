-- GoldScalp AI Phase 6 (additive; research store only): new strategy discovery. Every development trial and result is
-- kept (failed ones included) so the number of attempts per hypothesis is always visible. Earlier tables untouched.

CREATE TABLE discovery_trials (
  id TEXT PRIMARY KEY,
  hypothesis TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('baseline', 'refinement')),
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
CREATE INDEX discovery_trials_hypothesis ON discovery_trials (hypothesis, created_at);

-- One row per configuration x timeframe x development month: a separate hypothetical $1,000 account.
CREATE TABLE discovery_results (
  trial_id TEXT NOT NULL REFERENCES discovery_trials(id),
  config_key TEXT NOT NULL,
  timeframe TEXT NOT NULL CHECK (timeframe IN ('M1', 'M5', 'M15')),
  window TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('COMPLETED', 'INCOMPLETE', 'FAILED')),
  reason TEXT,
  metrics TEXT,
  trades TEXT,
  dataset TEXT,
  result_hash TEXT,
  PRIMARY KEY (trial_id, config_key, timeframe, window)
);
