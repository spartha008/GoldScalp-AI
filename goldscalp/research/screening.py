"""Pre-Phase 7 screening (research only): a SHORT historical screen of externally sourced strategy concepts.

A screen is not validation. It shows only whether a concept is worth deeper research, and it decides nothing about
profitability or trading readiness.

- Data: development data that research has ALREADY used. Never the sealed Phase 6 block, never forward data; the
  forward-data guard applies through ``runner.prepare``.
- Engine and costs: the same bt@1 engine and the frozen Phase 5 costs as every earlier study.
- Accounts: each candidate and timeframe is one separate hypothetical $1,000 account.
- Classification: the rules are declared in ``config/research/pre_phase7/candidates.json`` before any result exists.

  python -m goldscalp.research.screening [config] [out.json]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from goldscalp.analysis.config import AnalysisConfig
from goldscalp.core.config import PROJECT_ROOT, load_settings
from goldscalp.core.timeframes import Timeframe
from goldscalp.monthly import discovery as dv
from goldscalp.monthly import robustness as rb
from goldscalp.monthly import runner
from goldscalp.monthly.data import DataSources
from goldscalp.monthly.store import ReadOnlySource
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.strategies import builder

CONFIG = PROJECT_ROOT / "config" / "research" / "pre_phase7" / "candidates.json"
OUT = PROJECT_ROOT / "research" / "pre_phase7" / "screening-results.json"


def load(path: Path = CONFIG) -> dict[str, Any]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    for c in spec["candidates"] + spec["baselines"]:
        if c.get("config") is not None:
            c["config"] = builder.validate(c["config"])
    return spec


def by_day(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    days: dict[str, list] = defaultdict(list)
    for t in trades:
        days[t["entry"][:10]].append(t)
    out = {}
    for d, ts in sorted(days.items()):
        rs = [t["r"] for t in ts if t["r"] is not None]
        out[d] = {"trades": len(ts), "sum_r": round(sum(rs), 3), "net_usd": round(sum(t["net"] for t in ts), 2)}
    return out


def concentration(trades: list[dict[str, Any]]) -> dict[str, Any]:
    days = by_day(trades)
    total = round(sum(d["sum_r"] for d in days.values()), 3)
    if not days:
        return {"days_traded": 0, "best_day": None, "sum_r_without_best_day": None, "best_day_share_of_gains": None}
    best = max(days, key=lambda k: days[k]["sum_r"])
    gains = sum(d["sum_r"] for d in days.values() if d["sum_r"] > 0)
    rest = [t for t in trades if t["entry"][:10] != best]
    rs = [t["r"] for t in rest if t["r"] is not None]
    return {"days_traded": len(days), "positive_days": sum(1 for d in days.values() if d["sum_r"] > 0),
            "best_day": best, "best_day_sum_r": days[best]["sum_r"], "sum_r": total,
            "sum_r_without_best_day": round(total - days[best]["sum_r"], 3),
            "expectancy_r_without_best_day": round(sum(rs) / len(rs), 4) if rs else None,
            "best_day_share_of_gains": round(days[best]["sum_r"] / gains, 3) if gains > 0 else None}


def classify(m5: dict[str, Any], m15: dict[str, Any], conc: dict[str, Any], rules: dict[str, Any]) -> tuple[str, list[str]]:
    """The pre-declared screening rules (config 'decision'); evaluated mechanically."""
    n, e = m5["trades"], m5["expectancy_r"]
    if n < rules["min_trades"]:
        return "INCONCLUSIVE", [f"{n} M5 trades, fewer than {rules['min_trades']}"]
    if e is None or e <= 0:
        return "REJECTED", [f"M5 expectancy {e} R/trade after costs over {n} trades"]
    checks = {
        "profit factor": (m5["profit_factor"] or 0) >= rules["min_profit_factor"],
        "positive without the best day": (conc["sum_r_without_best_day"] or 0) > 0,
        "positive with costs x2": (m5["expectancy_r_costs_x2"] or -1) > 0,
        "M15 execution not negative": m15["trades"] == 0 or (m15["expectancy_r"] or 0) >= 0,
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        return "INCONCLUSIVE", [f"positive ({e:+.3f} R over {n} trades) but fails: " + ", ".join(failed)]
    return "WORTH FURTHER RESEARCH", [f"{e:+.3f} R/trade over {n} trades, PF {m5['profit_factor']}, passes every screen"]


def run(spec: dict[str, Any]) -> dict[str, Any]:
    settings = load_settings()
    src = DataSources(ReadOnlySource(settings.db_path), ReadOnlySource(Path(settings.db_path).parent / "monthly" / "monthly.sqlite3"))
    w = spec["window"]
    start = datetime.fromisoformat(w["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(w["end"].replace("Z", "+00:00"))
    store, series = spec["series"]["store"], spec["series"]["key"]
    row = src.series_row(store, series)
    cfg = AnalysisConfig.load()
    cspec, _ = runner.contract_spec(InstrumentSpec.load(settings.instrument_spec), src.verified_spec())
    assumptions = runner.make_assumptions(rb.COSTS)
    items = [c for c in spec["candidates"] + spec["baselines"] if c.get("config") is not None]
    detail = set().union(*(builder.detail_timeframes(c["config"]) for c in items))
    results: dict[str, Any] = {}
    for tf_name in spec["timeframes"]:
        tf = Timeframe.parse(tf_name)
        prepared = runner.prepare(src, store, row, tf, start, end, lookback_days=rb.LOOKBACK_DAYS, detail=detail, cfg=cfg)
        snaps = {s["cutoff"]: s for s in prepared["snapshots"]}
        for c in items:
            res = runner.run_one(c["config"], prepared, tf, start, end, assumptions, cspec)
            trades = rb.compact(res["trades"], snaps, c["config"]["exit"]["stop"]["timeframe"])
            results.setdefault(c["key"], {})[tf_name] = {"stats": dv.stats(trades), "by_day": by_day(trades),
                                                         "concentration": concentration(trades), "trades": trades,
                                                         "result_hash": res["result_hash"],
                                                         "dataset": prepared["dataset"]}
    for c in spec["candidates"]:
        r = results.get(c["key"])
        if r is None:                                     # not screened: classification declared with its reason
            c["classification"], c["why"] = c["pre_classification"], [c["not_screened_reason"]]
            continue
        empty = {"trades": 0, "expectancy_r": None}
        c["classification"], c["why"] = classify(r["M5"]["stats"], r.get("M15", {}).get("stats", empty),
                                                 r["M5"]["concentration"], spec["decision"])
    return {"definition": spec["definition"], "ran_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "window": w, "series": spec["series"], "timeframes": spec["timeframes"], "costs": rb.COSTS,
            "decision": spec["decision"],
            "classifications": {c["key"]: {"classification": c["classification"], "why": c["why"]} for c in spec["candidates"]},
            "results": results}


def main(argv: list[str]) -> None:
    cfg = Path(argv[0]) if argv else CONFIG
    out = Path(argv[1]) if len(argv) > 1 else OUT
    res = run(load(cfg))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    print(json.dumps(res["classifications"], indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
