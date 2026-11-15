-- goldscalp: foreign_keys=off
-- Phase 4: live strategy evaluation on SIMULATED paper accounts, MT5 terminal verification records, feed
-- health, per-trade evaluations, improvement studies/proposals and the research-assistant log.
-- Runs with foreign keys OFF (SQLite's documented 12-step table rebuild); the runner requires an empty
-- PRAGMA foreign_key_check before it commits.

-- 1) paper_positions rebuilt: close_reason gains time_exit and strategy_exit (live strategy validation).
CREATE TABLE paper_positions_new (
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
  close_reason TEXT CHECK (close_reason IS NULL OR close_reason IN ('manual', 'stop_loss', 'take_profit',
                                                                    'time_exit', 'strategy_exit')),
  realized_gross TEXT,
  realized_net TEXT,
  open_source TEXT,
  close_source TEXT,
  signal_id TEXT REFERENCES signals(id)
);
INSERT INTO paper_positions_new (id, account_id, side, lots, ounces, open_price, open_time, open_quote_time,
  open_price_state, open_spread, open_spread_source, open_slippage, open_commission, margin, stop_loss, take_profit,
  status, close_price, close_time, close_quote_time, close_price_state, close_commission, close_reason,
  realized_gross, realized_net, open_source, close_source, signal_id)
SELECT id, account_id, side, lots, ounces, open_price, open_time, open_quote_time,
  open_price_state, open_spread, open_spread_source, open_slippage, open_commission, margin, stop_loss, take_profit,
  status, close_price, close_time, close_quote_time, close_price_state, close_commission, close_reason,
  realized_gross, realized_net, open_source, close_source, signal_id FROM paper_positions;
DROP TABLE paper_positions;
ALTER TABLE paper_positions_new RENAME TO paper_positions;
CREATE INDEX paper_positions_account_status ON paper_positions(account_id, status);

-- 2) paper accounts: the user's manual account(s) vs one validation account per monitored strategy version.
ALTER TABLE paper_accounts ADD COLUMN purpose TEXT NOT NULL DEFAULT 'manual' CHECK (purpose IN ('manual', 'validation'));

-- 3) actual-terminal verification records (never written by tests: they use a temporary database).
CREATE TABLE mt5_verifications (
  id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  overall TEXT NOT NULL CHECK (overall IN ('VERIFIED', 'PARTIAL', 'FAILED')),
  adapter TEXT NOT NULL,
  broker TEXT,
  is_vantage INTEGER,
  environment TEXT,
  symbol TEXT,
  checks TEXT NOT NULL,
  summary TEXT NOT NULL
);
CREATE INDEX mt5_verifications_started ON mt5_verifications(started_at);

-- 4) feed health: one sample per minute and every market-state transition.
CREATE TABLE feed_samples (
  ts TEXT NOT NULL,
  provider TEXT NOT NULL,
  state TEXT NOT NULL,
  bid TEXT,
  ask TEXT,
  spread TEXT,
  quote_time TEXT,
  tick_age_seconds REAL,
  PRIMARY KEY (ts, provider)
);
CREATE TABLE feed_events (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  provider TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  reason TEXT
);
CREATE INDEX feed_events_ts ON feed_events(ts);

-- 5) live strategy monitor: monitored strategy VERSIONS, every evaluated cut-off, signals, simulated trades.
CREATE TABLE monitored_strategies (
  id INTEGER PRIMARY KEY,
  strategy_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  fingerprint TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('observe', 'paper')),
  status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'stopped')),
  account_id INTEGER REFERENCES paper_accounts(id),
  eligibility TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  stopped_at TEXT,
  note TEXT,
  FOREIGN KEY (strategy_id, version) REFERENCES strategy_versions(strategy_id, version)
);
CREATE TABLE monitor_evaluations (
  monitored_id INTEGER NOT NULL REFERENCES monitored_strategies(id),
  cutoff TEXT NOT NULL,
  evaluated_at TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('no_setup', 'setup', 'repeat', 'ambiguous', 'gated', 'skipped', 'missed')),
  reason TEXT,
  signal_id TEXT,
  PRIMARY KEY (monitored_id, cutoff)
);
CREATE TABLE strategy_signals (
  id TEXT PRIMARY KEY,
  monitored_id INTEGER NOT NULL REFERENCES monitored_strategies(id),
  strategy_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  cutoff TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('long', 'short')),
  status TEXT NOT NULL CHECK (status IN ('observed', 'opened', 'blocked')),
  status_reason TEXT,
  session TEXT NOT NULL,
  payload TEXT NOT NULL,
  outcome TEXT,
  outcome_at TEXT,
  notes TEXT
);
CREATE INDEX strategy_signals_cutoff ON strategy_signals(cutoff);
CREATE INDEX strategy_signals_strategy ON strategy_signals(strategy_id, version, cutoff);
CREATE TABLE live_trades (
  id INTEGER PRIMARY KEY,
  signal_id TEXT NOT NULL UNIQUE REFERENCES strategy_signals(id),
  monitored_id INTEGER NOT NULL REFERENCES monitored_strategies(id),
  strategy_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  account_id INTEGER NOT NULL REFERENCES paper_accounts(id),
  position_id INTEGER NOT NULL UNIQUE REFERENCES paper_positions(id),
  opened_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
  plan TEXT NOT NULL,
  entry TEXT NOT NULL,
  context_entry TEXT NOT NULL,
  mfe TEXT NOT NULL DEFAULT '0',
  mae TEXT NOT NULL DEFAULT '0',
  mfe_at TEXT,
  mae_at TEXT,
  checks TEXT NOT NULL DEFAULT '[]',
  exit TEXT,
  context_exit TEXT,
  closed_at TEXT
);
CREATE INDEX live_trades_status ON live_trades(status);

-- 6) per-trade evaluations: inserted once, never updated (the repository has no update or delete).
CREATE TABLE trade_evaluations (
  id INTEGER PRIMARY KEY,
  trade_id INTEGER NOT NULL REFERENCES live_trades(id),
  definition TEXT NOT NULL,
  created_at TEXT NOT NULL,
  summary TEXT NOT NULL,
  answers TEXT NOT NULL,
  tags TEXT NOT NULL,
  went_well TEXT NOT NULL,
  went_wrong TEXT NOT NULL,
  UNIQUE (trade_id, definition)
);

-- 7) improvement studies and proposals (adopted only by an explicit user approval).
CREATE TABLE research_studies (
  id TEXT PRIMARY KEY,
  strategy_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'complete', 'failed', 'cancelled')),
  params TEXT NOT NULL,
  progress INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  finished_at TEXT,
  result TEXT,
  error TEXT,
  FOREIGN KEY (strategy_id, version) REFERENCES strategy_versions(strategy_id, version)
);
CREATE TABLE strategy_proposals (
  id TEXT PRIMARY KEY,
  study_id TEXT NOT NULL REFERENCES research_studies(id),
  strategy_id INTEGER NOT NULL,
  base_version INTEGER NOT NULL,
  base_fingerprint TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  candidate TEXT NOT NULL,
  candidate_fingerprint TEXT NOT NULL,
  diff TEXT NOT NULL,
  explanation TEXT NOT NULL,
  evidence TEXT NOT NULL,
  limitations TEXT NOT NULL,
  validation TEXT,
  verdict TEXT CHECK (verdict IS NULL OR verdict IN ('improved', 'not_improved', 'insufficient_sample')),
  status TEXT NOT NULL CHECK (status IN ('validated', 'inconclusive', 'not_improved', 'approved', 'rejected')),
  created_at TEXT NOT NULL,
  decided_at TEXT,
  decision_note TEXT,
  resulting_version INTEGER
);
CREATE INDEX strategy_proposals_strategy ON strategy_proposals(strategy_id, created_at);

-- 8) research-assistant audit log: every question, the tools it used and the records it cited.
CREATE TABLE assistant_log (
  id INTEGER PRIMARY KEY,
  asked_at TEXT NOT NULL,
  question TEXT NOT NULL,
  mode TEXT NOT NULL,
  intent TEXT,
  answer TEXT NOT NULL,
  tools TEXT NOT NULL,
  sources TEXT NOT NULL
);
