-- Phase 2: rule-based signal history (live and replay) and the link from a simulated position to the
-- signal that prefilled its ticket. Payload = the full signal (evidence, invalidation, context, definitions).
CREATE TABLE signals (
  id TEXT PRIMARY KEY,
  mode TEXT NOT NULL CHECK (mode IN ('live', 'replay')),
  replay_id TEXT,
  series_key TEXT NOT NULL,
  as_of TEXT NOT NULL,
  cutoff TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('BULLISH', 'BEARISH', 'NEUTRAL', 'NO SIGNAL')),
  score INTEGER NOT NULL,
  definition TEXT NOT NULL,
  config_fingerprint TEXT NOT NULL,
  payload TEXT NOT NULL,
  evaluation TEXT
);
CREATE INDEX signals_series_cutoff ON signals(mode, series_key, cutoff);
CREATE INDEX signals_replay ON signals(replay_id, cutoff);
ALTER TABLE paper_positions ADD COLUMN signal_id TEXT REFERENCES signals(id);
