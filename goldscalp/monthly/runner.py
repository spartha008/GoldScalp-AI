"""Monthly backtest runner (definition monthly@1). PHASE5_ARCHITECTURE.md sections 4-6.

Each run = ONE strategy version x ONE timeframe in its OWN hypothetical account ($1,000, 1:500 margin only):
  decisions  at every close of the run timeframe (M15 / M5 / M1), from the as-of snapshot of the analysis dataset
             (M5 analysis candles for M15/M5 runs, M1 for M1 runs; only candles closed by each cut-off);
  execution  on the run timeframe's OWN native candles with the Phase 3 engine (bt@1): next-open fills, candle
             spread, commission, adverse slippage, stop-first collisions, gaps, margin from free margin, rejected
             entries recorded; open positions close at the month end (window_end).
Timelines are computed once per run timeframe and shared by every strategy of the evaluation (identical inputs).
Research uses data strictly BEFORE the month (asserted)."""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from goldscalp.analysis.config import AnalysisConfig
from goldscalp.analysis.intelligence import FRAME_LIMITS
from goldscalp.analysis.replay import ReplayDataset
from goldscalp.backtesting import engine, features, metrics
from goldscalp.core import forward
from goldscalp.core.clock import iso, parse_iso_aware
from goldscalp.core.timeframes import ALL_TIMEFRAMES, Timeframe
from goldscalp.monthly import candidates as cand
from goldscalp.monthly.data import DataSources
from goldscalp.monthly.report import RISK_BANNER  # noqa: F401 - one banner text for page, API and PDFs
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.strategies import builder

VERSION = "monthly@1"   # unchanged by Phase 5.1: fixed-lot runs are computed exactly as before
STARTING_BALANCE = "1000"
LEVERAGE = 500
CODE_FILES = ("backtesting/engine.py", "backtesting/features.py", "backtesting/metrics.py", "strategies/builder.py",
              "strategies/conditions.py", "analysis/replay.py", "monthly/runner.py", "monthly/report.py",
              "monthly/candidates.py")
MIN_RESEARCH_TRADES = 10
RESEARCH_DEV_SHARE = 0.7


class MonthlyError(ValueError):
    pass


def app_version() -> str:
    """Package version + a hash of the code that decides results (reproducibility record)."""
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    for rel in CODE_FILES:
        p = root / rel
        h.update(rel.encode())
        h.update(p.read_bytes() if p.exists() else b"")
    return f"goldscalp 0.1.0 / code {h.hexdigest()[:16]}"


# ------------------------------------------------------------------ contract and assumptions
def contract_spec(app_spec: InstrumentSpec, verified: dict[str, Any] | None) -> tuple[InstrumentSpec, dict[str, Any]]:
    """The application's spec with the contract values of the ACTUAL-terminal verification record, if any."""
    m = app_spec.to_mapping()
    if not verified:
        return app_spec, {"verified": False, "source": app_spec.spec_origin,
                          "note": "no actual-terminal verification record: hypothetical contract values, not broker-accurate"}
    m.update(contract_size=str(verified["contract_size"]), min_lot=str(verified["volume_min"]),
             lot_step=str(verified["volume_step"]), max_lot=str(verified["volume_max"]),
             price_decimals=int(verified["digits"]), tick_size=str(verified["point"]), is_hypothetical=True,
             name=f"{verified['name']} (contract from MT5 terminal verification {verified['record']})",
             spec_origin=(f"Contract size, lot limits, digits and tick size read from the actual MT5 terminal "
                          f"(verification {verified['record']}, {verified['verified_at']}). The trading schedule is the "
                          "application's hypothetical one; tick value is derived, not read from the terminal."))
    spec = InstrumentSpec.from_mapping(m)
    tick_value = float(Decimal(str(verified["contract_size"])) * Decimal(str(verified["point"])))
    return spec, {"verified": True, "record": verified["record"], "symbol": verified["name"],
                  "contract_size": verified["contract_size"], "min_lot": verified["volume_min"],
                  "lot_step": verified["volume_step"], "max_lot": verified["volume_max"], "digits": verified["digits"],
                  "tick_size": verified["point"], "tick_value_per_lot": tick_value,
                  "tick_value_note": "derived: contract size x tick size (USD-quoted XAUUSD); not read from the terminal",
                  "margin_note": f"1:{LEVERAGE} is a hypothetical margin parameter (notional / {LEVERAGE}); the broker's "
                                 "own margin rules may differ"}


def make_assumptions(params: dict[str, Any]) -> engine.Assumptions:
    data = {"configured_spread": params["configured_spread"], "commission_per_lot_per_side": params["commission"],
            "slippage": params["slippage"], "intrabar": "stop_first", "starting_balance": STARTING_BALANCE,
            "leverage": LEVERAGE, "min_trades_for_ratios": 30}
    scenario = params.get("risk_scenario")
    if scenario:
        # Phase 5.1: hypothetical stress test. Equity-based, cost-inclusive sizing; the strategy's own
        # max_open_positions and max_exposure_lots stay in force (merged, not replaced).
        data["sizing_override"] = {"sizing": engine.RISK_EQUITY, "risk_percent": float(scenario["risk_percent"])}
    elif params.get("risk_percent_override"):
        data["sizing_override"] = {"sizing": "risk_percent", "risk_percent": float(params["risk_percent_override"]),
                                   "lots": "0.01", "max_open_positions": 1, "max_exposure_lots": "100"}
    return engine.Assumptions.from_mapping(data)


RISK_SCENARIOS = ({"key": "S1", "risk_percent": 5.0}, {"key": "S2", "risk_percent": 7.5},
                  {"key": "S3", "risk_percent": 10.0})


def readiness(config: dict[str, Any], params: dict[str, Any], contract: dict[str, Any] | None = None) -> dict[str, Any]:
    """Risk configuration actually used (never invented) and the RR check against the user's 1:1.5-1:2 aim."""
    risk = config["risk"]
    scenario = params.get("risk_scenario")
    if scenario:
        if contract is not None and not contract.get("verified"):
            return {"ready": False, "reason": "percentage risk sizing needs the verified contract (contract size, lot "
                                              "limits, step, tick size) from an actual MT5 terminal verification record"}
        sizing = {"mode": f"{engine.RISK_EQUITY} (HYPOTHETICAL STRESS SCENARIO {scenario['key']})",
                  "risk_percent": float(scenario["risk_percent"]),
                  "note": "budget = current simulated equity x risk %; planned loss per lot = (|entry - stop| + "
                          "slippage) x contract size + round-trip commission; volume rounded down to the lot step; "
                          f"strategy limits kept: max {risk['max_open_positions']} open, max {risk['max_exposure_lots']} lots"}
    elif params.get("risk_percent_override"):
        sizing = {"mode": "risk_percent (USER OVERRIDE for this evaluation)",
                  "risk_percent": float(params["risk_percent_override"])}
    elif risk["sizing"] == "fixed_lots":
        sizing = {"mode": "fixed_lots (as configured)", "lots": str(risk["lots"]),
                  "note": "monetary risk per trade = |entry - stop| x lots x contract size, computed per entry"}
    elif risk["sizing"] == "risk_percent":
        sizing = {"mode": "risk_percent (as configured)", "risk_percent": risk["risk_percent"]}
    else:
        return {"ready": False, "reason": f"unknown sizing {risk['sizing']!r}"}
    t, s = config["exit"]["target"], config["exit"]["stop"]
    return {"ready": True, "sizing": sizing, "max_open_positions": risk["max_open_positions"],
            "stop": s, "target": t, "max_holding_minutes": config["exit"]["max_holding_minutes"],
            "rr": cand.rr_check(config)}


# ------------------------------------------------------------------ datasets
def load_dataset(src: DataSources, store: str, row: Any, base_tf: Timeframe, start: datetime, end: datetime,
                 lookback_days: int) -> ReplayDataset:
    """Analysis candles at a FORCED base timeframe from start - lookback to end (closed only), plus native coarser
    history that ended before the first base candle (like services.load_dataset, without choosing the base)."""
    first_wanted = pd.Timestamp(start - timedelta(days=lookback_days)).floor("h").to_pydatetime()
    base = src.load(store, row, base_tf, first_wanted, end)
    history: dict[str, pd.DataFrame] = {}
    if len(base):
        first = base.index[0]
        for tf in ALL_TIMEFRAMES:
            if tf.seconds <= base_tf.seconds:
                continue
            native = src.load(store, row, tf, None, first.to_pydatetime(), limit=FRAME_LIMITS[tf.value])
            native = native[(native.index + pd.Timedelta(seconds=tf.seconds)) <= first]
            if len(native):
                history[tf.value] = native
    return ReplayDataset(series_key=row["key"], source_id=row["source_id"], base_tf=base_tf, base=base, history=history,
                         note=f"monthly@1: analysis base {base_tf.value} (forced)")


def prepare(src: DataSources, store: str, row: Any, run_tf: Timeframe, start: datetime, end: datetime, *,
            lookback_days: int, detail: set[str], cfg: AnalysisConfig,
            progress: Callable[[int], None] | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    base_tf = Timeframe.M1 if run_tf is Timeframe.M1 else Timeframe.M5
    what = f"Research run on {row['key']} ({run_tf.value})"
    forward.check_range(start, end, what)            # every research job of this service passes here
    ds = load_dataset(src, store, row, base_tf, start, end, lookback_days)
    if ds.base.empty:
        raise MonthlyError(f"no {base_tf.value} analysis candles for {row['key']}")
    forward.check_dataset(ds, what)
    execution = src.load(store, row, run_tf, start - timedelta(seconds=run_tf.seconds), end)
    forward.check_frame(execution, f"{what}: execution candles")
    cutoffs = features.step_cutoffs(ds, run_tf, start, end)
    snaps = features.compute_timeline(ds, run_tf, cutoffs, cfg, detail=detail, progress=progress, cancelled=cancelled)
    in_month = execution[(execution.index >= pd.Timestamp(start)) & (execution.index < pd.Timestamp(end))]
    dataset = {"store": store, "series": row["key"], "source_id": row["source_id"], "run_timeframe": run_tf.value,
               "analysis_base": base_tf.value, "lookback_days": lookback_days, "detail": sorted(detail),
               "analysis_first": iso(ds.base.index[0].to_pydatetime()), "analysis_last": iso(ds.base.index[-1].to_pydatetime()),
               "analysis_candles": int(len(ds.base)), "history_before_base": {k: int(len(v)) for k, v in ds.history.items()},
               "execution_candles": int(len(in_month)), "execution_origin": "native",
               "execution_first": iso(in_month.index[0].to_pydatetime()) if len(in_month) else None,
               "execution_last": iso(in_month.index[-1].to_pydatetime()) if len(in_month) else None,
               "spread_known_share": round(float((in_month["spread"].fillna(0) > 0).mean()), 4) if len(in_month) else 0.0,
               "decision_cutoffs": len(cutoffs), "dataset_hash": features.dataset_hash(ds),
               "execution_hash": features.candle_hash(execution),
               "range": {"start": iso(start), "end": iso(end)}}
    return {"ds": ds, "execution": execution, "snapshots": snaps, "dataset": dataset}


def run_one(config: dict[str, Any], prepared: dict[str, Any], run_tf: Timeframe, start: datetime, end: datetime,
            assumptions: engine.Assumptions, spec: InstrumentSpec) -> dict[str, Any]:
    """One independent account: this strategy's decisions on this timeframe's candles only."""
    return engine.run(config, prepared["snapshots"], prepared["execution"], run_tf, start=start, end=end,
                      assumptions=assumptions, spec=spec, force_close_at_end=True, step_tf=run_tf)


# ------------------------------------------------------------------ research (strictly before the month)
def research(src: DataSources, store: str, row: Any, *, month_start: datetime, window_start: datetime,
             lookback_days: int, cfg: AnalysisConfig, assumptions: engine.Assumptions, spec: InstrumentSpec,
             progress: Callable[[int], None] | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Develop the candidates on [window_start, month_start): choose each concept's pre-declared RR variant on the
    development part (first 70 %), report it on the check part (last 30 %), freeze. No candle at or after the month
    start is loaded."""
    end = month_start
    if window_start >= end:
        raise MonthlyError("the research window must end at or before the month start")
    dev_end = window_start + (end - window_start) * RESEARCH_DEV_SHARE
    all_variants = {c["key"]: cand.variants(c) for c in cand.CONCEPTS}
    detail = set().union(*(builder.detail_timeframes(v["config"]) for vs in all_variants.values() for v in vs))
    prepared = prepare(src, store, row, Timeframe.M5, window_start, end, lookback_days=lookback_days, detail=detail,
                       cfg=cfg, progress=progress, cancelled=cancelled)
    last_loaded = max(prepared["ds"].base.index[-1], prepared["execution"].index[-1]) if len(prepared["execution"]) else \
        prepared["ds"].base.index[-1]
    if last_loaded + pd.Timedelta(minutes=5) > pd.Timestamp(month_start):
        raise MonthlyError("look-ahead guard: research data reached into the evaluation month")
    keys = ("trades", "wins", "losses", "net", "win_rate", "profit_factor", "expectancy_r", "max_drawdown")
    out = []
    for concept in cand.CONCEPTS:
        rows = []
        for v in all_variants[concept["key"]]:
            r = run_one(v["config"], prepared, Timeframe.M5, window_start, dev_end, assumptions, spec)
            m = metrics.compute(r["trades"], starting_balance=float(assumptions.starting_balance), min_trades=1,
                                closed_equity=r["closed_equity"])
            rows.append({"rr": v["rr"], "config": v["config"], "dev": {k: m.get(k) for k in keys}})
        qualified = [x for x in rows if x["dev"]["trades"] >= MIN_RESEARCH_TRADES and x["dev"]["expectancy_r"] is not None]
        if qualified:
            chosen = max(qualified, key=lambda x: (x["dev"]["expectancy_r"], -x["rr"]))
            chosen_by = (f"higher development expectancy ({chosen['dev']['expectancy_r']:+.3f} R over "
                         f"{chosen['dev']['trades']} trades)")
            flag = None
        else:
            chosen = next(x for x in rows if x["rr"] == 2.0)
            chosen_by = "pre-declared default 2.0 R (no variant reached 10 development trades)"
            flag = "insufficient research sample"
        chk = run_one(chosen["config"], prepared, Timeframe.M5, dev_end, end, assumptions, spec)
        cm = metrics.compute(chk["trades"], starting_balance=float(assumptions.starting_balance), min_trades=1,
                             closed_equity=chk["closed_equity"])
        config = dict(chosen["config"])
        out.append({"key": concept["key"], "concept": concept["concept"], "name": config["name"],
                    "version": cand.CANDIDATE_VERSION, "config": config, "fingerprint": builder.fingerprint(config),
                    "documentation": {**concept["documentation"], "conditions": builder.describe(config)},
                    "research": {"variants": [{"rr": x["rr"], "development": x["dev"]} for x in rows],
                                 "chosen_rr": chosen["rr"], "chosen_by": chosen_by, "flag": flag,
                                 "check": {k: cm.get(k) for k in keys}, "rr": cand.rr_check(config)}})
    return {"definition": VERSION, "window": {"start": iso(window_start), "development_end": iso(dev_end),
                                              "end": iso(end)},
            "cadence": "M5 decisions on M5 analysis and execution candles", "dataset": prepared["dataset"],
            "candidates": out,
            "note": "Developed and frozen on data strictly before the evaluation month; the month's results never "
                    "influenced these rules."}


def parse_iso(text: str) -> datetime:
    return parse_iso_aware(text)
