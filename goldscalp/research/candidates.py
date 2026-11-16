"""Research candidates registry (research-candidate@1): externally sourced rule sets kept in GoldScalp for further
research, with their source, their original rules and the evidence they were registered on.

A research candidate lives in config/research/candidates as a file and runs in the research rule simulator. Being
registered says "worth a proper test"; it says nothing about profitability or trading readiness.

Since 16 November 2026 each candidate also has a Strategy Lab (strategy@1) form, listed in LAB_STRATEGIES.json and added
at the owner's request so it can be backtested in the Lab and monitored in Live Validation. The as-screened versions
place the same trades as the screened rule sets. A version with a protective stop exists for demo execution, which
never sends an order without a stop-loss. What a Lab form does live is not a validation of the candidate.

The rules are preserved exactly as screened:
  - each file carries a fingerprint of its rule and parameters;
  - ``check`` also compares them with the screen's own declaration (candidates_stage2.json);
  - a changed rule is a new candidate with a new id, never an edit.

Read-only API for the Monthly Backtest page (GET only):  /api/monthly/research-candidates

  python -m goldscalp.research.candidates --check       # fingerprints and rules unchanged
  python -m goldscalp.research.candidates --reproduce   # re-run both windows and compare with the stored evidence
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from goldscalp.core.config import PROJECT_ROOT

DEFINITION = "research-candidate@1"
DIR = PROJECT_ROOT / "config" / "research" / "candidates"
PRE7 = PROJECT_ROOT / "research" / "pre_phase7"
STATUS = "research candidate"
BANNER = ("Research candidates are published rule sets that passed a short screen on development data. They are not "
          "validated, not shown to be profitable and not trading-ready. Since 16 November 2026 each also has a Strategy "
          "Lab form, added at the owner's request; what that form does live or on the MT5 demo account is not a "
          "validation of the candidate.")
LAB_FILE = "LAB_STRATEGIES.json"
HISTORY_FILE = "candidates-2026-history.json"
STAT_KEYS = ("trades", "win_rate", "expectancy_r", "profit_factor", "sum_r", "max_dd_r", "net_usd",
             "expectancy_r_costs_x2", "expectancy_r_costs_x3")

router = APIRouter(prefix="/api/monthly/research-candidates")
_cache: dict[str, tuple[float, Any]] = {}


def fingerprint(rule: str, params: dict[str, Any]) -> str:
    body = json.dumps({"rule": rule, "params": params}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _read(path: Path) -> Any:
    """A JSON file, re-read only when it changes (the result files are a few megabytes)."""
    if not path.is_file():
        return None
    mtime = path.stat().st_mtime
    hit = _cache.get(str(path))
    if hit is None or hit[0] != mtime:
        hit = (mtime, json.loads(path.read_text(encoding="utf-8")))
        _cache[str(path)] = hit
    return hit[1]


def load_all(directory: Path | None = None) -> list[dict[str, Any]]:
    d = directory or DIR
    registry = _read(d / "REGISTRY.json")
    if not registry or registry.get("definition") != DEFINITION:
        return []
    out = []
    for cid in registry["candidates"]:
        c = _read(d / f"{cid}.json")
        if c is not None:
            out.append(c)
    return out


def check(directory: Path | None = None, declaration: Path | None = None) -> dict[str, Any]:
    """Every candidate's rule is known, its fingerprint matches, and its rule and parameters are exactly the ones the
    screen declared."""
    from goldscalp.research import external_rules, screening_ext
    declared = {c["key"]: c for c in (json.loads(declaration.read_text(encoding="utf-8")) if declaration
                                      else screening_ext.load2())["candidates"]}
    rows = []
    for c in load_all(directory):
        d = declared.get(c["id"])
        rows.append({"id": c["id"], "rule_known": c["rule"] in external_rules.RULES,
                     "fingerprint_ok": fingerprint(c["rule"], c["params"]) == c["fingerprint"],
                     "matches_declaration": bool(d) and d["rule"] == c["rule"] and d["params"] == c["params"],
                     "status": c["status"]})
    ok = bool(rows) and all(r["rule_known"] and r["fingerprint_ok"] and r["matches_declaration"] and r["status"] == STATUS for r in rows)
    return {"definition": DEFINITION, "candidates": len(rows), "unchanged": ok, "rows": rows}


# ------------------------------------------------------------------ evidence, from the stored screen results
def _by_day(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    days: dict[str, list] = defaultdict(list)
    for t in trades:
        days[t["entry"][:10]].append(t)
    out, cum = [], 0.0
    for d in sorted(days):
        r = sum(t["r"] for t in days[d])
        cum += r
        out.append({"day": d, "trades": len(days[d]), "sum_r": round(r, 3), "cum_r": round(cum, 3),
                    "net_usd": round(sum(t["net"] for t in days[d]), 2)})
    return out


def _by_month(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    months: dict[str, list] = defaultdict(list)
    for t in trades:
        months[t["entry"][:7]].append(t)
    return [{"month": m, "trades": len(ts), "sum_r": round(sum(t["r"] for t in ts), 3),
             "net_usd": round(sum(t["net"] for t in ts), 2),
             "win_rate": round(sum(1 for t in ts if t["net"] > 0) / len(ts), 4)} for m, ts in sorted(months.items())]


def evidence(c: dict[str, Any], pre7: Path | None = None) -> dict[str, Any]:
    """Per window: pooled figures, day-by-day and month-by-month series and the legs, read from the stored results."""
    d = pre7 or PRE7
    res = _read(d / "stage2-results.json") or {}
    entry = (res.get("results") or {}).get(c["id"])
    if not entry:
        return {"available": False}
    out: dict[str, Any] = {"available": True, "ran_at": res.get("ran_at"), "windows": {}}
    for name, w in entry.items():
        conc = w["concentration"]
        out["windows"][name] = {
            "range": (res.get("windows") or {}).get(name), "frame": w.get("frame"), "nominal_r": w.get("nominal_r"),
            "stats": {k: w["stats"].get(k) for k in STAT_KEYS}, "usd_per_trade": w.get("usd_per_trade"),
            "days_traded": conc.get("days_traded"), "positive_days": conc.get("positive_days"),
            "sum_r_without_best_day": conc.get("sum_r_without_best_day"), "best_day": conc.get("best_day"),
            "delayed": {k: (w.get("delayed") or {}).get(k) for k in ("trades", "expectancy_r")} if w.get("delayed") else None,
            "legs": w.get("by_tag") or {}, "by_day": _by_day(w["trades"]), "by_month": _by_month(w["trades"]),
            "exits": w.get("exits")}
    cls = (res.get("classifications") or {}).get(c["id"])
    out["screen_classification"] = cls
    placebo = _read(d / "stage2-fix-placebo.json")
    if placebo and c["id"] == "P7-FIX":
        ranked = sorted([r for r in placebo["windows"] if r.get("rank")], key=lambda r: r["rank"])
        out["placebo"] = {"note": placebo.get("note"), "windows_tested": len(ranked),
                          "positive_windows": placebo.get("positive_windows"),
                          "own_windows": [r for r in ranked if r["enter_new_york"] in ("05:05", "09:35")],
                          "top": ranked[:5], "bottom": ranked[-3:]}
    hist = (_read(d / HISTORY_FILE) or {}).get(c["id"])
    if hist:                                                   # a longer run made after registration; exploratory
        conc = hist.get("concentration") or {}
        out["history"] = {
            "spans": [{k: s.get(k) for k in ("start", "end", "trades")} for s in hist["spans"]],
            "stats": {k: hist["stats"].get(k) for k in STAT_KEYS}, "legs": hist.get("legs") or {},
            "by_month": [{"month": m, **v} for m, v in sorted(hist["by_month"].items())],
            "positive_months": hist.get("positive_months"), "months": hist.get("months"),
            "days_traded": conc.get("days_traded"), "positive_days": conc.get("positive_days")}
    return out


def lab_strategies(directory: Path | None = None) -> list[dict[str, Any]]:
    """The Strategy Lab (strategy@1) form of the candidates: validated configurations with their fingerprints."""
    from goldscalp.strategies import builder
    doc = _read((directory or DIR) / LAB_FILE) or {}
    out = []
    for item in doc.get("strategies", []):
        versions = [{**v, "config": builder.validate(v["config"]), "fingerprint_ok": builder.fingerprint(builder.validate(v["config"])) == v["fingerprint"]}
                    for v in item["versions"]]
        out.append({**item, "versions": versions})
    return out


def overview(directory: Path | None = None, pre7: Path | None = None) -> dict[str, Any]:
    chk = {r["id"]: r for r in check(directory)["rows"]} if load_all(directory) else {}
    lab: dict[str, list] = defaultdict(list)
    for item in lab_strategies(directory):
        lab[item["candidate"]].append({"key": item["key"], "strategy_name": item["strategy_name"],
                                       "versions": [{k: v.get(k) for k in ("label", "as_screened", "difference", "fingerprint", "fingerprint_ok")}
                                                    for v in item["versions"]]})
    return {"definition": DEFINITION, "banner": BANNER,
            "candidates": [{**c, "check": chk.get(c["id"]), "evidence": evidence(c, pre7), "lab": lab.get(c["id"], [])}
                           for c in load_all(directory)]}


@router.get("")
def list_candidates() -> dict[str, Any]:
    return overview()


# ------------------------------------------------------------------ reproduce (research simulator, guarded data)
def reproduce() -> dict[str, Any]:
    """Re-run every candidate over both declared windows and compare with the evidence stored at registration."""
    from goldscalp.core.timeframes import Timeframe
    from goldscalp.monthly import discovery as dv
    from goldscalp.research import screening_ext as sx
    spec2, env = sx.load2(), sx.environment()
    out = {}
    for c in load_all():
        rows = {}
        for name, w in spec2["windows"].items():
            start, end = sx._t(w["start"]), sx._t(w["end"])
            bars = sx.load_bars(env["src"], spec2["series"]["store"], spec2["series"]["key"], Timeframe.M5,
                                sx._t(spec2["history_from"]), end)
            stats = dv.stats(sx.run_rule({"rule": c["rule"], "params": c["params"]}, bars, 5, start, end, env)["trades"])
            stored = c["evidence_at_registration"][name]
            rows[name] = {"trades": stats["trades"], "expectancy_r": stats["expectancy_r"], "net_usd": stats["net_usd"],
                          "matches_registration": all(stats[k] == stored[k] for k in ("trades", "expectancy_r", "net_usd"))}
        out[c["id"]] = rows
    return out


def main(argv: list[str]) -> None:
    if argv[:1] == ["--check"]:
        print(json.dumps(check(), indent=2))
    elif argv[:1] == ["--reproduce"]:
        print(json.dumps(reproduce(), indent=2))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
