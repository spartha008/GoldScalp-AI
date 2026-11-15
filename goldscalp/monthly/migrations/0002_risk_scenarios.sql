-- GoldScalp AI Phase 5.1 (additive; research store only): hypothetical percentage-risk scenario studies and import
-- labels. Existing evaluations, candidates, runs and candles are not touched.

-- A risk study groups the scenario evaluations (one evaluation per risk percentage, each with its own runs and
-- separate hypothetical accounts) created from one completed fixed-lot BASELINE evaluation. The baseline is
-- referenced, never modified.
CREATE TABLE risk_studies (
  id TEXT PRIMARY KEY,
  baseline_evaluation_id TEXT NOT NULL REFERENCES evaluations(id),
  month TEXT NOT NULL,
  evaluation_ids TEXT NOT NULL,
  params TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('READY', 'RUNNING', 'COMPLETED', 'INCOMPLETE', 'FAILED')),
  phase TEXT,
  progress INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  app_version TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT
);

-- A free-text dataset label and instrument for each CSV import (the import itself is in data_imports).
CREATE TABLE import_labels (
  import_id INTEGER PRIMARY KEY REFERENCES data_imports(id),
  label TEXT NOT NULL,
  instrument TEXT NOT NULL,
  created_at TEXT NOT NULL
);
