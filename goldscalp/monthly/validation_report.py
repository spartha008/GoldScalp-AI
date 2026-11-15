"""Phase 5.3: freeze the validation protocol, check it, and render the validation report (read-only on the store).

    python -m goldscalp.monthly.validation_report --freeze "<note>"
    python -m goldscalp.monthly.validation_report <validation_study_id> [out_dir]

The Phase 5.2 manifests (verdicts-framework-frozen*.json) are separate and never touched."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from goldscalp.monthly import runner
from goldscalp.monthly import validation as vl

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "var" / "monthly" / "monthly.sqlite3"
REPORTS = STORE.parent / "reports"
MANIFEST = REPORTS / "validation-protocol-frozen.json"       # v1; amendments -v2 ... (never overwritten)
FILES = ("goldscalp/monthly/validation.py", "goldscalp/monthly/validation_report.py")
PHASE52_FINAL = REPORTS / "rb_7ca33b7b5a81-final-verdicts.json"
PHASE52_FILES = ("var/monthly/reports/verdicts-framework-frozen.json", "var/monthly/reports/verdicts-framework-frozen-v2.json",
                 "var/monthly/reports/rb_7ca33b7b5a81-final-verdicts.md", "var/monthly/reports/rb_7ca33b7b5a81-final-verdicts.json",
                 "var/monthly/reports/rb_7ca33b7b5a81-variants.csv", "var/monthly/reports/rb_7ca33b7b5a81-configurations.csv",
                 "PHASE_5_2_RESEARCH_REPORT.md", "goldscalp/monthly/verdicts.py", "goldscalp/monthly/robustness.py",
                 "goldscalp/monthly/verdicts_report.py")


def hashes() -> dict[str, str]:
    return {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in FILES}


def _manifests() -> list[Path]:
    return sorted(REPORTS.glob(MANIFEST.stem + "*.json"), key=lambda q: (len(q.name), q.name))


def phase52_fingerprint(store: Path = STORE) -> dict[str, Any]:
    """What must stay untouched by this round: the Phase 5.2 files and every stored result of the source study."""
    con = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        n = 0
        for row in con.execute("SELECT config_key, timeframe, window, status, result_hash, metrics FROM robustness_results "
                               "WHERE study_id = ? ORDER BY config_key, timeframe, window", (vl.SOURCE_STUDY,)):
            h.update(json.dumps(list(row)).encode())
            n += 1
        cfg = hashlib.sha256()
        for row in con.execute("SELECT key, fingerprint FROM robustness_configs WHERE study_id = ? ORDER BY key",
                               (vl.SOURCE_STUDY,)):
            cfg.update(json.dumps(list(row)).encode())
        study = con.execute("SELECT status, analysis FROM robustness_studies WHERE id = ?", (vl.SOURCE_STUDY,)).fetchone()
    finally:
        con.close()
    return {"files": {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() if (ROOT / f).exists() else None
                      for f in PHASE52_FILES},
            "results": {"rows": n, "sha256": h.hexdigest()}, "configs_sha256": cfg.hexdigest(),
            "study": {"status": study[0] if study else None,
                      "analysis_sha256": hashlib.sha256((study[1] or "").encode()).hexdigest() if study else None}}


def phase52_check(frozen: dict[str, Any] | None, store: Path = STORE) -> dict[str, Any]:
    if not frozen:
        return {"checked": False}
    now = phase52_fingerprint(store)
    changed = [f for f, h in frozen["files"].items() if now["files"].get(f) != h]
    for k in ("results", "configs_sha256", "study"):
        if now[k] != frozen[k]:
            changed.append(k)
    return {"checked": True, "unchanged": not changed, "changed": changed, "result_rows": now["results"]["rows"]}


def series_candles(store: Path = STORE) -> int:
    con = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT id FROM series WHERE key = ?", (vl.SERIES[1],)).fetchone()
        return 0 if row is None else con.execute("SELECT COUNT(*) FROM candles WHERE series_id = ?", (row[0],)).fetchone()[0]
    finally:
        con.close()


def freeze(note: str) -> Path:
    existing = _manifests()
    target = MANIFEST if not existing else MANIFEST.with_name(f"{MANIFEST.stem}-v{len(existing) + 1}.json")
    if target.exists():
        raise SystemExit(f"already frozen: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "version": len(existing) + 1, "definition": vl.DEFINITION, "frozen_at": datetime.now(UTC).isoformat(),
        "note": note, "files": hashes(), "source_study": vl.SOURCE_STUDY, "candidates": vl.CANDIDATES,
        "fingerprints": vl.FINGERPRINTS, "period": [vl.PERIOD[0].isoformat(), vl.PERIOD[1].isoformat()],
        "series": list(vl.SERIES), "timeframes": list(vl.TIMEFRAMES), "min_months": vl.MIN_MONTHS, "share": vl.SHARE,
        "candles_in_validation_series_at_freeze": series_candles(), "code_version": runner.app_version(),
        "phase52_at_freeze": phase52_fingerprint(), "criteria": vl.__doc__}, indent=1), encoding="utf-8")
    return target


def check() -> dict[str, Any]:
    ms = _manifests()
    if not ms:
        return {"frozen": False, "unchanged": False, "note": "no validation protocol manifest"}
    m = json.loads(ms[-1].read_text(encoding="utf-8"))
    now = hashes()
    changed = [f for f, h in m["files"].items() if now.get(f) != h]
    return {"frozen": True, "unchanged": not changed, "changed_files": changed, "manifest": ms[-1].name,
            "frozen_at": m["frozen_at"], "candles_at_freeze": m["candles_in_validation_series_at_freeze"],
            "code_version_at_freeze": m["code_version"], "note": m["note"], "phase52_at_freeze": m.get("phase52_at_freeze")}


def _e(x: Any) -> str:
    return "–" if x is None else f"{x:+.3f}"


def _n(x: Any, d: int = 2) -> str:
    return "–" if x is None else f"{x:.{d}f}"


def write(vid: str, out_dir: Path = REPORTS, store: Path = STORE) -> dict[str, Path]:
    con = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    s = con.execute("SELECT * FROM validation_studies WHERE id = ?", (vid,)).fetchone()
    params, configs = json.loads(s["params"]), json.loads(s["configs"])
    a = json.loads(s["analysis"]) if s["analysis"] else None
    if a is None:
        raise SystemExit(f"{vid} is {s['status']}; nothing to report yet")
    datasets: dict[tuple[str, str], set] = {}
    status = {}
    for r in con.execute("SELECT config_key, timeframe, window, status, reason, dataset, metrics FROM validation_results "
                         "WHERE study_id = ?", (vid,)):
        status[(r["config_key"], r["timeframe"], r["window"])] = (r["status"], r["reason"], json.loads(r["metrics"]) if r["metrics"] else None)
        if r["dataset"]:
            d = json.loads(r["dataset"])
            datasets.setdefault((r["timeframe"], r["window"]), set()).add((d["dataset_hash"], d["execution_hash"]))
    p52 = json.loads(PHASE52_FINAL.read_text(encoding="utf-8"))["final"]["strategies"] if PHASE52_FINAL.exists() else {}
    src_version = con.execute("SELECT app_version FROM robustness_studies WHERE id = ?", (vl.SOURCE_STUDY,)).fetchone()
    fw = check()
    p52_ok = phase52_check(fw.get("phase52_at_freeze"), store) if fw.get("frozen") else {"checked": False}
    months = params["windows"]
    L = [f"<!-- generated by goldscalp.monthly.validation_report from {vid} ({params['definition']}) -->",
         "# GoldScalp AI: Phase 5.3 Validation Report", "",
         "**Frozen pre-June historical validation of the Phase 5.2 shortlist.** Simulation only.", "",
         "> Historical simulation on Vantage MT5 XAUUSD bid candles for January-May 2026: separate hypothetical $1,000 "
         "accounts per strategy, timeframe and month; no orders; no strategy changed. These results are reported "
         "**separately** and do not change any Phase 5.2 classification. The classes describe historical research "
         "candidates, **not trading readiness**. At least 30 trades is a minimum screen, not proof.", "",
         "## 1. Protocol", "",
         f"- Definition `{params['definition']}`; candidates locked from study `{params['source_study']}`: "
         + ", ".join(f"`{v}`" for v in params["candidates"].values()) + "; benchmarks: their originals, unchanged.",
         "- Months: " + ", ".join(f"{w['key']} {w['month']}" for w in months) + "; data: "
         f"`{params['series'][1]}` ({params['series'][0]} store); timeframes {', '.join(params['timeframes'])}.",
         "- Same engine code, costs, lots and execution model as Phase 5.2; **no selection** - nothing is chosen, tuned or "
         "replaced on January-May.",
         "- Criteria (Phase 5.2 thresholds; five-month rules made explicit): expectancy > 0; ≥ 30 trades; pooled PF ≥ 1.1 "
         "and PF > 1 in ≥ ceil(0.75 × months) (4 of 5); recovery ≥ 1; ≥ 4 of 5 months positive (a month with no trade "
         "counts as not positive) and positive without the best month; M15 > 0 as well as M5; still > 0 with spread + "
         "slippage ×2; at least 4 months with sufficient data.", "",
         "## 2. Results", "",
         "| Strategy | Version | Class | Trades | R/trade | PF | Sum R | Max DD R | Months + | M15 R/trade | ×2 | ×3 |",
         "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for strat, v in a["strategies"].items():
        for label in ("candidate", "original"):
            x = v[label]
            pp = x["pooled"]
            mpos = sum(1 for m in x["months"].values() if m.get("status") == "COMPLETED" and m["trades"] and m["sum_r"] > 0)
            mval = sum(1 for m in x["months"].values() if m.get("status") == "COMPLETED")
            L.append(f"| {strat} | {label} `{x['key']}` | **{x['class']}** | {pp['trades']} | {_e(pp['expectancy_r'])} | "
                     f"{_n(pp['profit_factor'])} | {_n(pp['sum_r'])} | {_n(pp['max_dd_r'])} | {mpos}/{mval} | "
                     f"{_e(x['m15']['expectancy_r'])} | {_e(pp['expectancy_r_costs_x2'])} | {_e(pp['expectancy_r_costs_x3'])} |")
    L += ["", "## 3. Month by month (M5: sum of R, trades)", "",
          "| Configuration | " + " | ".join(f"{w['key']} {w['month']}" for w in months) + " |",
          "|---|" + "---:|" * len(months)]
    for v in a["strategies"].values():
        for label in ("candidate", "original"):
            x = v[label]
            L.append(f"| `{x['key']}` | " + " | ".join(
                (f"{_n(x['months'][w['key']]['sum_r'])} ({x['months'][w['key']]['trades']})"
                 if x["months"][w["key"]].get("status") == "COMPLETED" else x["months"][w["key"]].get("status", "-"))
                for w in months) + " |")
    L += ["", "## 4. Timeframes (M15 month by month: sum of R, trades)", "",
          "| Configuration | M5 R/trade | M15 R/trade | M15 trades | " + " | ".join(f"{w['key']}" for w in months) + " |",
          "|---|---:|---:|---:|" + "---:|" * len(months)]
    for v in a["strategies"].values():
        for label in ("candidate", "original"):
            x = v[label]
            mm = x.get("m15_months") or {}
            L.append(f"| `{x['key']}` | {_e(x['pooled']['expectancy_r'])} | {_e(x['m15']['expectancy_r'])} | "
                     f"{x['m15']['trades']} | " + " | ".join(
                         (f"{_n(mm[w['key']]['sum_r'])} ({mm[w['key']]['trades']})" if w["key"] in mm else "INCOMPLETE")
                         for w in months) + " |")
    L += ["", "## 5. Cost stress (spread + slippage multiplied; commission unchanged)", "",
          "Exact for the cost component; second-order effects of wider spreads on stop and target triggers are not "
          "modelled. x2 is a criterion; x3 is reported only.", "",
          "| Configuration | M5 base | M5 ×2 | M5 ×3 | M15 base | M15 ×2 | M15 ×3 | Months + base | ×2 | ×3 |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for v in a["strategies"].values():
        for label in ("candidate", "original"):
            x = v[label]
            pp, mq = x["pooled"], x["m15"]
            done = [m for m in x["months"].values() if m.get("status") == "COMPLETED"]
            pos = [sum(1 for m in done if m["trades"] and (m[f] or 0) > 0)
                   for f in ("sum_r", "sum_r_costs_x2", "sum_r_costs_x3")]
            L.append(f"| `{x['key']}` | {_e(pp['expectancy_r'])} | {_e(pp['expectancy_r_costs_x2'])} | "
                     f"{_e(pp['expectancy_r_costs_x3'])} | {_e(mq['expectancy_r'])} | {_e(mq['expectancy_r_costs_x2'])} | "
                     f"{_e(mq['expectancy_r_costs_x3'])} | " + " | ".join(f"{c}/{len(done)}" for c in pos) + " |")
    L += ["", "## 6. Criteria", ""]
    for strat, v in a["strategies"].items():
        L += [f"### {strat}", "", f"- **Candidate `{v['candidate']['key']}`: {v['candidate']['class']}** — {v['candidate']['why']}.",
              f"- **Original: {v['original']['class']}** — {v['original']['why']}.", "",
              "| Criterion | Candidate | | Original | |", "|---|---|---|---|---|"]
        for c in v["candidate"]["criteria"]:
            cc, oc = v["candidate"]["criteria"][c], v["original"]["criteria"][c]
            L.append(f"| {c} | {'pass' if cc['pass'] else '**fail**'} | {cc['detail']} | {'pass' if oc['pass'] else '**fail**'} | "
                     f"{oc['detail']} |")
        L.append("")
    L += ["## 7. The questions asked of each candidate", ""]
    q = {"R2": "Does it remain positive despite its negative October in Phase 5.2?",
         "R7": "Does its unusually high expectancy survive a much larger sample?",
         "R1": "Does its small positive expectancy persist across another period?",
         "R5": "Does its small positive expectancy persist across another period?"}
    for strat, v in a["strategies"].items():
        x = v["candidate"]
        prev = (p52.get(strat) or {}).get("redesign") or {}
        oct_ = ((prev.get("secondary_october") or {}).get("M5") or {})
        L.append(f"- **{strat}: {q.get(strat, '')}** January-May: {x['pooled']['trades']} trades, "
                 f"{_e(x['pooled']['expectancy_r'])} R/trade (PF {_n(x['pooled']['profit_factor'])}); Phase 5.2 out of sample "
                 f"{(prev.get('oos') or {}).get('trades', '–')} trades, {_e((prev.get('oos') or {}).get('expectancy_r'))} R/trade"
                 + (f"; Phase 5.2 October (secondary) {_e(oct_.get('expectancy_r'))} R/trade over {oct_.get('trades')} trades"
                    if strat == "R2" else "") + f". Class: **{x['class']}**.")
    L += ["", "## 8. Integrity", "",
          "- **Protocol frozen before the data:** " + (
              f"manifest {fw['manifest']} of {fw['frozen_at']} ({fw['candles_at_freeze']} candles in the validation "
              f"series at freeze); protocol code " + ("**unchanged**" if fw["unchanged"] else f"**CHANGED** {fw['changed_files']}")
              if fw.get("frozen") else "**no manifest**") + ".",
          f"- **Same code as Phase 5.2:** this run {s['app_version']}; Phase 5.2 study "
          f"{src_version[0] if src_version else '–'} ({'identical' if src_version and src_version[0] == s['app_version'] else '**different**'}).",
          f"- **Locked rules:** the {len(configs)} configurations ran with fingerprints "
          + ", ".join(f"`{k}` {params['configs'][k]['fingerprint'][:16]}" for k in params["configs"]) + "; re-verified at run time.",
          "- **No selection:** only these configurations exist in this study; nothing was chosen on January-May.",
          "- **Fair comparison:** one dataset per timeframe and month for all configurations: "
          + "; ".join(f"{tf} {w} {len(v)} dataset" for (tf, w), v in sorted(datasets.items())) + ".",
          "- **Phase 5.2 untouched:** " + (
              (f"verified against the fingerprint recorded at freeze: {p52_ok['result_rows']} stored results of "
               f"{vl.SOURCE_STUDY}, its configurations and analysis, its report files, the verdicts@2 manifests and the "
               "frozen framework code are **unchanged**") if p52_ok.get("checked") and p52_ok["unchanged"] else
              (f"**CHANGED since freeze: {p52_ok['changed']}**" if p52_ok.get("checked") else "not verifiable (no fingerprint)"))
          + ". Phase 5.2 classifications are not re-evaluated here."]
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"markdown": out_dir / f"{vid}-validation.md", "json": out_dir / f"{vid}-validation.json",
             "csv": out_dir / f"{vid}-validation.csv"}
    paths["markdown"].write_text("\n".join(L) + "\n", encoding="utf-8")
    paths["json"].write_text(json.dumps({"study": vid, "params": params, "analysis": a, "protocol": fw,
                                         "phase52_check": p52_ok,
                                         "datasets": {f"{tf} {w}": len(v) for (tf, w), v in datasets.items()}},
                                        indent=1, default=str), encoding="utf-8")
    with paths["csv"].open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["# SIMULATED historical validation - separate $1,000 accounts; not trading advice", vid])
        cols = ["trades", "wins", "losses", "win_rate", "net_usd", "sum_r", "expectancy_r", "profit_factor", "max_dd_r",
                "costs_usd", "cost_r"]
        w.writerow(["config", "timeframe", "month", "status", *cols, "reason"])
        for (k, tf, win), (st, reason, m) in sorted(status.items()):
            month = next(x["month"] for x in months if x["key"] == win)
            w.writerow([k, tf, month, st, *[(m or {}).get(c) for c in cols], reason])
    return paths


if __name__ == "__main__":
    if sys.argv[1] == "--freeze":
        print(freeze(" ".join(sys.argv[2:]) or "frozen before the January-May data was imported"))
    else:
        print({k: str(v) for k, v in write(sys.argv[1], Path(sys.argv[2]) if len(sys.argv) > 2 else REPORTS).items()})
