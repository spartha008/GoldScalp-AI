"""Phase 6: freeze the discovery protocol (hypotheses, rules, predictions, criteria) and report development trials.

    python -m goldscalp.monthly.discovery_report --freeze "<note>"
    python -m goldscalp.monthly.discovery_report --check
    python -m goldscalp.monthly.discovery_report [out_dir]          # development report of every trial so far

Freezing records SHA-256 hashes of the specification, the protocol document and the code, before any trial; trials
are refused while the protocol is unfrozen or changed. Versioned manifests are never overwritten."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from goldscalp.monthly import discovery as dv
from goldscalp.monthly import runner

ROOT = dv.ROOT
STORE = ROOT / "var" / "monthly" / "monthly.sqlite3"
REPORTS = STORE.parent / "reports"
MANIFEST = REPORTS / "discovery-protocol-frozen.json"
FILES = ("config/discovery/phase6.json", "PHASE_6_PROTOCOL.md", "goldscalp/monthly/discovery.py",
         "goldscalp/monthly/discovery_report.py")


def hashes() -> dict[str, str | None]:
    return {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() if (ROOT / f).exists() else None for f in FILES}


def _manifests() -> list[Path]:
    return sorted(REPORTS.glob(MANIFEST.stem + "*.json"), key=lambda q: (len(q.name), q.name))


def trial_count(store: Path | None = None) -> int:
    path = store or STORE
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        has = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovery_trials'").fetchone()
        return 0 if not has else con.execute("SELECT COUNT(*) FROM discovery_trials").fetchone()[0]
    finally:
        con.close()


def freeze(note: str) -> Path:
    existing = _manifests()
    target = MANIFEST if not existing else MANIFEST.with_name(f"{MANIFEST.stem}-v{len(existing) + 1}.json")
    if target.exists():
        raise SystemExit(f"already frozen: {target}")
    spec = dv.load_spec()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "version": len(existing) + 1, "definition": dv.DEFINITION, "frozen_at": datetime.now(UTC).isoformat(), "note": note,
        "files": hashes(), "trials_at_freeze": trial_count(), "code_version": runner.app_version(),
        "hypotheses": [{k: h[k] for k in ("id", "title", "question", "hypothesis", "baselines", "prediction")}
                       for h in spec["hypotheses"]],
        "fingerprints": {k: dv.builder.fingerprint(c) for k, c in spec["configs"].items()},
        "development_windows": dv.dev_windows(),
        "sealed": [dv.SEALED[0].isoformat(), dv.SEALED[1].isoformat()],
        "sealed_evaluation": [dv.SEALED_EVAL[0].isoformat(), dv.SEALED_EVAL[1].isoformat()],
        "forward_from": dv.FORWARD_FROM.isoformat(), "max_trials": dv.MAX_TRIALS,
        "sealed_criteria": spec["sealed_validation"]["criteria"]}, indent=1), encoding="utf-8")
    return target


def check() -> dict[str, Any]:
    ms = _manifests()
    if not ms:
        return {"frozen": False, "unchanged": False, "note": "no discovery protocol manifest"}
    m = json.loads(ms[-1].read_text(encoding="utf-8"))
    now = hashes()
    changed = [f for f, h in m["files"].items() if now.get(f) != h]
    return {"frozen": True, "unchanged": not changed, "changed_files": changed, "manifest": ms[-1].name,
            "frozen_at": m["frozen_at"], "trials_at_freeze": m["trials_at_freeze"], "version": m["version"]}


def _r(x: Any) -> str:
    return "–" if x is None else f"{x:+.3f}"


def _n(x: Any, d: int = 2) -> str:
    return "–" if x is None else f"{x:.{d}f}"


def write(out_dir: Path = REPORTS, store: Path = STORE) -> dict[str, Path]:
    """The development report: every trial of every hypothesis, in order, failures included."""
    con = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    trials = con.execute("SELECT * FROM discovery_trials ORDER BY hypothesis, created_at").fetchall()
    spec = dv.load_spec()
    fw = check()
    L = ["# GoldScalp AI: Phase 6 development report", "",
         "> Historical simulation on development data only (January to 13 November 2026). Separate hypothetical $1,000 "
         "accounts per configuration and month; no orders. Nothing here is validated: the sealed block (November 2023 "
         "to October 2025) and forward data are untouched by every trial below.", "",
         f"Protocol: {fw.get('manifest', 'not frozen')}" + (f" (frozen {fw['frozen_at']}, "
                                                            f"{'unchanged' if fw['unchanged'] else 'CHANGED'})" if fw.get("frozen") else ""), ""]
    for h in spec["hypotheses"]:
        ts = [t for t in trials if t["hypothesis"] == h["id"]]
        L += [f"## {h['id']} {h['title']}", "", f"**Question.** {h['question']}", "", f"**Hypothesis.** {h['hypothesis']}", "",
              f"Trials used: {len(ts)} of {dv.MAX_TRIALS}.", ""]
        for t in ts:
            p = json.loads(t["params"])
            a = json.loads(t["analysis"]) if t["analysis"] else None
            L += [f"### Trial {p['trial_number']}: {t['kind']} `{t['id']}` ({t['status']})", ""]
            if p.get("rationale"):
                L += [f"Rationale (written before running): {p['rationale']}", ""]
            if not a:
                L += [f"No analysis yet{': ' + t['error'] if t['error'] else ''}.", ""]
                continue
            L += ["| Configuration | Trades | R/trade | PF | Sum R | Max DD R | x2 costs | Months valid |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
            for k, c in a["configs"].items():
                s = c["pooled"]
                L.append(f"| `{k}` | {s['trades']} | {_r(s['expectancy_r'])} | {_n(s['profit_factor'])} | {_n(s['sum_r'])} | "
                         f"{_n(s['max_dd_r'])} | {_r(s['expectancy_r_costs_x2'])} | {c['valid_months']} |")
            for field, title in (("by_vol_m15", "M15 volatility regime"), ("by_session", "session"), ("by_h1", "H1 structure")):
                L += ["", f"By {title} (trades, R/trade):", "", "| Configuration | " + " | ".join(
                    sorted({g for c in a["configs"].values() for g in c[field]})) + " |"]
                groups = sorted({g for c in a["configs"].values() for g in c[field]})
                L.append("|---|" + "---:|" * len(groups))
                for k, c in a["configs"].items():
                    L.append(f"| `{k}` | " + " | ".join(
                        f"{c[field][g]['trades']}, {_r(c[field][g]['expectancy_r'])}" if g in c[field] else "–" for g in groups) + " |")
            if a.get("prediction"):
                pr = a["prediction"]
                L += ["", f"**Prediction ({'holds' if pr['holds'] else 'does not hold'} on development data).** "
                          f"{h['prediction']['text']} Observed: {pr['detail']}.", "", f"_{a['gate']}._"]
            L.append("")
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"markdown": out_dir / "phase6-development.md", "json": out_dir / "phase6-development.json"}
    paths["markdown"].write_text("\n".join(L) + "\n", encoding="utf-8")
    paths["json"].write_text(json.dumps({"protocol": fw, "trials": [
        {**{k: t[k] for k in ("id", "hypothesis", "kind", "status", "created_at", "finished_at", "error")},
         "params": json.loads(t["params"]), "analysis": json.loads(t["analysis"]) if t["analysis"] else None}
        for t in trials]}, indent=1, default=str), encoding="utf-8")
    return paths


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--freeze":
        print(freeze(" ".join(sys.argv[2:]) or "Phase 6 protocol frozen before any trial"))
    elif len(sys.argv) > 1 and sys.argv[1] == "--check":
        print(json.dumps(check(), indent=1))
    else:
        print({k: str(v) for k, v in write(Path(sys.argv[1]) if len(sys.argv) > 1 else REPORTS).items()})
