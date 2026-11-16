"""Pre-Phase 7 screening, stage 2 (research only): the externally sourced concepts stage 1 could not express.

Stage 1 (screening.py) used the strategy builder. Stage 2 adds two tools and screens the remaining concepts with them:
  - indicators.py: moving averages, RSI, ATR, Bollinger and Keltner bands, the squeeze momentum, higher-timeframe and
    daily bars, clock-time helpers and a tick-volume VWAP;
  - rulesim.py: a bar-by-bar rule simulator with market and stop-entry orders, clock exits and reversals, using the
    same cost assumptions as the bt@1 engine.

Rules, windows and thresholds are declared in config/research/pre_phase7/candidates_stage2.json before any result.
Every candle read goes through ``load_bars``, which applies the forward-data guard and refuses the sealed block.
A screen is not validation: it decides nothing about profitability or trading readiness.

  python -m goldscalp.research.screening_ext --builder-context   # stage 1 candidates over the context window (bt@1)
  python -m goldscalp.research.screening_ext --parity            # replay those bt@1 trades in the rule simulator
  python -m goldscalp.research.screening_ext --run               # stage 2 screen, delay check and context run
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from goldscalp.analysis.config import AnalysisConfig
from goldscalp.core import forward
from goldscalp.core.config import PROJECT_ROOT, load_settings
from goldscalp.core.timeframes import Timeframe
from goldscalp.monthly import discovery as dv
from goldscalp.monthly import robustness as rb
from goldscalp.monthly import runner
from goldscalp.monthly.data import DataSources
from goldscalp.monthly.store import ReadOnlySource
from goldscalp.research import screening as sc
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.strategies import builder

CONFIG2 = PROJECT_ROOT / "config" / "research" / "pre_phase7" / "candidates_stage2.json"
OUT_DIR = PROJECT_ROOT / "research" / "pre_phase7"
SEALED_SERIES = "csv:vantage-mt5-xauusd-sealed-2023-2025"


class ScreeningError(RuntimeError):
    pass


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load2(path: Path = CONFIG2) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def environment() -> dict[str, Any]:
    settings = load_settings()
    src = DataSources(ReadOnlySource(settings.db_path),
                      ReadOnlySource(Path(settings.db_path).parent / "monthly" / "monthly.sqlite3"))
    cspec, _ = runner.contract_spec(InstrumentSpec.load(settings.instrument_spec), src.verified_spec())
    return {"src": src, "spec": cspec, "ounces_per_lot": float(cspec.ounces(Decimal(1)))}


def load_bars(src: DataSources, store: str, series: str, tf: Timeframe, start: datetime, end: datetime) -> pd.DataFrame:
    """CLOSED candles in [start, end) for research: never the sealed block, never forward data."""
    if series == SEALED_SERIES or (start < dv.SEALED[1] and end > dv.SEALED[0]):
        raise ScreeningError("the sealed Phase 6 block is not available to a screen")
    what = f"Pre-Phase 7 screen on {series} ({tf.value})"
    forward.check_range(start, end, what)
    row = src.series_row(store, series)
    if row is None:
        raise ScreeningError(f"series {series} not found in the {store} store")
    frame = src.load(store, row, tf, start, end)
    forward.check_frame(frame, what)
    return frame


def month_chunks(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    out, a = [], start
    while a < end:
        nxt = datetime(a.year + (a.month == 12), a.month % 12 + 1, 1, tzinfo=UTC)
        out.append((a, min(nxt, end)))
        a = min(nxt, end)
    return out


# ------------------------------------------------------------------ stage 1 candidates over the context window (bt@1)
RAW_KEYS = ("side", "entry_time", "entry_price", "stop", "target", "exit_time", "exit_price", "exit_reason", "net_pnl",
            "r_multiple", "lots", "ounces", "commission", "spread_cost", "slippage_cost", "holding_minutes")


def builder_context(spec2: dict[str, Any]) -> dict[str, Any]:
    spec1 = sc.load()
    env = environment()
    src = env["src"]
    w = spec2["windows"]["context"]
    store, series = spec2["series"]["store"], spec2["series"]["key"]
    row = src.series_row(store, series)
    cfg = AnalysisConfig.load()
    assumptions = runner.make_assumptions(rb.COSTS)
    items = [c for c in spec1["candidates"] + spec1["baselines"] if c.get("config") is not None]
    detail = set().union(*(builder.detail_timeframes(c["config"]) for c in items))
    acc: dict[str, dict[str, list]] = {c["key"]: {"raw": [], "trades": []} for c in items}
    chunks = month_chunks(_t(w["start"]), _t(w["end"]))
    for a, b in chunks:
        prepared = runner.prepare(src, store, row, Timeframe.M5, a, b, lookback_days=rb.LOOKBACK_DAYS, detail=detail, cfg=cfg)
        snaps = {s["cutoff"]: s for s in prepared["snapshots"]}
        for c in items:
            res = runner.run_one(c["config"], prepared, Timeframe.M5, a, b, assumptions, env["spec"])
            acc[c["key"]]["raw"] += [{k: t.get(k) for k in RAW_KEYS} for t in res["trades"]]
            acc[c["key"]]["trades"] += rb.compact(res["trades"], snaps, c["config"]["exit"]["stop"]["timeframe"])
        print(f"bt@1 context {a:%Y-%m-%d} to {b:%Y-%m-%d} done", flush=True)
    out = {"definition": spec2["definition"], "engine": "bt@1", "window": w, "ran_at": _now(),
           "chunks": [[a.isoformat(), b.isoformat()] for a, b in chunks],
           "note": "Run in monthly pieces: a position still open at a month end is closed there.",
           "configs": {c["key"]: {"max_holding_minutes": c["config"]["exit"]["max_holding_minutes"]} for c in items},
           "results": {k: {"stats": dv.stats(v["trades"]), "by_day": sc.by_day(v["trades"]),
                           "concentration": sc.concentration(v["trades"]), "trades": v["trades"], "raw": v["raw"]}
                       for k, v in acc.items()}}
    return out


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ stage 2: rule simulator
def classify2(stats: dict[str, Any], conc: dict[str, Any], delayed: dict[str, Any], rules: dict[str, Any]) -> tuple[str, list[str]]:
    n, e = stats["trades"], stats["expectancy_r"]
    if n < rules["min_trades"]:
        return "INCONCLUSIVE", [f"{n} trades, fewer than {rules['min_trades']}"]
    if e is None or e <= 0:
        return "REJECTED", [f"expectancy {e} R/trade after costs over {n} trades"]
    checks = {
        "profit factor": (stats["profit_factor"] or 0) >= rules["min_profit_factor"],
        "positive without the best day": (conc["sum_r_without_best_day"] or 0) > 0,
        "positive with costs x2": (stats["expectancy_r_costs_x2"] or -1) > 0,
        "not negative with a one-bar delay": delayed["expectancy_r"] is not None and delayed["expectancy_r"] >= 0,
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        return "INCONCLUSIVE", [f"positive ({e:+.3f} R over {n} trades) but fails: " + ", ".join(failed)]
    return "WORTH FURTHER RESEARCH", [f"{e:+.3f} R/trade over {n} trades, PF {stats['profit_factor']}, passes every screen"]


def by_tag(trades: list[dict[str, Any]]) -> dict[str, Any]:
    tags = sorted({t.get("tag") for t in trades if t.get("tag")})
    return {tag: dv.stats([t for t in trades if t.get("tag") == tag]) for tag in tags} if len(tags) > 1 else {}


def run_rule(item: dict[str, Any], bars: pd.DataFrame, bar_minutes: int, start: datetime, end: datetime,
             env: dict[str, Any], delay_bars: int = 0) -> dict[str, Any]:
    from goldscalp.research import external_rules, rulesim
    rule = external_rules.make(item["rule"], item["params"])
    costs = rulesim.Costs(configured_spread=float(rb.COSTS["configured_spread"]),
                          commission_per_lot_per_side=float(rb.COSTS["commission"]), slippage=float(rb.COSTS["slippage"]))
    return rulesim.simulate(bars, rule, bar_minutes=bar_minutes, start=start, end=end, costs=costs, lots=0.02,
                            ounces_per_lot=env["ounces_per_lot"], delay_bars=delay_bars)


def run(spec2: dict[str, Any]) -> dict[str, Any]:
    env = environment()
    src = env["src"]
    store, series = spec2["series"]["store"], spec2["series"]["key"]
    hist = _t(spec2["history_from"])
    out: dict[str, Any] = {"definition": spec2["definition"], "ran_at": _now(), "series": spec2["series"],
                           "windows": spec2["windows"], "decision": spec2["decision"], "costs": rb.COSTS,
                           "results": {}, "classifications": {}}
    items = []
    for c in spec2["candidates"]:
        items.append((c, c["key"], c["params"], True))
        for extra in c.get("also_report", []):
            items.append(({**c, "params": extra["params"]}, extra["key"], extra["params"], False))
    for b in spec2["baselines"]:
        items.append((b, b["key"], b["params"], False))
    frames: dict[tuple[str, str], pd.DataFrame] = {}
    for wname, w in spec2["windows"].items():
        start, end = _t(w["start"]), _t(w["end"])
        for c, key, params, classified in items:
            tf_name = (c.get("frames") or {}).get(wname, "M5")
            tf = Timeframe.parse(tf_name)
            if (wname, tf_name) not in frames:
                frames[(wname, tf_name)] = load_bars(src, store, series, tf, hist, end)
            bars = frames[(wname, tf_name)]
            item = {"rule": c["rule"], "params": params}
            res = run_rule(item, bars, tf.seconds // 60, start, end, env)
            trades = res["trades"]
            entry = {"frame": tf_name, "stats": dv.stats(trades), "usd_per_trade": _usd(trades),
                     "by_day": sc.by_day(trades), "concentration": sc.concentration(trades), "by_tag": by_tag(trades),
                     "nominal_r": any(t.get("nominal_r") for t in trades), "exits": res["exits"],
                     "diagnostics": res["diagnostics"], "trades": trades}
            if wname == "screen":
                delayed = run_rule(item, bars, tf.seconds // 60, start, end, env, delay_bars=spec2["decision"]["delay_bars"])
                entry["delayed"] = dv.stats(delayed["trades"])
                if classified:
                    cls, why = classify2(entry["stats"], entry["concentration"], entry["delayed"], spec2["decision"])
                    out["classifications"][key] = {"classification": cls, "why": why}
            out["results"].setdefault(key, {})[wname] = entry
    return out


def _usd(trades: list[dict[str, Any]]) -> float | None:
    return round(sum(t["net"] for t in trades) / len(trades), 2) if trades else None


# ------------------------------------------------------------------ parity: bt@1 trades replayed in the rule simulator
def parity(context: dict[str, Any], spec2: dict[str, Any]) -> dict[str, Any]:
    """Replay every bt@1 trade (same entry bar, side, stop, target and time limit) in the rule simulator and compare
    the exit and the net result. Checks fills and costs; it says nothing about any strategy."""
    from goldscalp.research import external_rules, rulesim
    env = environment()
    w = context["window"]
    start, end = _t(w["start"]), _t(w["end"])
    bars = load_bars(env["src"], spec2["series"]["store"], spec2["series"]["key"], Timeframe.M5, _t(spec2["history_from"]), end)
    costs = rulesim.Costs(configured_spread=float(rb.COSTS["configured_spread"]),
                          commission_per_lot_per_side=float(rb.COSTS["commission"]), slippage=float(rb.COSTS["slippage"]))
    report: dict[str, Any] = {}
    chunk_ends = {pd.Timestamp(b) for _, b in context["chunks"]}
    for key, r in context["results"].items():
        hold = context["configs"][key]["max_holding_minutes"]
        compared = matched = 0
        worst = 0.0
        mismatches = []
        for t in r["raw"]:
            rule = external_rules.make("replay", {"entry_time": t["entry_time"], "side": t["side"], "stop": t["stop"],
                                                  "target": t["target"], "hold_minutes": hold})
            entry = pd.Timestamp(t["entry_time"])
            exit_cap = min((c for c in chunk_ends if c > entry), default=pd.Timestamp(end))       # bt@1 ran in monthly pieces
            near = bars[(bars.index >= entry - pd.Timedelta(hours=2)) & (bars.index < exit_cap)]
            res = rulesim.simulate(near, rule, bar_minutes=5, start=start, end=exit_cap.to_pydatetime(), costs=costs,
                                   lots=float(t["lots"]), ounces_per_lot=env["ounces_per_lot"])
            compared += 1
            if not res["trades"]:
                mismatches.append({"entry": t["entry_time"], "problem": "no trade in the simulator"})
                continue
            s = res["trades"][0]
            diff = abs(s["net"] - t["net_pnl"])
            worst = max(worst, diff)
            same = bool(s["exit"] == t["exit_time"] and diff <= 0.02)
            matched += int(same)
            if not same and len(mismatches) < 10:
                mismatches.append({"entry": t["entry_time"], "bt1": [t["exit_time"], t["exit_reason"], t["net_pnl"]],
                                   "sim": [s["exit"], s["reason"], s["net"]]})
        report[key] = {"compared": compared, "matched": matched, "largest_net_difference_usd": round(float(worst), 4),
                       "mismatches": mismatches}
    return report


# ------------------------------------------------------------------ after-the-fact placebo check for the clock rules
def placebo(spec2: dict[str, Any], window: str = "context", minutes: int = 30) -> dict[str, Any]:
    """The same short, held ``minutes``, started at EVERY half hour of the New York day (hh:05 and hh:35), over one
    window. It shows whether the two pre-fix half hours of P7-FIX stand out from an ordinary half hour in the same
    market. Exploratory and run after the screen: it is not part of the declared screen and selects nothing."""
    env = environment()
    w = spec2["windows"][window]
    start, end = _t(w["start"]), _t(w["end"])
    bars = load_bars(env["src"], spec2["series"]["store"], spec2["series"]["key"], Timeframe.M5, _t(spec2["history_from"]), end)
    rows = []
    for m in range(5, 24 * 60, 30):
        enter, leave = f"{m // 60:02d}:{m % 60:02d}", f"{((m + minutes) // 60) % 24:02d}:{(m + minutes) % 60:02d}"
        item = {"rule": "clock_trades", "params": {"tz": "America/New_York", "trades": [{"enter": enter, "side": "short", "exit": leave}]}}
        trades = run_rule(item, bars, 5, start, end, env)["trades"]
        nets = [t["net"] for t in trades]
        n = len(nets)
        mean = sum(nets) / n if n else None
        sd = (sum((x - mean) ** 2 for x in nets) / (n - 1)) ** 0.5 if n > 1 else None
        rows.append({"enter_new_york": enter, "trades": n, "net_usd_per_trade": None if mean is None else round(mean, 2),
                     "t_stat": None if not sd else round(mean / (sd / n ** 0.5), 2),
                     "win_rate": round(sum(1 for x in nets if x > 0) / n, 3) if n else None,
                     "expectancy_r": dv.stats(trades)["expectancy_r"], "cost_usd_per_trade": round(sum(t["cost_usd"] for t in trades) / n, 2) if n else None})
    ranked = sorted([r for r in rows if r["net_usd_per_trade"] is not None], key=lambda r: -r["net_usd_per_trade"])
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    return {"window": w, "side": "short", "hold_minutes": minutes, "note": "Exploratory placebo, run after the screen.",
            "positive_windows": sum(1 for r in rows if (r["net_usd_per_trade"] or 0) > 0), "windows": rows}


# ------------------------------------------------------------------ markdown tables for the report
def _r(v: Any, d: int = 3, sign: bool = True) -> str:
    if v is None:
        return "—"
    text = f"{abs(v):.{d}f}"
    return ("+" if v > 0 else "−" if v < 0 else "") + text if sign else text


def _row(key: str, e: dict[str, Any], extra: list[str]) -> str:
    st, c = e["stats"], e["concentration"]
    win = "—" if st["win_rate"] is None else f"{st['win_rate'] * 100:.1f}%"
    return (f"| {key} | {st['trades']} | {win} | {_r(st['expectancy_r'])} | {_r(st['profit_factor'], 2, False)} | "
            f"{_r(st['sum_r'], 2)} | {_r(st['max_dd_r'], 2, False)} | {_r(st['net_usd'], 2)} | "
            f"{_r(st['expectancy_r_costs_x2'])} | " + " | ".join(extra) + f" | {c.get('positive_days', 0) or 0} of {c['days_traded']} | "
            f"{_r(c.get('sum_r_without_best_day'), 2)} |")


def tables(res: dict[str, Any], ctx: dict[str, Any] | None, stage1: dict[str, Any] | None) -> str:
    L: list[str] = []
    keys = list(res["results"])
    head = "| Key | Trades | Win rate | R/trade | PF | Sum R | Max DD (R) | Net USD | R/trade, costs ×2 | {} | Positive days | Sum R without best day |"
    L += ["### Screen", "", head.format("R/trade, 1-bar delay | R unit"), "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|"]
    for k in keys:
        e = res["results"][k]["screen"]
        L.append(_row(k, e, [_r(e["delayed"]["expectancy_r"]), "nominal" if e["nominal_r"] else "stop"]))
    L += ["", "### Screen by day (sum of R)", ""]
    days = sorted({d for k in keys for d in res["results"][k]["screen"]["by_day"]})
    L += ["| Key | " + " | ".join(d[5:] for d in days) + " |", "|---|" + "---:|" * len(days)]
    for k in keys:
        bd = res["results"][k]["screen"]["by_day"]
        L.append(f"| {k} | " + " | ".join(_r(bd[d]["sum_r"], 2) if d in bd else "—" for d in days) + " |")
    L += ["", "### Legs and sub-groups (screen and context)", "", "| Key | Group | Window | Trades | R/trade | Sum R | Net USD |", "|---|---|---|---:|---:|---:|---:|"]
    for k in keys:
        for wname in ("screen", "context"):
            for tag, st in res["results"][k][wname]["by_tag"].items():
                L.append(f"| {k} | {tag} | {wname} | {st['trades']} | {_r(st['expectancy_r'])} | {_r(st['sum_r'], 2)} | {_r(st['net_usd'], 2)} |")
    L += ["", "### Context", "", head.format("Engine | R unit"), "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|"]
    for k in keys:
        e = res["results"][k]["context"]
        L.append(_row(k, e, ["rule simulator", "nominal" if e["nominal_r"] else "stop"]))
    for k, e in (ctx or {}).get("results", {}).items():
        L.append(_row(k, e, ["bt@1", "stop"]))
    if stage1:
        L += ["", "### Stage 1 results on the screen window (bt@1, M5), for comparison", "",
              "| Key | Trades | R/trade | PF | Sum R | Net USD |", "|---|---:|---:|---:|---:|---:|"]
        for k, r in stage1["results"].items():
            st = r["M5"]["stats"]
            L.append(f"| {k} | {st['trades']} | {_r(st['expectancy_r'])} | {_r(st['profit_factor'], 2, False)} | {_r(st['sum_r'], 2)} | {_r(st['net_usd'], 2)} |")
    L += ["", "### Classifications", "", "| Key | Classification | Why |", "|---|---|---|"]
    for k, c in res["classifications"].items():
        L.append(f"| {k} | **{c['classification']}** | {'; '.join(c['why'])} |")
    return "\n".join(L) + "\n"


def main(argv: list[str]) -> None:
    spec2 = load2()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if argv[:1] == ["--builder-context"]:
        res = builder_context(spec2)
        (OUT_DIR / "stage2-builder-context.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
        print(json.dumps({k: v["stats"] for k, v in res["results"].items()}, indent=1))
    elif argv[:1] == ["--parity"]:
        ctx = json.loads((OUT_DIR / "stage2-builder-context.json").read_text(encoding="utf-8"))
        rep = parity(ctx, spec2)
        (OUT_DIR / "stage2-parity.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
        print(json.dumps({k: {x: v[x] for x in ("compared", "matched", "largest_net_difference_usd")} for k, v in rep.items()}, indent=1))
    elif argv[:1] == ["--run"]:
        res = run(spec2)
        (OUT_DIR / "stage2-results.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
        print(json.dumps(res["classifications"], indent=1))
    elif argv[:1] == ["--placebo"]:
        res = placebo(spec2)
        (OUT_DIR / "stage2-fix-placebo.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
        for r in sorted(res["windows"], key=lambda r: r.get("rank", 99)):
            print(r.get("rank"), r["enter_new_york"], r["trades"], r["net_usd_per_trade"], r["t_stat"], r["win_rate"])
    elif argv[:1] == ["--tables"]:
        def read(name: str) -> Any:
            f = OUT_DIR / name
            return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
        text = tables(read("stage2-results.json"), read("stage2-builder-context.json"), read("screening-results.json"))
        (OUT_DIR / "stage2-tables.md").write_text(text, encoding="utf-8")
        print(f"written {OUT_DIR / 'stage2-tables.md'}")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
