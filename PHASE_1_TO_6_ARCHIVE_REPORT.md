# GoldScalp AI: Phase 1-6 Research Archive Report

**Archival only.** No strategy was developed, optimised, re-run, re-tuned or promoted. Results, reports and verdicts are preserved as recorded; missing artifacts are listed, never reconstructed.

- Archive: `research_archive/phase_1_to_6/` (read-only after verification).
- Manifest: `ARCHIVE_MANIFEST.json`, whose own SHA-256 is in `ARCHIVE_MANIFEST.sha256`.
- Re-check it any time with `python -m goldscalp.research_archive --check`.

## 1. Inventory at a glance

| Item | Count |
|---|---:|
| Strategy concepts (named strategies) | 18 |
| Headline strategy versions (named versions, proposals, Phase 6 configurations) | 35 |
| Phase 5.2 variants and exit experiments (unique fingerprints) | 848 |
| Unique strategy versions in total (one per fingerprint) | 883 |
| Evaluation rows attached to versions | 6197 |
| Trade-record files (gzip JSON lines) | 103 |
| Archived files (plus the two manifest files) | 357 |

Every version found in the records was archived, matched by its exact fingerprint (discovery, not the requested list). Identical rules under two names count once, with both names kept as aliases. For example, C v3 has exactly the same rules as C v1.

## 2. Complete strategy inventory (headline versions)

| Version | Family | Phases | Fingerprint | Evaluation rows | Classification | Evidence |
|---|---|---|---|---:|---|---|
| B v1 | breakout | 1-3 | `1868cade122e` | 0 | superseded | Phase 3 template version, replaced by B v2 before any formal verdict. |
| B v2 (also: B, B|original) | breakout | 1-3, 5, 5.2 | `5d8d81c69794` | 49 | inconclusive: insufficient evidence | Phase 5.2 out of sample +0.029 R/trade over only 24 trades. |
| B v3 (also: B|v-d-Xe-be) | breakout | 5.2 | `b626b5e20a42` | 18 | inconclusive: insufficient evidence | 5.2 +0.025 over 35 trades. |
| R2 original (also: R2, R2|original) | breakout | 5, 5.2, 5.3 | `eb622d47fb2f` | 50 | rejected | 5.2 +0.022 (195, inconclusive); 5.3 -0.190 (413, rejected). |
| R2 v2 (also: R2|v-d-Xb) | breakout | 5.2, 5.3 | `655888c68fa6` | 28 | promising at an earlier stage, subsequently rejected | 5.2 +0.054 (147, promising); 5.3 -0.054 (273, rejected). |
| R4 original (also: R4, R4|original) | breakout | 5, 5.2 | `0a77bd5bbc6e` | 40 | rejected | 5.2 -0.087 (40). |
| R4 v2 (also: R4|v-d-Xe-be) | breakout | 5.2 | `68310a999b95` | 18 | inconclusive: insufficient evidence | 5.2 +0.121 over only 37 trades. |
| R7 original (also: R7, R7|original) | breakout | 5, 5.2, 5.3 | `40d023f434fe` | 47 | rejected | 5.2 +0.689 over only 9 trades (inconclusive); 5.3 -0.353 (16, rejected). |
| R7 v2 (also: R7|v-c-Xe-tr) | breakout | 5.2, 5.3 | `566c147b5370` | 28 | rejected | 5.2 +0.400 over 21 trades (inconclusive); 5.3 -0.107 (37, rejected). |
| R9 original (also: R9, R9|original) | breakout | 5, 5.2 | `488e73e9e13f` | 37 | rejected | 5.2 -0.150 (51). |
| R9 v2 (also: R9|v-a-Xe-tr) | breakout | 5.2 | `fb5ff4c12152` | 18 | rejected | 5.2 -0.212 (51). |
| SES-B | breakout | 6 | `0de7025d5ca9` | 11 | inconclusive: insufficient evidence (development) | Development +0.110 (127); 22 London-open trades. |
| C v1 (also: C v3) | reversal | 1-3 | `2d578c1230ec` | 0 | superseded | Phase 3 template version; no formal verdict. |
| C v2 | reversal | 1-3 | `6c2073a85d74` | 0 | superseded | Phase 3 iteration; no formal verdict. |
| C v4 (also: C, C|original) | reversal | 1-3, 5, 5.2 | `14ceea2a1c9e` | 49 | rejected | Phase 5.2 out of sample -0.005 R/trade over 249 trades. |
| C v4 proposal sp_b29a787a2e52f3b4 | reversal | 4 | `4ed891a53798` | 0 | inconclusive: insufficient sample (proposal not adopted) | Phase 4 improvement proposal; verdict insufficient_sample, status inconclusive. |
| C v5 (also: C|v-c-Xc-tr) | reversal | 5.2 | `eecc13b60703` | 18 | rejected | 5.2 -0.328 (9). |
| R3 original (also: R3, R3|original) | reversal | 5, 5.2 | `1b09c92a964d` | 40 | rejected | 5.2 -0.045 (34). |
| R3 v2 (also: R3|v-b-Xg-be) | reversal | 5.2 | `f8b183f60789` | 18 | rejected | 5.2 -0.163 (28). |
| R8 original (also: R8, R8|original) | reversal | 5, 5.2 | `7755b93084ce` | 37 | rejected | 5.2 -0.171 (3). |
| R8 v2 (also: R8|v-c-Xb) | reversal | 5.2 | `a723c401d093` | 18 | rejected | 5.2 -0.062 (25). |
| VOL-F | reversal | 6 | `83ea92fb8627` | 11 | development-only observation: not supported | Development -0.012 (129); H6.1 not supported. |
| A v1 | trend | 1-3 | `931f63653a79` | 0 | superseded | Phase 3 template version, replaced by A v2 before any formal verdict. |
| A v2 (also: A, A|original) | trend | 1-3, 5, 5.2 | `c7bc3da5f422` | 49 | rejected | Phase 5.2 out of sample -0.005 R/trade over 157 trades. |
| A v3 (also: A|v-d-Xc) | trend | 5.2 | `947c7238ecce` | 18 | rejected | 5.2 -0.018 (148). |
| H6.4 | trend | 6 | `0019c2a629e8` | 59 | sealed-validation result: rejected | Development +0.113 (269); sealed 2023-2025 -0.006 (663). |
| R1 original (also: R1, R1|original) | trend | 5, 5.2, 5.3 | `b549a86b00d9` | 50 | break-even / insufficient evidence | 5.2 +0.042 (358, inconclusive); 5.3 +0.003 (726, inconclusive). |
| R1 v2 (also: R1|v-b-Xd) | trend | 5.2, 5.3 | `4a48dc6838b2` | 28 | break-even / insufficient evidence | 5.2 +0.026 (258, inconclusive); 5.3 +0.006 (368, inconclusive); retired. |
| R5 original (also: R5, R5|original) | trend | 5, 5.2, 5.3 | `952765a67b99` | 50 | rejected | 5.2 -0.017 (167); 5.3 -0.098 (311). |
| R5 v2 (also: R5|v-d-Xd-tr) | trend | 5.2, 5.3 | `a07170303651` | 28 | rejected | 5.2 +0.034 (123, inconclusive); 5.3 -0.053 (222, rejected). |
| R6 original (also: R6, R6|original) | trend | 5, 5.2 | `08b1f3a254d1` | 40 | rejected | 5.2 -0.170 (62). |
| R6 v2 (also: R6|v-d-Xd-be) | trend | 5.2 | `a647d10b764d` | 18 | rejected | 5.2 -0.139 (45). |
| STR-0 | trend | 6 | `17c136ab9a6b` | 11 | development-only observation (control) | Development +0.101 (241); the H6.2 control. |
| STR-X | trend | 6 | `33a8b2547fad` | 11 | rejected (development) | Development +0.062 (111), worse than its control; H6.2 rejected. |
| VOL-C | trend | 6 | `64d2a3b37538` | 70 | development-only observation; negative as the sealed control | Development +0.102 (294); sealed 2023-2025 -0.029 (762) as the H6.4 control (comparison only). |

### Variants and exit experiments per strategy (Phase 5.2)

| Strategy | Variants (unique fingerprints) | Classification |
|---|---:|---|
| A | 74 | development variant (not selected): 47; variant from a superseded or cancelled study: 27 |
| B | 106 | development variant (not selected): 59; variant from a superseded or cancelled study: 47 |
| C | 86 | variant from a superseded or cancelled study: 43; development variant (not selected): 43 |
| R1 | 68 | development variant (not selected): 47; variant from a superseded or cancelled study: 21 |
| R2 | 68 | development variant (not selected): 47; variant from a superseded or cancelled study: 21 |
| R3 | 64 | development variant (not selected): 43; variant from a superseded or cancelled study: 21 |
| R4 | 70 | development variant (not selected): 47; variant from a superseded or cancelled study: 23 |
| R5 | 67 | development variant (not selected): 47; variant from a superseded or cancelled study: 20 |
| R6 | 68 | development variant (not selected): 47; variant from a superseded or cancelled study: 21 |
| R7 | 56 | development variant (not selected): 35; variant from a superseded or cancelled study: 21 |
| R8 | 53 | development variant (not selected): 32; variant from a superseded or cancelled study: 21 |
| R9 | 68 | development variant (not selected): 47; variant from a superseded or cancelled study: 21 |

Each variant's exact configuration, aliases and evaluation rows are in `strategies/<family>/<strategy>/variants.json`. Variants were chosen among on development months only, and none is a separate validated strategy.

## 3. Final historical classifications

| Classification | Headline versions |
|---|---|
| break-even / insufficient evidence | R1 original, R1 v2 |
| development-only observation (control) | STR-0 |
| development-only observation: not supported | VOL-F |
| development-only observation; negative as the sealed control | VOL-C |
| inconclusive: insufficient evidence | B v2, B v3, R4 v2 |
| inconclusive: insufficient evidence (development) | SES-B |
| inconclusive: insufficient sample (proposal not adopted) | C v4 proposal sp_b29a787a2e52f3b4 |
| promising at an earlier stage, subsequently rejected | R2 v2 |
| rejected | A v2, A v3, C v4, C v5, R2 original, R3 original, R3 v2, R4 original, R5 original, R5 v2, R6 original, R6 v2, R7 original, R7 v2, R8 original, R8 v2, R9 original, R9 v2 |
| rejected (development) | STR-X |
| sealed-validation result: rejected | H6.4 |
| superseded | A v1, B v1, C v1, C v2 |

No version is classified as successful. A positive result in one period was never taken as success on its own: R2 v2 was promising in Phase 5.2 and was then rejected on Phase 5.3's unseen months; VOL-C was positive on development data and negative as the sealed control.

Each version's `verdicts.json` keeps the verdicts exactly as recorded, with their source: verdicts@1 and verdicts@2 for Phase 5.2, validation@1 for 5.3, the Phase 6 gates, the sealed@1 assessment, and the Phase 4 proposal verdict.

## 4. Archive directory structure

| Folder | Files | Size |
|---|---:|---:|
| `INVENTORY.csv` | 1 | 0.2 MB |
| `INVENTORY.json` | 1 | 0.9 MB |
| `KNOWN_ISSUES.md` | 1 | 0.0 MB |
| `MISSING_ARTIFACTS.md` | 1 | 0.0 MB |
| `README.md` | 1 | 0.0 MB |
| `datasets` | 1 | 0.0 MB |
| `phases/phase_1_to_3_foundations` | 8 | 2.8 MB |
| `phases/phase_4_live_validation` | 7 | 0.2 MB |
| `phases/phase_5_1_risk_scenarios` | 1 | 0.1 MB |
| `phases/phase_5_2_robustness` | 9 | 2.4 MB |
| `phases/phase_5_3_validation` | 6 | 0.2 MB |
| `phases/phase_5_closure` | 1 | 0.0 MB |
| `phases/phase_5_monthly` | 4 | 0.3 MB |
| `phases/phase_6_discovery` | 12 | 0.5 MB |
| `snapshots/generation-1` | 48 | 271.6 MB |
| `strategies/breakout` | 92 | 7.0 MB |
| `strategies/reversal` | 67 | 3.9 MB |
| `strategies/trend` | 96 | 9.0 MB |

- `strategies/<family>/<strategy>/<version>/`: `config.json` (stored configuration, verbatim),
  `fingerprint.json` (stored and recomputed), `evaluations.json` (every evaluation row: study, period,
  timeframe, metrics, dataset identifier, result hash) and `verdicts.json`.
- `strategies/<family>/<strategy>/trades/`: trade-level simulated records per study.
- `phases/`: frozen protocols, manifests, reports and study records for Phases 1-3, 4, 5, 5.1, 5.2, 5.3, the
  closure and 6.
- `datasets/datasets.json`: series, imports (file name, SHA-256, rows, ranges) and candle ranges per series and
  timeframe.
- `snapshots/generation-1/`: a byte-identical copy of `archive/generation-1` (database snapshots, code zip,
  Phase 4 final state).

## 5. Preserved reports and datasets

- **phase_1_to_3_foundations**: `ARCHITECTURE_AUDIT.md`, `PHASE2_ARCHITECTURE.md`, `PHASE3_ARCHITECTURE.md`, `PHASE_2_IMPLEMENTATION_REPORT.md`, `PHASE_3_IMPLEMENTATION_REPORT.md`, `PROPOSED_ARCHITECTURE.md`, `REUSE_VS_REBUILD.md`, `lab_runs.json`
- **phase_4_live_validation**: `PHASE4_ARCHITECTURE.md`, `PHASE_4_IMPLEMENTATION_REPORT.md`, `final_state/phase4-final-state-post-stop.json`, `final_state/phase4-final-state-post-stop.md`, `final_state/phase4-final-state-pre-stop.json`, `final_state/phase4-final-state-pre-stop.md`, `improvement_studies.json`
- **phase_5_monthly**: `PHASE5_ARCHITECTURE.md`, `PHASE_5_IMPLEMENTATION_REPORT.md`, `candidates.json`, `evaluations.json`
- **phase_5_1_risk_scenarios**: `risk_studies.json`
- **phase_5_2_robustness**: `PHASE_5_2_RESEARCH_REPORT.md`, `rb_7ca33b7b5a81-configurations.csv`, `rb_7ca33b7b5a81-final-verdicts.json`, `rb_7ca33b7b5a81-final-verdicts.md`, `rb_7ca33b7b5a81-variants.csv`, `research_versions.json`, `robustness_studies.json`, `verdicts-framework-frozen-v2.json`, `verdicts-framework-frozen.json`
- **phase_5_3_validation**: `PHASE_5_3_VALIDATION_REPORT.md`, `validation-protocol-frozen.json`, `validation_studies.json`, `vl_747cce289048-validation.csv`, `vl_747cce289048-validation.json`, `vl_747cce289048-validation.md`
- **phase_5_closure**: `phase5-research-cycle-closed.json`
- **phase_6_discovery**: `PHASE_6_H64_REGISTRATION.md`, `PHASE_6_PROTOCOL.md`, `config/phase6-h64.json`, `config/phase6.json`, `discovery-protocol-frozen.json`, `discovery_trials.json`, `h64-protocol-frozen.json`, `h64-report.json`, `h64-report.md`, `phase6-development.json`, `phase6-development.md`, `sealed_runs.json`
- **datasets**: `datasets/datasets.json`. The candles themselves are in the database snapshots (`snapshots/generation-1/data/`).

## 6. SHA-256 verification summary

- Files written and hashed: 357. Each was re-hashed after writing and matched.
- Verbatim copies: 86, each identical to its source (same SHA-256 at copy time).
- Exports from the databases: 270, each regenerated from the sources and compared byte for byte.
- Missing planned files: 0; mismatched: 0.
- Every discovered fingerprint has an archive entry: True. Duplicate fingerprints: 0. Duplicate headline names: none.
- Requested versions missing from the inventory: none.
- Fingerprints that do not recompute from the stored configuration: none.
- Headline versions without a classification: none.
- Evaluation rows that could not be matched to a version: none.

## 7. Missing artifacts

See `MISSING_ARTIFACTS.md` in the archive; in short:
- Phase 4 has no trade-level records, because it recorded no evaluation, signal or trade.
- Results that never completed have no trades.
- The raw MT5 export files were not kept; their hashes and ranges are in `datasets/datasets.json`.
- PDF and CSV exports are generated on demand and were not stored as files.
- No configuration, report or frozen manifest that the records refer to is missing.

## 8. Known bugs, limitations and audit findings

These are in `KNOWN_ISSUES.md`:
- the incomplete and cancelled studies;
- the Phase 5.1 M15 cache-key bug that was fixed and re-run;
- Phase 4's missing live evidence and the MT5 verification records;
- profit factor in money versus expectancy in R;
- small samples and development-data reuse;
- the H6.4 report's cosmetic halves display;
- the Phase 0 audit.

## 9. Phase 4 preservation

- Monitor 1: stopped (stopped 2026-11-15T18:21:04.646639+00:00).
- Monitor 2: stopped (stopped 2026-11-15T18:21:04.720676+00:00).
- Monitor 3: stopped (stopped 2026-11-15T18:21:04.799355+00:00).
- Strategies archived in the application: Example A: aligned structure + M5 BOS (archived=1), Example B: sweep + CHoCH + FVG in session (archived=1), Example C: alignment + confirmation + volatility (archived=1).
- Open positions in Phase 4 validation accounts: 0.
- `archive/generation-1`: unchanged=True, 46 files, writable files: [].
- The archive build only read the databases (`mode=ro`). No monitor, account, position, journal or ledger was reactivated, restarted, modified or deleted. Account 1 (manual Phase 1 paper trading) and its open position are outside Phase 4 and were left unchanged.

## 10. Frozen manifests (before and after the build)

| Record | Before | After |
|---|---|---|
| phase5_closure | {'closed': True, 'unchanged': True} | {'closed': True, 'unchanged': True} |
| phase53_protocol | {'frozen': True, 'unchanged': True} | {'frozen': True, 'unchanged': True} |
| phase6_discovery_protocol | {'frozen': True, 'unchanged': True} | {'frozen': True, 'unchanged': True} |
| h64_protocol | {'frozen': True, 'unchanged': True} | {'frozen': True, 'unchanged': True} |

## 11. Forward dataset (from 14 November 2026)

| Database | Candles from 14 Nov 2026 (before) | After | Latest |
|---|---:|---:|---|
| application | 0 | 0 | None |
| research | 0 | 0 | None |

- The forward dataset was not altered: the build opened both databases read-only and wrote nothing outside the archive folder.
- It holds no forward candles yet, because the market has been closed since 13 November 21:56 UTC.
- The live MT5 feed will store new candles from the next market open. They are reserved forward data: they are not part of this archive and were not used.

## 12. Research isolation

- This archive is separate from any future research workspace. No Phase 7 workspace or registry was created.
- The archived strategies may serve as documented benchmarks and controls. Their evaluation periods are spent and must never be presented as fresh, independent validation:
  - every stored month of 2026 up to 13 November;
  - the Phase 5.3 months (January-May 2026);
  - the sealed block (November 2023 - October 2025).
- Forward data must not be used to tune a strategy that is then evaluated on the same forward period.

## 13. Incomplete verification

- Strategy behaviour was deliberately not re-run, so stored results are preserved, not reproduced. Each row keeps its result hash, and the Phase 5 runs remain reproducible through the research service.
- A later `--verify` regenerates the exports from the live sources. `datasets/datasets.json` will differ once the live feed stores new candles; that is the live source moving on, not a change to the archive. `--check` is the integrity check for the archive itself.
- The raw MT5 export files cannot be verified, because they were not kept. Only their recorded hashes are available.
- The test-suite result is reported with this build in the session and is not stored inside the archive.

