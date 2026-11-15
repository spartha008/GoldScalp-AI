-- Phase 1.1: record which market-data provider priced each simulated fill (mt5, tiingo, gold-api, ...).
ALTER TABLE paper_positions ADD COLUMN open_source TEXT;
ALTER TABLE paper_positions ADD COLUMN close_source TEXT;
