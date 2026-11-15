# GoldScalp AI research archive: Phases 1-6

Archival only. Every strategy version found in the records, its exact configuration and fingerprint, every evaluation
row, the trade-level simulated records, the verdicts as recorded and one archival classification. Read-only; every
file is listed with its SHA-256 and origin in `ARCHIVE_MANIFEST.json` (hash in `ARCHIVE_MANIFEST.sha256`).

| Path | Contents |
|---|---|
| `INVENTORY.json`, `INVENTORY.csv` | Every version (keyed by fingerprint): aliases in each study, studies, periods, classification |
| `strategies/<family>/<strategy>/<version>/` | `config.json` (verbatim), `fingerprint.json`, `evaluations.json`, `verdicts.json` |
| `strategies/<family>/<strategy>/variants.json` | Phase 5.2 variants and exit experiments of that strategy, with their rows |
| `strategies/<family>/<strategy>/trades/` | Trade-level simulated records per study (gzip JSON lines) |
| `phases/` | Frozen protocols, manifests, reports and study records, by phase |
| `datasets/` | Series, imports (with file hashes) and candle ranges |
| `snapshots/generation-1/` | Byte-identical copy of `archive/generation-1`: database snapshots, code, Phase 4 final state |
| `KNOWN_ISSUES.md`, `MISSING_ARTIFACTS.md` | Bugs, limitations, audit findings, and what does not exist |
| `ARCHIVE_REPORT.md` | The archive report (also `PHASE_1_TO_6_ARCHIVE_REPORT.md` in the project root) |

Historical evaluation periods are spent: they must never be presented as fresh, independent validation data. Forward
data from 14 November 2026 is not part of this archive. Verify with `python -m goldscalp.research_archive --check`.
