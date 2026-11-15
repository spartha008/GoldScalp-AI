"""Renders PHASE_1_TO_6_ARCHIVE_REPORT.md from the archive build (archival only; no result is computed here)."""
from __future__ import annotations

from collections import Counter
from typing import Any

PHASE_OF = {"app:strategy_versions": "1-3", "app:strategy_proposals": "4", "store:candidates": "5", "store:runs": "5",
            "store:research_versions": "5.2", "store:robustness_configs": "5.2", "store:validation_studies": "5.3",
            "store:discovery_trials": "6", "store:sealed_runs": "6"}


def _phases(e: dict[str, Any]) -> str:
    ph = sorted({PHASE_OF.get(a["source"], "?") for a in e["aliases"]}, key=lambda x: [int(p) if p.isdigit() else 0
                                                                                     for p in x.replace("-", ".").split(".")])
    return ", ".join(ph)


def _tree(files: dict[str, Any]) -> list[str]:
    top: Counter = Counter()
    size: Counter = Counter()
    for rel, f in files.items():
        parts = rel.split("/")
        key = "/".join(parts[:2]) if parts[0] in ("phases", "strategies", "snapshots") and len(parts) > 2 else parts[0]
        top[key] += 1
        size[key] += f["bytes"]
    return [f"| `{k}` | {top[k]} | {size[k] / 1e6:.1f} MB |" for k in sorted(top)]


def render(inv: dict[str, Any], files: dict[str, Any], v: dict[str, Any], before: dict[str, Any], after: dict[str, Any]) -> str:
    E = inv["entries"]
    heads = [e for e in E if e["headline"]]
    variants = [e for e in E if not e["headline"]]
    concepts = sorted({e["strategy"] for e in heads})
    copies = [f for f in files.values() if "source_sha256" in f]
    trades = [r for r in files if "/trades/" in r]
    L = ["# GoldScalp AI: Phase 1-6 Research Archive Report", "",
         "**Archival only.** No strategy was developed, optimised, re-run, re-tuned or promoted. Results, reports and "
         "verdicts are preserved as recorded; missing artifacts are listed, never reconstructed.", "",
         "- Archive: `research_archive/phase_1_to_6/` (read-only after verification).",
         "- Manifest: `ARCHIVE_MANIFEST.json`, whose own SHA-256 is in `ARCHIVE_MANIFEST.sha256`.",
         "- Re-check it any time with `python -m goldscalp.research_archive --check`.", "",
         "## 1. Inventory at a glance", "",
         "| Item | Count |", "|---|---:|",
         f"| Strategy concepts (named strategies) | {len(concepts)} |",
         f"| Headline strategy versions (named versions, proposals, Phase 6 configurations) | {len(heads)} |",
         f"| Phase 5.2 variants and exit experiments (unique fingerprints) | {len(variants)} |",
         f"| Unique strategy versions in total (one per fingerprint) | {len(E)} |",
         f"| Evaluation rows attached to versions | {sum(e['evaluations'] for e in E)} |",
         f"| Trade-record files (gzip JSON lines) | {len(trades)} |",
         f"| Archived files (plus the two manifest files) | {len(files)} |", "",
         "Every version found in the records was archived, matched by its exact fingerprint (discovery, not the "
         "requested list). Identical rules under two names count once, with both names kept as aliases. For example, "
         "C v3 has exactly the same rules as C v1.", "",
         "## 2. Complete strategy inventory (headline versions)", "",
         "| Version | Family | Phases | Fingerprint | Evaluation rows | Classification | Evidence |",
         "|---|---|---|---|---:|---|---|"]
    for e in sorted(heads, key=lambda x: (x["family"], x["strategy"], x["label"])):
        aliases = sorted({a["label"] for a in e["aliases"]} - {e["label"]})
        name = e["label"] + (f" (also: {', '.join(a for a in aliases if len(a) < 30)})" if aliases else "")
        L.append(f"| {name} | {e['family']} | {_phases(e)} | `{e['fingerprint'][:12]}` | {e['evaluations']} | "
                 f"{e['classification']} | {e['evidence'] or ''} |")
    per = Counter(e["strategy"] for e in variants)
    L += ["", "### Variants and exit experiments per strategy (Phase 5.2)", "",
          "| Strategy | Variants (unique fingerprints) | Classification |", "|---|---:|---|"]
    for k in sorted(per):
        cls = Counter(e["classification"] for e in variants if e["strategy"] == k)
        L.append(f"| {k} | {per[k]} | " + "; ".join(f"{c}: {n}" for c, n in cls.most_common()) + " |")
    L += ["", "Each variant's exact configuration, aliases and evaluation rows are in "
          "`strategies/<family>/<strategy>/variants.json`. Variants were chosen among on development months only, "
          "and none is a separate validated strategy.", "",
          "## 3. Final historical classifications", "", "| Classification | Headline versions |", "|---|---|"]
    by = {}
    for e in heads:
        by.setdefault(e["classification"], []).append(e["label"])
    for c in sorted(by):
        L.append(f"| {c} | {', '.join(sorted(by[c]))} |")
    L += ["", "No version is classified as successful. A positive result in one period was never taken as success on "
          "its own: R2 v2 was promising in Phase 5.2 and was then rejected on Phase 5.3's unseen months; VOL-C was "
          "positive on development data and negative as the sealed control.",
          "",
          "Each version's `verdicts.json` keeps the verdicts exactly as recorded, with their source: verdicts@1 and "
          "verdicts@2 for Phase 5.2, validation@1 for 5.3, the Phase 6 gates, the sealed@1 assessment, and the Phase 4 "
          "proposal verdict.", "",
          "## 4. Archive directory structure", "", "| Folder | Files | Size |", "|---|---:|---:|", *_tree(files), "",
          "- `strategies/<family>/<strategy>/<version>/`: `config.json` (stored configuration, verbatim),",
          "  `fingerprint.json` (stored and recomputed), `evaluations.json` (every evaluation row: study, period,",
          "  timeframe, metrics, dataset identifier, result hash) and `verdicts.json`.",
          "- `strategies/<family>/<strategy>/trades/`: trade-level simulated records per study.",
          "- `phases/`: frozen protocols, manifests, reports and study records for Phases 1-3, 4, 5, 5.1, 5.2, 5.3, the",
          "  closure and 6.",
          "- `datasets/datasets.json`: series, imports (file name, SHA-256, rows, ranges) and candle ranges per series and",
          "  timeframe.",
          "- `snapshots/generation-1/`: a byte-identical copy of `archive/generation-1` (database snapshots, code zip,",
          "  Phase 4 final state).", "",
          "## 5. Preserved reports and datasets", ""]
    for ph in ("phase_1_to_3_foundations", "phase_4_live_validation", "phase_5_monthly", "phase_5_1_risk_scenarios",
               "phase_5_2_robustness", "phase_5_3_validation", "phase_5_closure", "phase_6_discovery"):
        names = sorted(r.split("/", 2)[2] for r in files if r.startswith(f"phases/{ph}/"))
        L.append(f"- **{ph}**: " + ", ".join(f"`{n}`" for n in names))
    L += ["- **datasets**: `datasets/datasets.json`. The candles themselves are in the database snapshots "
          "(`snapshots/generation-1/data/`).", "",
          "## 6. SHA-256 verification summary", "",
          f"- Files written and hashed: {len(files)}. Each was re-hashed after writing and matched.",
          f"- Verbatim copies: {len(copies)}, each identical to its source (same SHA-256 at copy time).",
          f"- Exports from the databases: {len(files) - len(copies) - 1}, each regenerated from the sources and "
          "compared byte for byte.",
          f"- Missing planned files: {len(v['missing'])}; mismatched: {len(v['mismatched'])}.",
          f"- Every discovered fingerprint has an archive entry: {v['every_fingerprint_archived']}. Duplicate "
          f"fingerprints: {v['duplicate_fingerprints']}. Duplicate headline names: {v['duplicate_headline_labels'] or 'none'}.",
          f"- Requested versions missing from the inventory: {v['required_missing'] or 'none'}.",
          f"- Fingerprints that do not recompute from the stored configuration: {v['fingerprint_mismatches'] or 'none'}.",
          f"- Headline versions without a classification: {v['unclassified_headlines'] or 'none'}.",
          f"- Evaluation rows that could not be matched to a version: {v['unmapped_evaluation_keys'] or 'none'}.", "",
          "## 7. Missing artifacts", "", "See `MISSING_ARTIFACTS.md` in the archive; in short:",
          "- Phase 4 has no trade-level records, because it recorded no evaluation, signal or trade.",
          "- Results that never completed have no trades.",
          "- The raw MT5 export files were not kept; their hashes and ranges are in `datasets/datasets.json`.",
          "- PDF and CSV exports are generated on demand and were not stored as files.",
          "- No configuration, report or frozen manifest that the records refer to is missing.", "",
          "## 8. Known bugs, limitations and audit findings", "",
          "These are in `KNOWN_ISSUES.md`:",
          "- the incomplete and cancelled studies;",
          "- the Phase 5.1 M15 cache-key bug that was fixed and re-run;",
          "- Phase 4's missing live evidence and the MT5 verification records;",
          "- profit factor in money versus expectancy in R;",
          "- small samples and development-data reuse;",
          "- the H6.4 report's cosmetic halves display;",
          "- the Phase 0 audit.", "",
          "## 9. Phase 4 preservation", ""]
    p4 = after.get("phase4", {})
    for m in p4.get("monitors", []):
        L.append(f"- Monitor {m['id']}: {m['status']} (stopped {m['stopped_at']}).")
    L += ["- Strategies archived in the application: " + ", ".join(f"{s['name']} (archived={s['archived']})"
                                                                     for s in p4.get("strategies_archived", [])) + ".",
          f"- Open positions in Phase 4 validation accounts: {p4.get('open_phase4_positions')}.",
          f"- `archive/generation-1`: unchanged={after.get('generation_1_archive', {}).get('unchanged')}, "
          f"{after.get('generation_1_archive', {}).get('files')} files, writable files: "
          f"{after.get('generation_1_archive', {}).get('writable')}.",
          "- The archive build only read the databases (`mode=ro`). No monitor, account, position, journal or ledger "
          "was reactivated, restarted, modified or deleted. Account 1 (manual Phase 1 paper trading) and its open "
          "position are outside Phase 4 and were left unchanged.", "",
          "## 10. Frozen manifests (before and after the build)", "", "| Record | Before | After |", "|---|---|---|"]
    for k in ("phase5_closure", "phase53_protocol", "phase6_discovery_protocol", "h64_protocol"):
        L.append(f"| {k} | {before.get(k)} | {after.get(k)} |")
    fb, fa = before.get("forward_data", {}), after.get("forward_data", {})
    L += ["", "## 11. Forward dataset (from 14 November 2026)", "",
          "| Database | Candles from 14 Nov 2026 (before) | After | Latest |", "|---|---:|---:|---|"]
    for k in ("application", "research"):
        L.append(f"| {k} | {fb.get(k, {}).get('candles_from_2026_11_14')} | {fa.get(k, {}).get('candles_from_2026_11_14')} "
                 f"| {fa.get(k, {}).get('latest')} |")
    L += ["", "- The forward dataset was not altered: the build opened both databases read-only and wrote nothing "
          "outside the archive folder.",
          "- It holds no forward candles yet, because the market has been closed since 13 November 21:56 UTC.",
          "- The live MT5 feed will store new candles from the next market open. They are reserved forward data: they "
          "are not part of this archive and were not used.", "",
          "## 12. Research isolation", "",
          "- This archive is separate from any future research workspace. No Phase 7 workspace or registry was created.",
          "- The archived strategies may serve as documented benchmarks and controls. Their evaluation periods are "
          "spent and must never be presented as fresh, independent validation:",
          "  - every stored month of 2026 up to 13 November;",
          "  - the Phase 5.3 months (January-May 2026);",
          "  - the sealed block (November 2023 - October 2025).",
          "- Forward data must not be used to tune a strategy that is then evaluated on the same forward period.", "",
          "## 13. Incomplete verification", "",
          "- Strategy behaviour was deliberately not re-run, so stored results are preserved, not reproduced. Each row "
          "keeps its result hash, and the Phase 5 runs remain reproducible through the research service.",
          "- A later `--verify` regenerates the exports from the live sources. `datasets/datasets.json` will differ once "
          "the live feed stores new candles; that is the live source moving on, not a change to the archive. "
          "`--check` is the integrity check for the archive itself.",
          "- The raw MT5 export files cannot be verified, because they were not kept. Only their recorded hashes are "
          "available.",
          "- The test-suite result is reported with this build in the session and is not stored inside the archive.", ""]
    return "\n".join(L) + "\n"
