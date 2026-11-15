-- Phase 3: versioned strategy configurations, research runs (backtests, comparisons, walk-forward) and the
-- strategy journal that links a strategy setup to a SIMULATED paper position. Outcomes of journal entries are
-- read from paper_positions (no duplication); paper tables themselves are unchanged.
CREATE TABLE strategies (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  current_version INTEGER NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE strategy_versions (
  strategy_id INTEGER NOT NULL REFERENCES strategies(id),
  version INTEGER NOT NULL,
  config TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT,
  PRIMARY KEY (strategy_id, version)
);
CREATE TABLE research_runs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('backtest', 'comparison', 'walk_forward')),
  name TEXT,
  saved INTEGER NOT NULL DEFAULT 0,
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
CREATE INDEX research_runs_created ON research_runs(created_at);
CREATE TABLE strategy_journal (
  id INTEGER PRIMARY KEY,
  strategy_id INTEGER NOT NULL REFERENCES strategies(id),
  strategy_version INTEGER NOT NULL,
  setup_id TEXT NOT NULL,
  signal_id TEXT REFERENCES signals(id),
  signal_time TEXT NOT NULL,
  position_id INTEGER NOT NULL UNIQUE REFERENCES paper_positions(id),
  opened_at TEXT NOT NULL,
  planned TEXT NOT NULL,
  entry_costs TEXT NOT NULL,
  conditions TEXT NOT NULL,
  notes TEXT
);
CREATE INDEX strategy_journal_strategy ON strategy_journal(strategy_id, opened_at);
