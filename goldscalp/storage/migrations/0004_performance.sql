-- Phase 2, Part 11/14: historical signal evaluation runs, and every analysis configuration that a stored
-- signal or run refers to (so a config fingerprint always resolves to its exact parameters).
-- Run signals are stored in the existing `signals` table (mode 'replay', replay_id '<run id>:<timeframe>').
CREATE TABLE analysis_configs (
  fingerprint TEXT PRIMARY KEY,
  data TEXT NOT NULL,
  first_seen TEXT NOT NULL
);
CREATE TABLE performance_runs (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  series_key TEXT NOT NULL,
  params TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'complete', 'failed', 'cancelled')),
  progress INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  config_fingerprint TEXT NOT NULL REFERENCES analysis_configs(fingerprint),
  started_at TEXT,
  finished_at TEXT,
  result TEXT,
  error TEXT
);
CREATE INDEX performance_runs_created ON performance_runs(created_at);
