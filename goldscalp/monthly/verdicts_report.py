"""Phase 5.2: render the final classification (verdicts@1) and its integrity checks as Markdown, and write the report
files. Read-only on the research store; the study's own stored results and analysis are never modified.

    python -m goldscalp.monthly.verdicts_report <study_id> [out_dir]"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from goldscalp.monthly import robustness as rb
from goldscalp.monthly import verdicts as vd

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "var" / "monthly" / "monthly.sqlite3"
ORDER = ["A", "B", "C", "R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9"]
#: the evaluation framework: the criteria and classes, the selection rules and this renderer
FRAMEWORK_FILES = ("goldscalp/monthly/verdicts.py", "goldscalp/monthly/robustness.py", "goldscalp/monthly/verdicts_report.py")
FREEZE = STORE.parent / "reports" / "verdicts-framework-frozen.json"     # v1; amendments: -v2, -v3 ... (never overwritten)


def _freezes() -> list[Path]:
    return sorted(FREEZE.parent.glob(FREEZE.stem + "*.json"), key=lambda q: (len(q.name), q.name))


def framework_hashes() -> dict[str, str]:
    return {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in FRAMEWORK_FILES}


def freeze(note: str) -> Path:
    """Record the framework (file hashes and thresholds) BEFORE the results are seen. Refuses to overwrite."""
    existing = _freezes()
    target = FREEZE if not existing else FREEZE.with_name(f"{FREEZE.stem}-v{len(existing) + 1}.json")
    if target.exists():
        raise SystemExit(f"already frozen: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "version": len(existing) + 1, "definition": vd.DEFINITION, "previous": [q.name for q in existing],
        "frozen_at": datetime.now(UTC).isoformat(), "note": note, "files": framework_hashes(),
        "thresholds": {"min_trades": vd.MIN_TRADES, "overfit_development_r": vd.OVERFIT_DEV_R,
                       "profit_factor_pooled": 1.1, "profit_factor_months": "> 1 in at least 75 % of OOS months",
                       "recovery_factor": 1.0, "consistency": "at least 75 % of OOS months positive and positive without "
                                                               "the best month",
                       "timeframes": "OOS > 0 on M15 as well as M5", "execution": "OOS > 0 with spread + slippage x2",
                       "selection_min_development_trades": rb.MIN_DEV_TRADES,
                       "walk_forward_min_trades_per_window": rb.MIN_TRADES_PER_WINDOW, "selection_timeframe": rb.SELECTION_TF},
        "secondary_months": list(vd.SECONDARY_MONTHS),
        "criteria": vd.__doc__}, indent=1), encoding="utf-8")
    return target


def framework_check() -> dict[str, object]:
    freezes = _freezes()
    if not freezes:
        return {"frozen": False, "note": "no freeze manifest"}
    m = json.loads(freezes[-1].read_text(encoding="utf-8"))
    now = framework_hashes()
    changed = [f for f, h in m["files"].items() if now.get(f) != h]
    return {"frozen": True, "frozen_at": m["frozen_at"], "unchanged": not changed, "changed_files": changed,
            "manifest": freezes[-1].name, "version": m.get("version", 1), "note": m.get("note"),
            "history": [{"manifest": q.name, "frozen_at": json.loads(q.read_text(encoding="utf-8"))["frozen_at"],
                         "note": json.loads(q.read_text(encoding="utf-8"))["note"]} for q in freezes]}


def _e(x: Any) -> str:
    return "–" if x is None else f"{x:+.3f}"


def _n(x: Any, d: int = 2) -> str:
    return "–" if x is None else f"{x:.{d}f}"


def load(store: Path, sid: str) -> tuple[dict, dict, dict, sqlite3.Connection]:
    con = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT params, status FROM robustness_studies WHERE id = ?", (sid,)).fetchone()
    params = json.loads(row["params"])
    results = {}
    for r in con.execute("SELECT config_key, timeframe, window, status, reason, trades FROM robustness_results WHERE "
                         "study_id = ?", (sid,)):
        results[(r["config_key"], r["timeframe"], r["window"])] = {
            "status": r["status"], "reason": r["reason"], "metrics": None, "trades": json.loads(r["trades"]) if r["trades"] else []}
    cfgs = {r["key"]: {"fingerprint": r["fingerprint"], "config": json.loads(r["config"])}
            for r in con.execute("SELECT key, config, fingerprint FROM robustness_configs WHERE study_id = ?", (sid,))}
    return params, results, cfgs, con


def integrity(con: sqlite3.Connection, sid: str, params: dict, results: dict, final: dict) -> dict[str, Any]:
    """Fair comparison (one dataset per timeframe x window), no hold-out leakage (choices recomputed without any
    hold-out row are identical), every variant preserved."""
    datasets: dict[tuple[str, str], set] = {}
    counts: dict[tuple[str, str], int] = {}
    for r in con.execute("SELECT timeframe, window, dataset FROM robustness_results WHERE study_id = ? AND status = "
                         "'COMPLETED'", (sid,)):
        d = json.loads(r["dataset"])
        datasets.setdefault((r["timeframe"], r["window"]), set()).add((d["dataset_hash"], d["execution_hash"]))
        counts[(r["timeframe"], r["window"])] = counts.get((r["timeframe"], r["window"]), 0) + 1
    hold = {w["key"] for w in params["windows"] if w["role"] == "hold-out"}
    no_hold = {k: v for k, v in results.items() if k[2] not in hold}
    again = vd.final(no_hold, params)
    same_choice = {k: (final["strategies"][k]["redesign"]["development_choice"] ==
                       again["strategies"][k]["redesign"]["development_choice"]) for k in final["strategies"]}
    statuses = {}
    for r in con.execute("SELECT status, COUNT(*) n FROM robustness_results WHERE study_id = ? GROUP BY status", (sid,)):
        statuses[r["status"]] = r["n"]
    return {"datasets": {f"{tf} {w}": {"configurations": counts[(tf, w)], "distinct_datasets": len(s)}
                         for (tf, w), s in sorted(datasets.items())},
            "choices_identical_without_hold_out": same_choice, "result_rows": statuses,
            "configurations": len(params["configs"])}


def markdown(sid: str, params: dict, final: dict, cfgs: dict, integ: dict, overlay: dict | None) -> str:
    L: list[str] = [f"<!-- generated by goldscalp.monthly.verdicts_report from study {sid} (definition {final['definition']}) -->",
                    "## 4. Final classification", "",
                    "Pre-declared criteria (`goldscalp/monthly/verdicts.py`, definition " + final["definition"] + ", fixed "
                    "before any October / November result existed). The out-of-sample (OOS) months for the verdict are "
                    + ", ".join(final["oos_months"]) + " (each chosen on development months only); the primary hold-out is "
                    + ", ".join(final["hold_out"]) + " (November 1-13). **October (" + ", ".join(final["secondary"]) + ") is a "
                    "secondary evaluation:** its historical results influenced the redesign hypotheses, so it never decides a "
                    "class. Win rate is shown but is not a criterion. **At least 30 trades is a minimum screen, not proof of "
                    "statistical reliability.** These classes describe historical research candidates, **not real-world "
                    "trading readiness**.", "",
                    "| Strategy | Original | Redesign | Redesign chosen on development | OOS trades | OOS R/trade | OOS PF | "
                    "November R/trade (trades) | Months + | M15 OOS R/trade | Fills ×2 R/trade | October, secondary (M5) |",
                    "|---|---|---|---|---:|---:|---:|---|---:|---:|---:|---|"]
    for k in [x for x in ORDER if x in final["strategies"]]:
        s = final["strategies"][k]
        o, r = s["original"], s["redesign"]
        months_pos = sum(1 for m in r["months"].values() if (m["sum_r"] or 0) > 0)
        m15 = r["criteria"]["timeframes"]["detail"].split("M15 ")[1].split(" R/trade")[0] if "M15 " in r["criteria"]["timeframes"]["detail"] else "–"
        L.append(f"| {k} → {s['version_name']} | **{o['class']}** | **{r['class']}** | `{r['development_choice'] or '–'}` | "
                 f"{r['oos']['trades']} | {_e(r['oos']['expectancy_r'])} | {_n(r['oos']['profit_factor'])} | "
                 f"{_e(r['hold_out']['expectancy_r'])} ({r['hold_out']['trades']}) | {months_pos}/{len(r['months'])} | {m15} | "
                 f"{_e(r['oos']['expectancy_r_costs_x2'])} | "
                 f"{_e((r.get('secondary_october') or {}).get('M5', {}).get('expectancy_r'))} "
                 f"({(r.get('secondary_october') or {}).get('M5', {}).get('trades', '–')}) |")
    L += ["", "## 5. Per strategy", ""]
    for k in [x for x in ORDER if x in final["strategies"]]:
        s = final["strategies"][k]
        L.append(f"### {k} → {s['version_name']} ({s['family']})\n")
        L.append(f"- **Original: {s['original']['class']}** — {s['original']['why']}.")
        L.append(f"- **Redesign: {s['redesign']['class']}** — {s['redesign']['why']}.")
        for label in ("original", "redesign"):
            so = s[label].get("secondary_october") or {}
            if so:
                L.append(f"- October (secondary, not part of the verdict), {label}: " + "; ".join(
                    f"{t} {_e(so[t]['expectancy_r'])} R/trade ({so[t]['trades']} trades)" for t in ("M5", "M15", "M1")) + ".")
        if s["redesign"]["flags"] or s["original"]["flags"]:
            L.append(f"- Flags: {'; '.join(s['redesign']['flags'] + ['original: ' + f for f in s['original']['flags']])}.")
        L += ["", "| Criterion | Original | | Redesign | |", "|---|---|---|---|---|"]
        for c in ("expectancy", "trade_count", "profit_factor", "drawdown", "consistency", "timeframes", "execution"):
            oc, rc = s["original"]["criteria"][c], s["redesign"]["criteria"][c]
            L.append(f"| {c} | {'pass' if oc['pass'] else '**fail**'} | {oc['detail']} | {'pass' if rc['pass'] else '**fail**'} | "
                     f"{rc['detail']} |")
        L += ["", "Every month on M5 (sum of R; trades) — development W1-W4, October W5 (secondary), November W6 (hold-out):", "",
              "| Version | " + " | ".join(w["key"] + (" (dev)" if w["role"] == "development" else
                                                    " (secondary)" if w["key"] in final["secondary"] else " (hold-out)")
                                         for w in params["windows"]) + " |",
              "|---|" + "---:|" * len(params["windows"])]
        for label in ("original", "redesign"):
            am = s[label]["all_months_m5"]
            L.append(f"| {label} `{s[label]['development_choice'] or '–'}` | "
                     + " | ".join(f"{_n(am[w['key']]['sum_r'])} ({am[w['key']]['trades']})" for w in params["windows"]) + " |")
        ch = s["redesign"]["chosen_per_month"]
        vdist = s["variant_distribution"]
        L += ["", "- Variant chosen for each OOS month (on development months before it): "
              + "; ".join(f"{m}: `{c or '–'}`" for m, c in ch.items()) + ".",
              f"- All {s['variants_tried']} variants: on development {vdist['development_positive']} of "
              f"{vdist['development_with_30_trades']} (≥ 30 trades) positive, median {_e(vdist['development_median_r'])} R; "
              f"on the hold-out {vdist['hold_out_positive']} of {vdist['hold_out_with_10_trades']} (≥ 10 trades) positive, "
              f"median {_e(vdist['hold_out_median_r'])} R; the development choice ranks "
              f"{vdist['chosen_hold_out_rank'] or '–'} of {vdist['hold_out_with_10_trades']} on the hold-out.", ""]
    L += r6_section(final, params)
    L += ["## 5b. Did the filters and exits help? Every strategy's full variant distribution (M5)", "",
          "Medians over all variants in each group (≥ 10 trades), development (W1-W4) and November (W6; October is "
          "secondary and excluded here). "
          "Every variant's own numbers are in the `-variants.csv` file.", ""]
    for k in [x for x in ORDER if x in final["strategies"]]:
        s = final["strategies"][k]
        L += [f"**{k}** ({len(s['variants'])} variants)", "",
              "| Group | Variants | Development median R (positive/n) | Hold-out median R (positive/n) |", "|---|---:|---|---|"]
        for by, label in (("by_entry", "entry"), ("by_exit", "exit"), ("by_overlay", "overlay")):
            for g in s[by]:
                L.append(f"| {label} {g[label]}: {g['label'].split(';')[0 if label == 'entry' else 1 if label == 'exit' else -1].strip()[:70]} | "
                         f"{g['variants']} | {_e(g['development_median_r'])} ({g['development_positive']}/{g['development_n']}) | "
                         f"{_e(g['hold_out_median_r'])} ({g['hold_out_positive']}/{g['hold_out_n']}) |")
        L.append("")
    if overlay:
        L += ["## 6. Break-even and trailing stops: paired effect on every redesign variant (M5)", "",
              "| Overlay | Development: improved (pairs) | median ΔR | median Δ win rate | Hold-out: improved (pairs) | "
              "median ΔR | median Δ win rate |", "|---|---|---:|---:|---|---:|---:|"]
        for x in overlay.values():
            d, h = x["development"], x["hold_out"]
            L.append(f"| {x['label']} | {d['improved']} of {d['pairs']} | {_e(d['median_delta_r'])} | "
                     f"{_n((d['median_delta_win_rate'] or 0) * 100, 1)} pts | {h['improved']} of {h['pairs']} | "
                     f"{_e(h['median_delta_r'])} | {_n((h['median_delta_win_rate'] or 0) * 100, 1)} pts |")
        L.append("")
    fw = integ.get("framework") or {}
    L += ["## 7. Integrity checks", "",
          "- **Framework frozen before the results:** " + (
              f"manifest {fw.get('manifest')} (version {fw.get('version')}) of {fw.get('frozen_at')}; history: "
              + "; ".join(f"{h['manifest']} {h['frozen_at']}: {h['note']}" for h in fw.get("history", [])) + ". Criteria, "
              "selection and report code "
              + ("**unchanged since the freeze**" if fw.get("unchanged") else f"**CHANGED**: {fw.get('changed_files')}")
              if fw.get("frozen") else "no freeze manifest") + ".",
          f"- **Every variant preserved:** {integ['configurations']} configurations with their exact rules and fingerprints; "
          f"result rows by status: {integ['result_rows']}. The cancelled studies and the diagnostic study are kept.",
          "- **Fair comparison:** one dataset per timeframe and month for every configuration that ran on it: "
          + "; ".join(f"{k} {v['configurations']} configurations / {v['distinct_datasets']} dataset"
                      for k, v in integ["datasets"].items()) + ".",
          "- **No hold-out leakage:** every development choice recomputed with all hold-out rows removed is identical: "
          + ", ".join(f"{k} {'yes' if v else '**NO**'}" for k, v in integ["choices_identical_without_hold_out"].items()) + ".",
          "", "**Fingerprints** of the originals and the development choices:", "",
          "| Strategy | Original | Fingerprint | Development choice | Fingerprint |", "|---|---|---|---|---|"]
    for k in [x for x in ORDER if x in final["strategies"]]:
        s = final["strategies"][k]
        c = s["redesign"]["development_choice"]
        L.append(f"| {k} | `{k}|original` | `{cfgs[f'{k}|original']['fingerprint'][:16]}` | `{c or '–'}` | "
                 f"`{cfgs[c]['fingerprint'][:16] if c else '–'}` |")
    return "\n".join(L) + "\n"


def r6_section(final: dict, params: dict) -> list[str]:
    """The user's R6 questions: improvement, the October repeat, filters, exits, execution robustness."""
    if "R6" not in final["strategies"]:
        return []
    s = final["strategies"]["R6"]
    o, r = s["original"], s["redesign"]
    am = o["all_months_m5"]
    rows = sorted(s["variants"], key=lambda v: (v["entry"], v["exit"], v["overlay"]))
    L = ["## 5a. Original R6 vs redesigned R6", "",
         "| Question | Evidence |", "|---|---|",
         f"| Did R6 v2 improve? | Development (W1-W4, M5): original {_e(o['development']['expectancy_r'])} R/trade "
         f"({o['development']['trades']} trades) vs v2 {_e(r['development']['expectancy_r'])} ({r['development']['trades']}). "
         f"Out of sample: original {_e(o['oos']['expectancy_r'])} ({o['oos']['trades']}) vs v2 {_e(r['oos']['expectancy_r'])} "
         f"({r['oos']['trades']}). Classes: original **{o['class']}**, v2 **{r['class']}**. |",
         f"| Did October profitability repeat? | Original R6 on M5: October (W5) {_n(am['W5']['sum_r'])} R over "
         f"{am['W5']['trades']} trades (${_n(am['W5']['net_usd'])}); November 1-13 (W6) {_n(am['W6']['sum_r'])} R over "
         f"{am['W6']['trades']} trades (${_n(am['W6']['net_usd'])}). Every month: "
         + ", ".join(f"{w['key']} {_n(am[w['key']]['sum_r'])} R" for w in params["windows"]) + ". |",
         "| Did filters help? | Entry groups (median hold-out R): "
         + "; ".join(f"{g['entry']} {_e(g['hold_out_median_r'])} ({g['hold_out_positive']}/{g['hold_out_n']} positive)"
                     for g in s["by_entry"]) + " — all 48 variants below. |",
         "| Which exit performed best? | Exit groups: " + "; ".join(
             f"{g['exit']} {_e(g['hold_out_median_r'])}" for g in s["by_exit"]) + ". Overlays: " + "; ".join(
             f"{g['overlay']} {_e(g['hold_out_median_r'])} (development {_e(g['development_median_r'])})" for g in s["by_overlay"])
         + " — paired table below. |",
         f"| Is the result robust? | v2 out of sample: {_e(r['oos']['expectancy_r'])} R/trade; spread + slippage x2 "
         f"{_e(r['oos']['expectancy_r_costs_x2'])}; x3 {_e(r['oos']['expectancy_r_costs_x3'])}. Original: "
         f"{_e(o['oos']['expectancy_r'])} / {_e(o['oos']['expectancy_r_costs_x2'])} / {_e(o['oos']['expectancy_r_costs_x3'])}. |",
         "", "**All 48 R6 variants** (M5; development W1-W4 and November W6 are R/trade with trades; costs x2 / x3 on "
         "November; October is secondary; then the sum of R in every month):", "",
         "| Variant | Development | November | November costs x2 / x3 | October (secondary) | "
         + " | ".join(w["key"] for w in params["windows"]) + " |",
         "|---|---|---|---|---|" + "---:|" * len(params["windows"])]
    for v in rows:
        d, h = v["development"], v["hold_out"]
        mark = " **(chosen)**" if v["key"] == r["development_choice"] else ""
        so = v.get("secondary_october") or {}
        L.append(f"| `{v['key']}`{mark} | {_e(d['expectancy_r'])} ({d['trades']}) | {_e(h['expectancy_r'])} ({h['trades']}) | "
                 f"{_e(h['expectancy_r_costs_x2'])} / {_e(h['expectancy_r_costs_x3'])} | "
                 f"{_e(so.get('expectancy_r'))} ({so.get('trades', '–')}) | "
                 + " | ".join(_n(v["months"][w["key"]]) for w in params["windows"]) + " |")
    L += ["", "**R6 paired exits** (November R/trade; the same entry and exit, with nothing / break-even / trailing):", "",
          "| Entry | Exit | plain | + break-even | + trailing |", "|---|---|---:|---:|---:|"]
    for x in s["paired_overlays"]:
        L.append(f"| {x['entry']} | {x['exit']} | {_e(x['none']['hold_out_r'])} | {_e(x['be']['hold_out_r'])} | "
                 f"{_e(x['tr']['hold_out_r'])} |")
    L.append("")
    return L


def write(sid: str, out_dir: Path, store: Path = STORE) -> dict[str, Path]:
    params, results, cfgs, con = load(store, sid)
    final = vd.final(results, params)
    integ = integrity(con, sid, params, results, final)
    analysis_row = con.execute("SELECT analysis FROM robustness_studies WHERE id = ?", (sid,)).fetchone()["analysis"]
    overlay = (json.loads(analysis_row) or {}).get("overlay_effect") if analysis_row else rb.overlay_effect(results, params)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"json": out_dir / f"{sid}-final-verdicts.json", "markdown": out_dir / f"{sid}-final-verdicts.md",
             "configs": out_dir / f"{sid}-configurations.csv", "variants": out_dir / f"{sid}-variants.csv"}
    check = framework_check()
    integ["framework"] = check
    paths["json"].write_text(json.dumps({"study": sid, "final": final, "integrity": integ, "overlay_effect": overlay},
                                        indent=1, default=str), encoding="utf-8")
    paths["markdown"].write_text(markdown(sid, params, final, cfgs, integ, overlay), encoding="utf-8")
    with paths["variants"].open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        wins = [x["key"] for x in params["windows"]]
        w.writerow(["# every redesign variant, failures included (M5, R after costs per trade)", sid])
        w.writerow(["strategy", "key", "entry", "exit", "overlay", "chosen_on_development", "dev_trades", "dev_r", "dev_pf",
                    "november_trades", "november_r", "november_pf", "november_r_costs_x2", "november_r_costs_x3",
                    "october_secondary_trades", "october_secondary_r", *[f"{k}_sum_r" for k in wins], "label"])
        for k, st in final["strategies"].items():
            for v in st["variants"]:
                d, h = v["development"], v["hold_out"]
                w.writerow([k, v["key"], v["entry"], v["exit"], v["overlay"], v["key"] == st["redesign"]["development_choice"],
                            d["trades"], d["expectancy_r"], d["profit_factor"], h["trades"], h["expectancy_r"],
                            h["profit_factor"], h["expectancy_r_costs_x2"], h["expectancy_r_costs_x3"],
                            (v.get("secondary_october") or {}).get("trades"), (v.get("secondary_october") or {}).get("expectancy_r"),
                            *[v["months"][x] for x in wins], v["label"]])
    with paths["configs"].open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["key", "strategy", "role", "entry", "exit", "overlay", "fingerprint", "label"])
        for c in params["configs"]:
            w.writerow([c["key"], c["strategy"], c["role"], c.get("entry"), c.get("exit"), c.get("overlay"),
                        cfgs[c["key"]]["fingerprint"], c["label"]])
    return paths


if __name__ == "__main__":
    if sys.argv[1] == "--freeze":
        print(freeze(" ".join(sys.argv[2:]) or "frozen before the final results"))
    else:
        out = write(sys.argv[1], Path(sys.argv[2]) if len(sys.argv) > 2 else STORE.parent / "reports")
        print({k: str(v) for k, v in out.items()})
