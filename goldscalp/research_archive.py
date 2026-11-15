"""Phase 1-6 strategy research archive: research_archive/phase_1_to_6 (archival only).

Every strategy version found in the records is archived under its strategy family, keyed by its exact fingerprint,
with its stored configuration (verbatim), every evaluation row, its trade-level simulated records, the verdicts recorded
for it (verbatim, with their source) and one archival classification. Phase folders hold the frozen protocols,
manifests, reports and study records. ``snapshots/generation-1`` is a byte-identical copy of the earlier
``archive/generation-1`` (database snapshots, code, Phase 4 final state).

Read-only by construction: the databases are opened with ``mode=ro`` and nothing is written outside the archive folder.
Nothing is re-run, re-scored or re-classified; missing artifacts are listed, never reconstructed.

  python -m goldscalp.research_archive --build    # one-shot: plan, write, verify, then mark read-only
  python -m goldscalp.research_archive --verify   # re-plan from the sources and compare every file
  python -m goldscalp.research_archive --check    # re-hash the archive against its manifest
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from goldscalp.core.config import PROJECT_ROOT
from goldscalp.strategies import builder

ROOT = PROJECT_ROOT
OUT = ROOT / "research_archive" / "phase_1_to_6"
GEN1 = ROOT / "archive" / "generation-1"
APP_DB = ROOT / "var" / "goldscalp.sqlite3"
STORE = ROOT / "var" / "monthly" / "monthly.sqlite3"
REPORTS = ROOT / "var" / "monthly" / "reports"
FORWARD_FROM = datetime(2026, 11, 14, tzinfo=UTC)
FAMILY = {"A": "trend", "B": "breakout", "C": "reversal", "R1": "trend", "R2": "breakout", "R3": "reversal",
          "R4": "breakout", "R5": "trend", "R6": "trend", "R7": "breakout", "R8": "reversal", "R9": "breakout",
          "VOL-C": "trend", "VOL-F": "reversal", "STR-0": "trend", "STR-X": "trend", "SES-B": "breakout", "H6.4": "trend"}
REQUIRED = ["A v2", "B v2", "C v4", *[f"R{i} original" for i in range(1, 10)], "A v3", "B v3", "C v5",
            *[f"R{i} v2" for i in range(1, 10)], "VOL-C", "VOL-F", "STR-0", "STR-X", "SES-B", "H6.4"]
# One archival classification per headline version, summarising the verdicts recorded for it (attached verbatim).
FINAL = {
    "A v1": ("superseded", "Phase 3 template version, replaced by A v2 before any formal verdict."),
    "B v1": ("superseded", "Phase 3 template version, replaced by B v2 before any formal verdict."),
    "C v1": ("superseded", "Phase 3 template version; no formal verdict."),
    "C v2": ("superseded", "Phase 3 iteration; no formal verdict."),
    "C v3": ("superseded", "Phase 3 iteration (same rules as C v1); no formal verdict."),
    "A v2": ("rejected", "Phase 5.2 out of sample -0.005 R/trade over 157 trades."),
    "B v2": ("inconclusive: insufficient evidence", "Phase 5.2 out of sample +0.029 R/trade over only 24 trades."),
    "C v4": ("rejected", "Phase 5.2 out of sample -0.005 R/trade over 249 trades."),
    "R1 original": ("break-even / insufficient evidence", "5.2 +0.042 (358, inconclusive); 5.3 +0.003 (726, inconclusive)."),
    "R2 original": ("rejected", "5.2 +0.022 (195, inconclusive); 5.3 -0.190 (413, rejected)."),
    "R3 original": ("rejected", "5.2 -0.045 (34)."),
    "R4 original": ("rejected", "5.2 -0.087 (40)."),
    "R5 original": ("rejected", "5.2 -0.017 (167); 5.3 -0.098 (311)."),
    "R6 original": ("rejected", "5.2 -0.170 (62)."),
    "R7 original": ("rejected", "5.2 +0.689 over only 9 trades (inconclusive); 5.3 -0.353 (16, rejected)."),
    "R8 original": ("rejected", "5.2 -0.171 (3)."),
    "R9 original": ("rejected", "5.2 -0.150 (51)."),
    "A v3": ("rejected", "5.2 -0.018 (148)."),
    "B v3": ("inconclusive: insufficient evidence", "5.2 +0.025 over 35 trades."),
    "C v5": ("rejected", "5.2 -0.328 (9)."),
    "R1 v2": ("break-even / insufficient evidence", "5.2 +0.026 (258, inconclusive); 5.3 +0.006 (368, inconclusive); retired."),
    "R2 v2": ("promising at an earlier stage, subsequently rejected", "5.2 +0.054 (147, promising); 5.3 -0.054 (273, rejected)."),
    "R3 v2": ("rejected", "5.2 -0.163 (28)."),
    "R4 v2": ("inconclusive: insufficient evidence", "5.2 +0.121 over only 37 trades."),
    "R5 v2": ("rejected", "5.2 +0.034 (123, inconclusive); 5.3 -0.053 (222, rejected)."),
    "R6 v2": ("rejected", "5.2 -0.139 (45)."),
    "R7 v2": ("rejected", "5.2 +0.400 over 21 trades (inconclusive); 5.3 -0.107 (37, rejected)."),
    "R8 v2": ("rejected", "5.2 -0.062 (25)."),
    "R9 v2": ("rejected", "5.2 -0.212 (51)."),
    "VOL-C": ("development-only observation; negative as the sealed control",
              "Development +0.102 (294); sealed 2023-2025 -0.029 (762) as the H6.4 control (comparison only)."),
    "VOL-F": ("development-only observation: not supported", "Development -0.012 (129); H6.1 not supported."),
    "STR-0": ("development-only observation (control)", "Development +0.101 (241); the H6.2 control."),
    "STR-X": ("rejected (development)", "Development +0.062 (111), worse than its control; H6.2 rejected."),
    "SES-B": ("inconclusive: insufficient evidence (development)", "Development +0.110 (127); 22 London-open trades."),
    "H6.4": ("sealed-validation result: rejected", "Development +0.113 (269); sealed 2023-2025 -0.006 (663)."),
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jdump(o: Any) -> bytes:
    return (json.dumps(o, indent=2, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def _ro(p: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _j(v: Any) -> Any:
    try:
        return json.loads(v) if isinstance(v, str) else v
    except ValueError:
        return v


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")


def short_name(name: str) -> str:
    m = re.match(r"^Example ([A-Z]) (v\d+)", name or "")
    return f"{m.group(1)} {m.group(2)}" if m else name


# ------------------------------------------------------------------ discovery of every version
class Inventory:
    def __init__(self) -> None:
        self.v: dict[str, dict[str, Any]] = {}

    def add(self, fp: str, cfg_text: str | None, strategy: str, label: str, source: str, study: str | None = None,
            key: str | None = None, headline: bool = False, rank: int = 9) -> None:
        e = self.v.setdefault(fp, {"fingerprint": fp, "strategy": strategy, "family": FAMILY.get(strategy, "other"),
                                   "config_text": None, "aliases": [], "label": label, "rank": rank, "headline": False})
        if cfg_text and not e["config_text"]:
            e["config_text"] = cfg_text
        e["aliases"].append({"source": source, "study": study, "key": key, "label": label})
        if headline and rank < e["rank"]:
            e["label"], e["rank"] = label, rank
        e["headline"] = e["headline"] or headline


def collect(app_db: Path = APP_DB, store: Path = STORE, reports: Path = REPORTS) -> dict[str, Any]:
    inv = Inventory()
    a, s = _ro(app_db), _ro(store)
    try:
        for r in a.execute("SELECT strategy_id, version, config, fingerprint, note FROM strategy_versions "
                           "ORDER BY strategy_id, version"):
            k = "ABC"[r["strategy_id"] - 1]
            inv.add(r["fingerprint"], r["config"], k, f"{k} v{r['version']}", "app:strategy_versions",
                    key=f"{r['strategy_id']}/{r['version']}", headline=True, rank=1)
        for r in a.execute("SELECT id, strategy_id, base_version, candidate, candidate_fingerprint FROM strategy_proposals "
                           "ORDER BY id"):
            k = "ABC"[r["strategy_id"] - 1]
            inv.add(r["candidate_fingerprint"], r["candidate"], k, f"{k} v{r['base_version']} proposal {r['id']}",
                    "app:strategy_proposals", key=r["id"], headline=True, rank=2)
        for r in s.execute("SELECT evaluation_id, key, config, fingerprint FROM candidates ORDER BY evaluation_id, key"):
            inv.add(r["fingerprint"], r["config"], r["key"], f"{r['key']} original", "store:candidates",
                    study=r["evaluation_id"], key=r["key"], headline=True, rank=3)
        for r in s.execute("SELECT evaluation_id, strategy_key, kind, timeframe, config, fingerprint FROM runs "
                           "ORDER BY evaluation_id, strategy_key, timeframe"):
            inv.add(r["fingerprint"], r["config"], r["strategy_key"], r["strategy_key"], "store:runs",
                    study=r["evaluation_id"], key=f"{r['strategy_key']}|{r['timeframe']}")
        for r in s.execute("SELECT id, study_id, strategy, name, config, fingerprint, chosen_variant FROM research_versions "
                           "ORDER BY id"):
            inv.add(r["fingerprint"], r["config"], r["strategy"], short_name(r["name"]), "store:research_versions",
                    study=r["study_id"], key=r["chosen_variant"], headline=True, rank=2)
        for r in s.execute("SELECT study_id, key, config, fingerprint FROM robustness_configs ORDER BY study_id, key"):
            inv.add(r["fingerprint"], r["config"], r["key"].split("|")[0], r["key"], "store:robustness_configs",
                    study=r["study_id"], key=r["key"])
        for table, idcol in (("validation_studies", "id"), ("discovery_trials", "id"), ("sealed_runs", "id")):
            for r in s.execute(f"SELECT {idcol} AS sid, configs FROM {table} ORDER BY created_at"):
                for key, cfg in (_j(r["configs"]) or {}).items():
                    fp = builder.fingerprint(cfg)
                    strat = key.split("|")[0]
                    head = table != "validation_studies"
                    inv.add(fp, json.dumps(cfg), strat, key, f"store:{table}", study=r["sid"], key=key,
                            headline=head, rank=4)
    finally:
        a.close()
        s.close()
    return {"inventory": inv.v}


# ------------------------------------------------------------------ evaluations and trades
def evaluations(store: Path = STORE) -> tuple[dict[str, list], dict[tuple, list]]:
    """fingerprint -> evaluation rows; (strategy, study) -> trade-record lines."""
    ev: dict[str, list] = {}
    trades: dict[tuple, list] = {}
    s = _ro(store)
    try:
        studies = {}
        for r in s.execute("SELECT id, params FROM evaluations"):
            p = _j(r["params"]) or {}
            studies[r["id"]] = ("5.1" if p.get("risk_study") else "5", p.get("month"))
        for r in s.execute("SELECT id, evaluation_id, strategy_key, kind, timeframe, status, fingerprint, reason, "
                           "result_hash, dataset, json_extract(result, '$.summary') AS summary, "
                           "json_extract(result, '$.trades') AS trades FROM runs ORDER BY evaluation_id, strategy_key, timeframe"):
            ph, month = studies.get(r["evaluation_id"], ("5", None))
            row = {"phase": ph, "study": r["evaluation_id"], "run": r["id"], "timeframe": r["timeframe"], "window": month,
                   "period": month, "status": r["status"], "reason": r["reason"], "metrics": _j(r["summary"]),
                   "dataset": _j(r["dataset"]), "result_hash": r["result_hash"]}
            ev.setdefault(r["fingerprint"], []).append(row)
            if r["trades"]:
                trades.setdefault((r["strategy_key"], f"phase{ph}_{r['evaluation_id']}"), []).append(
                    {**{k: row[k] for k in ("study", "run", "timeframe", "window", "status")}, "fingerprint": r["fingerprint"],
                     "trades": _j(r["trades"])})

        def windows(params: dict) -> dict:
            return {w["key"]: w.get("month") for w in (params or {}).get("windows", [])}

        rb_fp = {(r["study_id"], r["key"]): r["fingerprint"] for r in s.execute("SELECT study_id, key, fingerprint FROM robustness_configs")}
        rb_w = {r["id"]: windows(_j(r["params"])) for r in s.execute("SELECT id, params FROM robustness_studies")}
        sources = [("5.2", "robustness_results", "study_id", lambda st, k: rb_fp.get((st, k)), rb_w)]
        for table, idcol, ph in (("validation_studies", "id", "5.3"), ("discovery_trials", "id", "6"), ("sealed_runs", "id", "6")):
            fpm, wm = {}, {}
            for r in s.execute(f"SELECT {idcol} AS sid, configs, params FROM {table}"):
                for k, cfg in (_j(r["configs"]) or {}).items():
                    fpm[(r["sid"], k)] = builder.fingerprint(cfg)
                wm[r["sid"]] = windows(_j(r["params"]))
            res = {"validation_studies": ("validation_results", "study_id"), "discovery_trials": ("discovery_results", "trial_id"),
                   "sealed_runs": ("sealed_results", "run_id")}[table]
            sources.append((ph, res[0], res[1], (lambda m: lambda st, k: m.get((st, k)))(fpm), wm))
        for ph, table, idcol, fpof, wm in sources:
            for r in s.execute(f"SELECT {idcol} AS sid, config_key, timeframe, window, status, reason, metrics, trades, "
                               f"dataset, result_hash FROM {table} ORDER BY {idcol}, config_key, timeframe, window"):
                fp = fpof(r["sid"], r["config_key"])
                period = (wm.get(r["sid"]) or {}).get(r["window"], r["window"])
                row = {"phase": ph, "study": r["sid"], "config_key": r["config_key"], "timeframe": r["timeframe"],
                       "window": r["window"], "period": period, "status": r["status"], "reason": r["reason"],
                       "metrics": _j(r["metrics"]), "dataset": _j(r["dataset"]), "result_hash": r["result_hash"]}
                ev.setdefault(fp or f"unmapped:{r['sid']}:{r['config_key']}", []).append(row)
                if r["trades"]:
                    strat = r["config_key"].split("|")[0]
                    trades.setdefault((strat, f"phase{ph}_{r['sid']}"), []).append(
                        {**{k: row[k] for k in ("study", "config_key", "timeframe", "window", "period", "status")},
                         "fingerprint": fp, "trades": _j(r["trades"])})
    finally:
        s.close()
    return ev, trades


def recorded_verdicts(store: Path = STORE, reports: Path = REPORTS, app_db: Path = APP_DB) -> dict[str, list]:
    """Verdicts exactly as the studies recorded them, keyed by fingerprint."""
    out: dict[str, list] = {}
    s = _ro(store)
    try:
        rb_fp = {(r["study_id"], r["key"]): r["fingerprint"] for r in s.execute("SELECT study_id, key, fingerprint FROM robustness_configs")}
        for r in s.execute("SELECT id, study_id, fingerprint, verdict, evidence FROM research_versions"):
            out.setdefault(r["fingerprint"], []).append({"source": f"store:research_versions:{r['id']}", "phase": "5.2",
                                                         "definition": "verdicts@1", "verdict": r["verdict"]})
        for f in sorted(reports.glob("rb_*-final-verdicts.json")):
            j = json.loads(f.read_text(encoding="utf-8"))
            for k, st in j["final"]["strategies"].items():
                for role in ("original", "redesign"):
                    v = st[role]
                    key = f"{k}|original" if role == "original" else v.get("development_choice")
                    fp = rb_fp.get((j["study"], key))
                    out.setdefault(fp or f"unmapped:{key}", []).append(
                        {"source": f"var/monthly/reports/{f.name}", "phase": "5.2", "definition": j["final"]["definition"],
                         "role": role, "class": v.get("class"), "why": v.get("why")})
        for f in sorted(reports.glob("vl_*-validation.json")):
            j = json.loads(f.read_text(encoding="utf-8"))
            cfgs = j["params"]["configs"]
            for _k, pair in j["analysis"]["strategies"].items():
                for role, v in pair.items():
                    out.setdefault(cfgs[v["key"]]["fingerprint"], []).append(
                        {"source": f"var/monthly/reports/{f.name}", "phase": "5.3", "definition": j["params"]["definition"],
                         "role": role, "class": v.get("class"), "why": v.get("why")})
        for r in s.execute("SELECT id, hypothesis, configs, analysis FROM discovery_trials ORDER BY created_at"):
            a = _j(r["analysis"]) or {}
            for cfg in (_j(r["configs"]) or {}).values():
                out.setdefault(builder.fingerprint(cfg), []).append(
                    {"source": f"store:discovery_trials:{r['id']}", "phase": "6", "hypothesis": r["hypothesis"],
                     "prediction_holds": (a.get("prediction") or {}).get("holds"), "gate": a.get("gate")})
        for r in s.execute("SELECT id, purpose, configs, analysis FROM sealed_runs WHERE status = 'COMPLETED' ORDER BY created_at"):
            a = _j(r["analysis"]) or {}
            for k, cfg in (_j(r["configs"]) or {}).items():
                ass = a.get("assessment") if k == "H6.4" else a.get("control_assessment")
                out.setdefault(builder.fingerprint(cfg), []).append(
                    {"source": f"store:sealed_runs:{r['id']}", "phase": "6", "purpose": r["purpose"], "config": k,
                     "role": "candidate" if k == "H6.4" else "control (comparison only)",
                     "class": (ass or {}).get("class"), "failed": (ass or {}).get("failed")})
    finally:
        s.close()
    a = _ro(app_db)
    try:
        for r in a.execute("SELECT id, candidate_fingerprint, verdict, status, decided_at FROM strategy_proposals"):
            out.setdefault(r["candidate_fingerprint"], []).append(
                {"source": f"app:strategy_proposals:{r['id']}", "phase": "4", "verdict": r["verdict"], "status": r["status"]})
    finally:
        a.close()
    return out


# ------------------------------------------------------------------ the plan: every archive file and its origin
def _rows_json(db: Path, sql: str, json_cols: tuple = ()) -> list[dict]:
    con = _ro(db)
    try:
        return [{k: (_j(v) if k in json_cols else v) for k, v in dict(r).items()} for r in con.execute(sql)]
    finally:
        con.close()


def plan() -> dict[str, dict[str, Any]]:
    """relative path -> {"bytes": ...} for exports or {"copy": Path} for verbatim copies."""
    P: dict[str, dict[str, Any]] = {}
    absent: list[str] = []

    def put(rel: str, data: bytes, origin: str) -> None:
        P[rel] = {"bytes": data, "origin": origin}

    def copy(rel: str, src: Path) -> None:
        if src.is_file():
            P[rel] = {"copy": src, "origin": str(src.relative_to(ROOT)).replace("\\", "/")}
        else:
            absent.append(str(src.relative_to(ROOT)).replace("\\", "/"))

    inv = collect()["inventory"]
    ev, trades = evaluations()
    verdicts = recorded_verdicts()
    entries = []
    by_strategy: dict[str, list] = {}
    for fp, e in sorted(inv.items(), key=lambda kv: (kv[1]["strategy"], kv[1]["rank"], kv[1]["label"])):
        cfg = _j(e["config_text"]) if e["config_text"] else None
        recomputed = builder.fingerprint(cfg) if isinstance(cfg, dict) else None
        head = e["headline"]
        cls = FINAL.get(e["label"]) if head else None
        if head and cls is None and "proposal" in e["label"]:
            cls = ("inconclusive: insufficient sample (proposal not adopted)", "Phase 4 improvement proposal; verdict "
                   "insufficient_sample, status inconclusive.")
        if not head:
            studies = sorted({a["study"] for a in e["aliases"] if a["study"]})
            cls = ("development variant (not selected)" if "rb_7ca33b7b5a81" in studies else
                   "variant from a superseded or cancelled study", None)
        fam = e["family"]
        base = f"strategies/{fam}/{slug(e['strategy'])}"
        entry = {"label": e["label"], "strategy": e["strategy"], "family": fam, "fingerprint": fp,
                 "fingerprint_recomputed": recomputed, "fingerprint_matches": recomputed == fp if recomputed else None,
                 "headline": head, "classification": cls[0] if cls else None, "evidence": cls[1] if cls else None,
                 "aliases": e["aliases"], "evaluations": len(ev.get(fp, [])),
                 "periods": sorted({x["period"] for x in ev.get(fp, []) if x.get("period")}),
                 "studies": sorted({x["study"] for x in ev.get(fp, [])}), "recorded_verdicts": verdicts.get(fp, [])}
        if head:
            d = f"{base}/{slug(e['label'])}"
            put(f"{d}/config.json", (e["config_text"] or "null").encode("utf-8"), "verbatim stored configuration")
            put(f"{d}/fingerprint.json", jdump({"stored": fp, "recomputed": recomputed,
                                               "matches": entry["fingerprint_matches"]}), "builder.fingerprint")
            put(f"{d}/evaluations.json", jdump(ev.get(fp, [])), "evaluation rows from the research store")
            put(f"{d}/verdicts.json", jdump({"archival_classification": entry["classification"],
                                             "evidence": entry["evidence"], "recorded": verdicts.get(fp, []),
                                             "aliases": e["aliases"]}), "recorded verdicts, verbatim")
            entry["path"] = d
        else:
            entry["path"] = f"{base}/variants.json"
            by_strategy.setdefault(base, []).append({**entry, "config": cfg, "evaluation_rows": ev.get(fp, [])})
        entries.append(entry)
    for base, items in by_strategy.items():
        put(f"{base}/variants.json", jdump(items), "Phase 5.2 variant configurations and their evaluation rows")
    for (strat, study), lines in sorted(trades.items()):
        fam = FAMILY.get(strat, "other")
        body = "".join(json.dumps(x, ensure_ascii=False, default=str) + "\n" for x in lines).encode("utf-8")
        put(f"strategies/{fam}/{slug(strat)}/trades/{slug(study)}.jsonl.gz", gzip.compress(body, mtime=0),
            "trade-level simulated records (gzip JSON lines, one line per result)")
    unmapped = sorted(k for k in ev if k.startswith("unmapped:"))
    inventory = {"entries": [{k: v for k, v in x.items() if k != "aliases"} | {"aliases": x["aliases"]} for x in entries],
                 "unmapped_evaluation_keys": unmapped}
    put("INVENTORY.json", jdump(inventory), "derived from every strategy record")
    csv_lines = ["label,strategy,family,headline,fingerprint,fingerprint_matches,classification,evaluations,path"]
    for x in entries:
        csv_lines.append(",".join(json.dumps(str(x[k]) if x[k] is not None else "") for k in
                                  ("label", "strategy", "family", "headline", "fingerprint", "fingerprint_matches",
                                   "classification", "evaluations", "path")))
    put("INVENTORY.csv", ("\n".join(csv_lines) + "\n").encode("utf-8"), "derived from INVENTORY.json")

    # phase folders: verbatim copies plus study records exported from the databases
    for f in ("PHASE2_ARCHITECTURE.md", "PHASE_2_IMPLEMENTATION_REPORT.md", "PHASE3_ARCHITECTURE.md",
              "PHASE_3_IMPLEMENTATION_REPORT.md", "ARCHITECTURE_AUDIT.md", "PROPOSED_ARCHITECTURE.md", "REUSE_VS_REBUILD.md"):
        copy(f"phases/phase_1_to_3_foundations/{f}", ROOT / f)
    put("phases/phase_1_to_3_foundations/lab_runs.json", jdump({
        "research_runs": _rows_json(APP_DB, "SELECT * FROM research_runs ORDER BY created_at", ("params", "result")),
        "performance_runs": _rows_json(APP_DB, "SELECT * FROM performance_runs ORDER BY created_at", ("params", "result"))}),
        "app: research_runs, performance_runs")
    for f in ("PHASE4_ARCHITECTURE.md", "PHASE_4_IMPLEMENTATION_REPORT.md"):
        copy(f"phases/phase_4_live_validation/{f}", ROOT / f)
    for f in sorted((GEN1 / "phase4").glob("*")):
        copy(f"phases/phase_4_live_validation/final_state/{f.name}", f)
    put("phases/phase_4_live_validation/improvement_studies.json", jdump({
        "research_studies": _rows_json(APP_DB, "SELECT * FROM research_studies", ("params", "result")),
        "strategy_proposals": _rows_json(APP_DB, "SELECT * FROM strategy_proposals",
                                         ("candidate", "diff", "evidence", "limitations", "validation"))}),
        "app: research_studies, strategy_proposals")
    copy("phases/phase_5_monthly/PHASE5_ARCHITECTURE.md", ROOT / "PHASE5_ARCHITECTURE.md")
    copy("phases/phase_5_monthly/PHASE_5_IMPLEMENTATION_REPORT.md", ROOT / "PHASE_5_IMPLEMENTATION_REPORT.md")
    evs = _rows_json(STORE, "SELECT * FROM evaluations ORDER BY created_at", ("params", "availability", "frozen", "research"))
    put("phases/phase_5_monthly/evaluations.json", jdump([e for e in evs if not (e["params"] or {}).get("risk_study")]),
        "store: evaluations")
    put("phases/phase_5_monthly/candidates.json", jdump(_rows_json(STORE, "SELECT * FROM candidates ORDER BY evaluation_id, key",
                                                                   ("config", "documentation", "research"))), "store: candidates")
    put("phases/phase_5_1_risk_scenarios/risk_studies.json", jdump({
        "risk_studies": _rows_json(STORE, "SELECT * FROM risk_studies", ("params", "evaluation_ids")),
        "scenario_evaluations": [e for e in evs if (e["params"] or {}).get("risk_study")]}), "store: risk_studies, evaluations")
    for f in sorted(REPORTS.glob("rb_*")) + [REPORTS / "verdicts-framework-frozen.json", REPORTS / "verdicts-framework-frozen-v2.json"]:
        copy(f"phases/phase_5_2_robustness/{f.name}", f)
    copy("phases/phase_5_2_robustness/PHASE_5_2_RESEARCH_REPORT.md", ROOT / "PHASE_5_2_RESEARCH_REPORT.md")
    put("phases/phase_5_2_robustness/robustness_studies.json", jdump(_rows_json(
        STORE, "SELECT * FROM robustness_studies ORDER BY created_at", ("params", "analysis"))), "store: robustness_studies")
    put("phases/phase_5_2_robustness/research_versions.json", jdump(_rows_json(
        STORE, "SELECT * FROM research_versions ORDER BY id", ("config", "evidence"))), "store: research_versions")
    for f in sorted(REPORTS.glob("vl_*")) + [REPORTS / "validation-protocol-frozen.json"]:
        copy(f"phases/phase_5_3_validation/{f.name}", f)
    copy("phases/phase_5_3_validation/PHASE_5_3_VALIDATION_REPORT.md", ROOT / "PHASE_5_3_VALIDATION_REPORT.md")
    put("phases/phase_5_3_validation/validation_studies.json", jdump(_rows_json(
        STORE, "SELECT * FROM validation_studies", ("params", "configs", "analysis"))), "store: validation_studies")
    copy("phases/phase_5_closure/phase5-research-cycle-closed.json", REPORTS / "phase5-research-cycle-closed.json")
    for f in ("discovery-protocol-frozen.json", "phase6-development.json", "phase6-development.md",
              "h64-protocol-frozen.json", "h64-report.json", "h64-report.md"):
        copy(f"phases/phase_6_discovery/{f}", REPORTS / f)
    for f in ("PHASE_6_PROTOCOL.md", "PHASE_6_H64_REGISTRATION.md"):
        copy(f"phases/phase_6_discovery/{f}", ROOT / f)
    for f in sorted((ROOT / "config" / "discovery").glob("*.json")):
        copy(f"phases/phase_6_discovery/config/{f.name}", f)
    put("phases/phase_6_discovery/discovery_trials.json", jdump(_rows_json(
        STORE, "SELECT * FROM discovery_trials ORDER BY created_at", ("params", "configs", "analysis"))), "store: discovery_trials")
    put("phases/phase_6_discovery/sealed_runs.json", jdump(_rows_json(
        STORE, "SELECT * FROM sealed_runs ORDER BY created_at", ("params", "configs", "analysis"))), "store: sealed_runs")
    put("datasets/datasets.json", jdump({
        "application_series": _rows_json(APP_DB, "SELECT * FROM series ORDER BY id"),
        "application_imports": _rows_json(APP_DB, "SELECT * FROM data_imports ORDER BY id", ("report_json",)),
        "research_series": _rows_json(STORE, "SELECT * FROM series ORDER BY id"),
        "research_imports": _rows_json(STORE, "SELECT * FROM data_imports ORDER BY id", ("report_json",)),
        "research_import_labels": _rows_json(STORE, "SELECT * FROM import_labels ORDER BY import_id"),
        "candle_ranges": _candle_ranges()}), "app and store: series, data_imports, candle ranges")
    for f in sorted(GEN1.rglob("*")):
        if f.is_file():
            copy(f"snapshots/generation-1/{f.relative_to(GEN1).as_posix()}", f)
    put("KNOWN_ISSUES.md", known_issues().encode("utf-8"), "compiled from the records (see each item's source)")
    put("MISSING_ARTIFACTS.md", missing_artifacts(entries, trades, absent).encode("utf-8"), "compiled from the records")
    put("README.md", _readme().encode("utf-8"), "archive overview")
    return P


def _candle_ranges() -> list[dict]:
    out = []
    for name, db in (("application", APP_DB), ("research", STORE)):
        con = _ro(db)
        try:
            for r in con.execute("SELECT s.key, c.tf, COUNT(*) AS n, MIN(c.ts) AS first, MAX(c.ts) AS last, "
                                 "SUM(c.ts >= ?) AS forward FROM candles c JOIN series s ON s.id = c.series_id "
                                 "GROUP BY s.key, c.tf ORDER BY s.key, c.tf", (int(FORWARD_FROM.timestamp()),)):
                d = dict(r)
                d["first"] = datetime.fromtimestamp(d["first"], UTC).isoformat()
                d["last"] = datetime.fromtimestamp(d["last"], UTC).isoformat()
                out.append({"database": name, **d})
        finally:
            con.close()
    return out


def known_issues() -> str:
    s = _ro(STORE)
    try:
        inc = [(r["id"], [(x["strategy_key"], x["timeframe"], x["status"], x["reason"]) for x in s.execute(
            "SELECT strategy_key, timeframe, status, reason FROM runs WHERE evaluation_id = ? AND status != 'COMPLETED' "
            "ORDER BY strategy_key, timeframe", (r["id"],))]) for r in s.execute(
            "SELECT id FROM evaluations WHERE status != 'COMPLETED' ORDER BY created_at")]
        rb = [dict(r) for r in s.execute("SELECT id, status, error, created_at FROM robustness_studies ORDER BY created_at")]
        risk = _j(s.execute("SELECT params FROM risk_studies").fetchone()[0]) or {}
    finally:
        s.close()
    L = ["# Known issues, limitations and audit findings (Phases 1-6)", "",
         "Compiled from the stored records. Each item names its source. Nothing here changes a result.", "",
         "## Incomplete or cancelled studies", ""]
    for eid, runs in inc:
        reasons = sorted({r[3] for r in runs if r[3]})
        L.append(f"- Phase 5 evaluation `{eid}` is INCOMPLETE ({len(runs)} runs not completed). Recorded reasons: "
                 + ("; ".join(reasons) if reasons else "none recorded") + ". Source: store `evaluations`, `runs`.")
    for r in rb:
        if r["id"] != "rb_7ca33b7b5a81":
            L.append(f"- Robustness study `{r['id']}` ({r['created_at']}): status {r['status']}"
                     f"{', error ' + repr(r['error']) if r['error'] else ''}. Only `rb_7ca33b7b5a81` has final verdicts; "
                     "its configurations are archived as variants of a superseded or cancelled study.")
    L += ["", "## Bugs found and fixed during the research", ""]
    for rr in risk.get("reruns", []):
        L.append(f"- Phase 5.1 risk study re-run at {rr.get('at')}: {rr.get('reason')} Replaced result hashes are listed "
                 "in `phases/phase_5_1_risk_scenarios/risk_studies.json`.")
    L += ["", "## Limitations of the evidence", "",
          "- Phase 4 live validation produced no live evidence: the monitors were started on Saturday 14 November 2026 "
          "after the weekly close and stopped on 15 November before the market reopened, so they recorded no "
          "evaluation, signal or trade (`phases/phase_4_live_validation/final_state`).",
          "- MT5 terminal verification: the first record FAILED and the second was PARTIAL (`mt5_verifications`); "
          "the fresh-tick VERIFIED record after a market open was never obtained.",
          "- Profit factor is computed on money and expectancy on R, so a result can show PF above 1 with negative "
          "expectancy in R (for example H6.4 on the sealed block: PF 1.04, -0.006 R/trade).",
          "- Small samples: several positive figures rest on fewer than 40 trades (R7, R7 v2, R4 v2, B v2, B v3).",
          "- Development data was reused across Phase 5.2, 5.3 and Phase 6 design; only the Phase 5.3 months "
          "(January-May 2026) for its locked candidates and the sealed 2023-2025 block for H6.4 were unseen when used.",
          "- The H6.4 report shows 'Half 1 / Half 2' as 0.00 for the development run, where halves do not apply; the "
          "report code is part of the frozen H6.4 files and was left unchanged.",
          "- PDF and CSV exports are generated on demand from the stored data; apart from the report files listed in "
          "the phase folders they were not stored as files.",
          "", "## Audit findings", "",
          "- The Phase 0 audit of the earlier SpicyStock repository is preserved as "
          "`phases/phase_1_to_3_foundations/ARCHITECTURE_AUDIT.md` (with REUSE_VS_REBUILD.md); the repository itself "
          "was not modified or copied."]
    return "\n".join(L) + "\n"


def missing_artifacts(entries: list[dict], trades: dict, absent: list[str] | None = None) -> str:
    L = ["# Missing artifacts", "", "Listed explicitly; nothing was reconstructed or invented.", ""]
    L.append(f"- Expected source files that do not exist: {', '.join(absent) if absent else 'none'}.")
    no_cfg = [e["label"] for e in entries if e["fingerprint_recomputed"] is None]
    mism = [f"{e['label']} (stored {e['fingerprint'][:12]}, recomputed {str(e['fingerprint_recomputed'])[:12]})"
            for e in entries if e["fingerprint_matches"] is False]
    no_eval = [e["label"] for e in entries if e["headline"] and e["evaluations"] == 0]
    L.append(f"- Versions without a stored configuration: {', '.join(no_cfg) if no_cfg else 'none'}.")
    L.append(f"- Fingerprints that do not recompute from the stored configuration: {'; '.join(mism) if mism else 'none'}.")
    L.append("- Headline versions with no evaluation rows in the research store: "
             f"{', '.join(no_eval) if no_eval else 'none'}. Their evidence, where any, is in the Phase 3 Lab runs "
             "(`phases/phase_1_to_3_foundations/lab_runs.json`) or the Phase 4 records.")
    L += ["- Results with status INCOMPLETE or FAILED have no trade records (they never ran to completion); their "
          "rows and reasons are kept in each version's `evaluations.json`.",
          "- Phase 4: no monitor evaluations, signals, live trades, journal entries or demo-execution trades exist (the "
          "tables are empty), so there are no Phase 4 trade-level records.",
          "- Phase 3 Lab runs store their results inside `lab_runs.json`; no separate trade files were kept by the Lab.",
          "- Raw MT5 export files used for the imports were not kept by the application; their names, SHA-256 hashes, "
          "row counts and ranges are in `datasets/datasets.json`, and the imported candles are in the database "
          "snapshots.",
          "- Forward data: none exists in the archive. No candle on or after 14 November 2026 was stored when the "
          "snapshots were taken (see `datasets/datasets.json`, column `forward`)."]
    return "\n".join(L) + "\n"


def _readme() -> str:
    return """# GoldScalp AI research archive: Phases 1-6

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
"""


# ------------------------------------------------------------------ build, verify, check
def _digest(item: dict[str, Any]) -> tuple[str, int]:
    if "bytes" in item:
        return sha(item["bytes"]), len(item["bytes"])
    return sha_file(item["copy"]), item["copy"].stat().st_size


def write(P: dict[str, dict[str, Any]], out: Path) -> dict[str, Any]:
    files = {}
    for rel, item in sorted(P.items()):
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if "bytes" in item:
            dest.write_bytes(item["bytes"])
        else:
            shutil.copyfile(item["copy"], dest)
        h, n = _digest(item)
        if sha_file(dest) != h:
            raise SystemExit(f"{rel}: written file does not match its source")
        files[rel] = {"sha256": h, "bytes": n, "origin": item["origin"],
                      **({"source_sha256": h} if "copy" in item else {})}
    return files


def verify(out: Path = OUT, P: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    P = P if P is not None else plan()
    mf = json.loads((out / "ARCHIVE_MANIFEST.json").read_text(encoding="utf-8")) if (out / "ARCHIVE_MANIFEST.json").exists() else None
    files = mf["files"] if mf else {}
    mismatched, missing, unexpected = [], [], []
    for rel, item in P.items():
        h, _ = _digest(item)
        p = out / rel
        if not p.exists():
            missing.append(rel)
        elif sha_file(p) != h or (files and files.get(rel, {}).get("sha256") != h):
            mismatched.append(rel)
    if files:
        unexpected = [r for r in files if r not in P and r not in ("ARCHIVE_REPORT.md",)]
    inv = json.loads(P["INVENTORY.json"]["bytes"])
    labels = [e["label"] for e in inv["entries"]]
    fps = [e["fingerprint"] for e in inv["entries"]]
    fresh = set(collect()["inventory"])
    return {"files_planned": len(P), "missing": missing, "mismatched": mismatched, "unexpected": unexpected,
            "every_fingerprint_archived": fresh <= set(fps), "duplicate_fingerprints": len(fps) - len(set(fps)),
            "duplicate_headline_labels": sorted({x for x in labels if labels.count(x) > 1
                                                 and any(e["headline"] and e["label"] == x for e in inv["entries"])}),
            "required_missing": [x for x in REQUIRED if x not in labels],
            "fingerprint_mismatches": [e["label"] for e in inv["entries"] if e["fingerprint_matches"] is False],
            "unclassified_headlines": [e["label"] for e in inv["entries"] if e["headline"] and not e["classification"]],
            "unmapped_evaluation_keys": inv["unmapped_evaluation_keys"]}


def integrity() -> dict[str, Any]:
    out: dict[str, Any] = {}
    from goldscalp import archive as gen
    from goldscalp.monthly import closure, discovery_report, sealed_report
    out["generation_1_archive"] = {k: v for k, v in gen.check().items() if k in ("archived", "unchanged", "files", "writable")}
    for name, mod in (("phase5_closure", closure), ("phase6_discovery_protocol", discovery_report),
                      ("h64_protocol", sealed_report)):
        try:
            c = mod.check()
            out[name] = {k: c.get(k) for k in ("frozen", "closed", "unchanged") if k in c}
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": str(exc)}
    try:
        from goldscalp.monthly import validation_report
        c = validation_report.check()
        out["phase53_protocol"] = {k: c.get(k) for k in ("frozen", "unchanged") if k in c}
    except Exception as exc:  # noqa: BLE001
        out["phase53_protocol"] = {"error": str(exc)}
    a = _ro(APP_DB)
    try:
        out["phase4"] = {"monitors": [dict(r) for r in a.execute("SELECT id, status, stopped_at FROM monitored_strategies")],
                         "strategies_archived": [dict(r) for r in a.execute("SELECT id, name, archived FROM strategies")],
                         "open_phase4_positions": a.execute(
                             "SELECT COUNT(*) FROM paper_positions p JOIN monitored_strategies m ON m.account_id = p.account_id "
                             "WHERE p.status = 'open'").fetchone()[0]}
    finally:
        a.close()
    fts = int(FORWARD_FROM.timestamp())
    fwd = {}
    for name, db in (("application", APP_DB), ("research", STORE)):
        con = _ro(db)
        try:
            n, last = con.execute("SELECT COUNT(*), MAX(ts) FROM candles WHERE ts >= ?", (fts,)).fetchone()
            fwd[name] = {"candles_from_2026_11_14": n,
                         "latest": datetime.fromtimestamp(last, UTC).isoformat() if last else None}
        finally:
            con.close()
    out["forward_data"] = fwd
    return out


def check(out: Path = OUT) -> dict[str, Any]:
    mfp = out / "ARCHIVE_MANIFEST.json"
    if not mfp.exists():
        return {"archived": False}
    mf = json.loads(mfp.read_text(encoding="utf-8"))
    side = (out / "ARCHIVE_MANIFEST.sha256").read_text(encoding="utf-8").split()[0]
    changed = [r for r, e in mf["files"].items() if not (out / r).exists() or sha_file(out / r) != e["sha256"]]
    writable = [r for r in mf["files"] if (out / r).exists() and os.access(out / r, os.W_OK)]
    listed = set(mf["files"]) | {"ARCHIVE_MANIFEST.json", "ARCHIVE_MANIFEST.sha256"}
    extra = [p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and p.relative_to(out).as_posix() not in listed]
    ok = sha_file(mfp) == side
    return {"archived": True, "files": len(mf["files"]), "manifest_unchanged": ok,
            "unchanged": ok and not changed and not extra, "changed": changed, "extra": extra, "writable": len(writable)}


def build(report_writer: Any = None) -> dict[str, Any]:
    if OUT.exists():
        raise SystemExit(f"{OUT} already exists: the archive is written once and never replaced")
    before = integrity()
    tmp = OUT.with_name(OUT.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp, onerror=lambda f, p, e: (os.chmod(p, stat.S_IWRITE), f(p)))
    P = plan()
    files = write(P, tmp)
    v = verify(tmp, P)
    after = integrity()
    summary = {"created_at": now_iso(), "files": files, "verification": v, "integrity_before": before,
               "integrity_after": after}
    inv = json.loads(P["INVENTORY.json"]["bytes"])
    if report_writer is not None:
        text = report_writer(inv, files, v, before, after).encode("utf-8")
        (tmp / "ARCHIVE_REPORT.md").write_bytes(text)
        files["ARCHIVE_REPORT.md"] = {"sha256": sha(text), "bytes": len(text), "origin": "archive report"}
        (ROOT / "PHASE_1_TO_6_ARCHIVE_REPORT.md").write_bytes(text)
    manifest = {"archive": "phase_1_to_6", "created_at": summary["created_at"], "files": files,
                "strategy_versions": [{k: e[k] for k in ("label", "strategy", "family", "fingerprint", "classification", "path")}
                                      for e in inv["entries"]],
                "verification": v, "integrity": after}
    (tmp / "ARCHIVE_MANIFEST.json").write_bytes(jdump(manifest))
    (tmp / "ARCHIVE_MANIFEST.sha256").write_text(sha_file(tmp / "ARCHIVE_MANIFEST.json") + "  ARCHIVE_MANIFEST.json\n",
                                                 encoding="utf-8")
    problems = v["missing"] or v["mismatched"] or not v["every_fingerprint_archived"] or v["duplicate_fingerprints"] \
        or v["required_missing"]
    if problems:
        raise SystemExit(f"verification failed; the archive was left unsealed in {tmp}: {json.dumps(v)[:2000]}")
    tmp.rename(OUT)
    for p in OUT.rglob("*"):
        if p.is_file():
            os.chmod(p, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    return {"archive": str(OUT), **{k: summary[k] for k in ("verification", "integrity_after")},
            "file_count": len(files) + 2, "check": check()}


def main(argv: list[str]) -> None:
    if argv[:1] == ["--build"]:
        from goldscalp.research_archive_report import render
        print(json.dumps(build(render), indent=2, default=str)[:6000])
    elif argv[:1] == ["--verify"]:
        print(json.dumps(verify(), indent=2)[:6000])
    elif argv[:1] == ["--check"]:
        print(json.dumps(check(), indent=2))
    elif argv[:1] == ["--dry-run"]:
        P = plan()
        v = json.loads(P["INVENTORY.json"]["bytes"])
        heads = [e for e in v["entries"] if e["headline"]]
        print(json.dumps({"files": len(P), "entries": len(v["entries"]), "headline": len(heads),
                          "labels": [e["label"] for e in heads], "unmapped": v["unmapped_evaluation_keys"],
                          "fp_mismatch": [e["label"] for e in v["entries"] if e["fingerprint_matches"] is False],
                          "bytes": sum(_digest(i)[1] for i in P.values())}, indent=1))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
