# Missing artifacts

Listed explicitly; nothing was reconstructed or invented.

- Expected source files that do not exist: none.
- Versions without a stored configuration: none.
- Fingerprints that do not recompute from the stored configuration: none.
- Headline versions with no evaluation rows in the research store: A v1, B v1, C v1, C v2, C v4 proposal sp_b29a787a2e52f3b4. Their evidence, where any, is in the Phase 3 Lab runs (`phases/phase_1_to_3_foundations/lab_runs.json`) or the Phase 4 records.
- Results with status INCOMPLETE or FAILED have no trade records (they never ran to completion); their rows and reasons are kept in each version's `evaluations.json`.
- Phase 4: no monitor evaluations, signals, live trades, journal entries or demo-execution trades exist (the tables are empty), so there are no Phase 4 trade-level records.
- Phase 3 Lab runs store their results inside `lab_runs.json`; no separate trade files were kept by the Lab.
- Raw MT5 export files used for the imports were not kept by the application; their names, SHA-256 hashes, row counts and ranges are in `datasets/datasets.json`, and the imported candles are in the database snapshots.
- Forward data: none exists in the archive. No candle on or after 14 November 2026 was stored when the snapshots were taken (see `datasets/datasets.json`, column `forward`).
