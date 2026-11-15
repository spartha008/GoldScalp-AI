"""Research orchestration: backtests and comparisons share ONE feature timeline per step timeframe, so
every strategy in a run sees identical inputs and identical run-level assumptions."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import pandas as pd

from goldscalp.analysis.config import AnalysisConfig
from goldscalp.analysis.replay import ReplayDataset
from goldscalp.backtesting import engine, features, metrics
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.strategies import builder

DISCLAIMER = ("Hypothetical historical research: simulated outcomes on past data under the stated assumptions. "
              "Not trading results, no orders were placed, and past behaviour does not predict future performance.")
LIMITATIONS = [
    "Fills are modelled on candle prices (bid candles; ask = bid + the candle's stored spread, else the configured "
    "spread). Real fills, requotes and broker stop levels may differ.",
    "When a candle touches both the stop and the target, the order is unknown; the intrabar rule decides and such "
    "trades are flagged as ambiguous.",
    "Swap / financing, partial fills and spread widening inside a candle are not modelled. Margin is checked at "
    "entry (ounces x price / leverage) like the paper simulator; margin calls are not modelled.",
    "Decisions use only candles closed by each step cut-off; a gap in the data can delay or cancel an entry.",
    "Small samples say little about how the rules behave; ratio metrics are withheld below the minimum trade count.",
]


def timelines_for(ds: ReplayDataset, configs: list[dict[str, Any]], start: datetime, end: datetime,
                  cfg: AnalysisConfig, *, progress: Callable[[int, int], None] | None = None,
                  cancelled: Callable[[], bool] | None = None) -> dict[str, dict[str, Any]]:
    """{step timeframe: {"cutoffs": [...], "snapshots": [...]}} for every step timeframe the configs use."""
    plans = {}
    for c in configs:
        tf = c["evaluation"]["step_timeframe"]
        plans.setdefault(tf, set()).update(builder.detail_timeframes(c))
    cut_sets = {tf: features.step_cutoffs(ds, Timeframe.parse(tf), start, end) for tf in plans}
    total = sum(len(v) for v in cut_sets.values())
    done = 0
    out = {}
    for tf, detail in plans.items():
        cutoffs = cut_sets[tf]

        def tick(n: int, offset: int = done) -> None:
            if progress is not None and (n % 10 == 0 or n == total):
                progress(offset + n, total)
        snaps = features.compute_timeline(ds, Timeframe.parse(tf), cutoffs, cfg, detail=detail, progress=tick,
                                          cancelled=cancelled) if cutoffs else []
        done += len(cutoffs)
        out[tf] = {"cutoffs": cutoffs, "snapshots": snaps}
    return out


def backtest_one(config: dict[str, Any], timeline: dict[str, Any], candles: pd.DataFrame, base_tf: Timeframe, *,
                 start: datetime, end: datetime, assumptions: engine.Assumptions, spec: InstrumentSpec,
                 force_close_at_end: bool = False) -> dict[str, Any]:
    result = engine.run(config, timeline["snapshots"], candles, base_tf, start=start, end=end, assumptions=assumptions,
                        spec=spec, force_close_at_end=force_close_at_end)
    result["metrics"] = metrics.full(result, starting_balance=float(assumptions.starting_balance),
                                     min_trades=assumptions.min_trades_for_ratios)
    return result


def run_strategies(ds: ReplayDataset, candles: pd.DataFrame, configs: list[dict[str, Any]], start: datetime,
                   end: datetime, cfg: AnalysisConfig, assumptions: engine.Assumptions, spec: InstrumentSpec, *,
                   meta: list[dict[str, Any]] | None = None, progress: Callable[[int, int], None] | None = None,
                   cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Backtest (one config) or comparison (several), under identical assumptions."""
    tls = timelines_for(ds, configs, start, end, cfg, progress=progress, cancelled=cancelled)
    results = []
    for i, c in enumerate(configs):
        tl = tls[c["evaluation"]["step_timeframe"]]
        r = backtest_one(c, tl, candles, ds.base_tf, start=start, end=end, assumptions=assumptions, spec=spec)
        r["strategy"].update((meta or [{}] * len(configs))[i])
        r["config"] = c
        r["conditions_text"] = builder.describe(c)
        r["steps"] = len(tl["cutoffs"])
        results.append(r)
    comparison = [{"strategy": r["strategy"], "step_timeframe": r["config"]["evaluation"]["step_timeframe"],
                   "conditions": r["conditions_text"], "steps": r["steps"],
                   **{k: r["metrics"]["summary"].get(k) for k in (
                       "trades", "wins", "losses", "net", "win_rate", "profit_factor", "expectancy_r",
                       "average_outcome", "max_drawdown", "max_drawdown_pct", "max_drawdown_marked", "mfe_avg",
                       "mae_avg", "mfe_r_avg", "mae_r_avg", "average_holding_minutes", "sufficient", "reason",
                       "ambiguous_bar_trades")}}
                  for r in results]
    return {"definition": engine.VERSION, "range": {"start": iso(start), "end": iso(end)},
            "series": ds.series_key, "base_tf": ds.base_tf.value, "dataset_hash": features.dataset_hash(ds),
            "analysis_config": cfg.fingerprint, "assumptions": assumptions.to_mapping(),
            "strategies": [{k: v for k, v in r.items() if not k.startswith("_")} for r in results],
            "comparison": comparison, "disclaimer": DISCLAIMER, "limitations": LIMITATIONS,
            "_results": results}
