"""Research routes: backtests (bt@1), strategy comparisons and walk-forward validation (wf@1), run as
background jobs on the shared job runner, plus CSV / PDF exports. Hypothetical historical research only:
nothing here places orders or touches paper accounts or positions."""
from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from goldscalp.analysis.performance import STEP_TIMEFRAMES
from goldscalp.api.research_data import research_dataset, research_outcome_candles
from goldscalp.core import forward
from goldscalp.api.services import AppServices, clean, pick_series, services
from goldscalp.backtesting import engine, features, lab, walk_forward
from goldscalp.core.clock import iso, parse_iso_aware
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.gaps import gap_report
from goldscalp.reports import exports
from goldscalp.storage import performance_repo, research_repo, strategy_repo

router = APIRouter(prefix="/api/research")
MAX_STRATEGIES = 6


class StrategyRef(BaseModel):
    id: int = Field(ge=1)
    version: int | None = Field(None, ge=1)


class AssumptionsIn(BaseModel):
    configured_spread: str | None = None
    commission_per_lot_per_side: str | None = None
    slippage: str | None = None
    intrabar: str = Field("stop_first", pattern="^(stop_first|target_first)$")
    starting_balance: str | None = None
    min_trades_for_ratios: int = Field(30, ge=1, le=10000)
    leverage: int | None = Field(None, ge=1, le=1000)
    sizing_override: dict[str, Any] | None = None


class RunIn(BaseModel):
    strategies: list[StrategyRef] = Field(min_length=1, max_length=MAX_STRATEGIES)
    series: str = Field(min_length=1, max_length=200)
    start: str
    end: str
    lookback_days: int = Field(5, ge=1, le=20)
    assumptions: AssumptionsIn = Field(default_factory=AssumptionsIn)
    name: str | None = Field(None, max_length=120)


class WalkForwardIn(RunIn):
    mode: str = Field("fixed", pattern="^(fixed|select)$")
    objective: str = Field("expectancy_r", pattern="^(expectancy_r|net|profit_factor)$")
    min_dev_trades: int = Field(5, ge=1, le=1000)
    dev_days: float = Field(5, gt=0, le=365)
    eval_days: float = Field(2, gt=0, le=365)
    step_days: float | None = Field(None, gt=0, le=365)
    embargo_minutes: int | None = Field(None, ge=0, le=10080)
    anchored: bool = False


def _assumptions(gs: AppServices, body: AssumptionsIn) -> engine.Assumptions:
    costs = gs.paper_costs
    data = {"configured_spread": body.configured_spread or str(costs.configured_spread or "0.30"),
            "commission_per_lot_per_side": body.commission_per_lot_per_side or str(costs.commission_per_lot_per_side),
            "slippage": body.slippage or str(costs.slippage), "intrabar": body.intrabar,
            "starting_balance": body.starting_balance or gs.settings.paper_starting_balance,
            "min_trades_for_ratios": body.min_trades_for_ratios,
            "leverage": body.leverage or gs.settings.paper_leverage}
    if body.sizing_override is not None:
        override = dict(body.sizing_override)
        probe = {"name": "probe", "conditions": [{"type": "session"}], "risk": override}
        from goldscalp.strategies import builder
        try:
            data["sizing_override"] = builder.validate(probe)["risk"]
        except builder.StrategyError as exc:
            raise HTTPException(422, f"sizing_override: {exc}") from exc
    try:
        return engine.Assumptions.from_mapping(data)
    except engine.BacktestError as exc:
        raise HTTPException(422, str(exc)) from exc


def _strategies(gs: AppServices, refs: list[StrategyRef]) -> list[dict[str, Any]]:
    out = []
    with gs.db.read() as con:
        for ref in refs:
            record = strategy_repo.get(con, ref.id, ref.version)
            if record is None:
                raise HTTPException(404, f"unknown strategy {ref.id}" + (f" version {ref.version}" if ref.version else ""))
            out.append(record)
    return out


def _prepare(gs: AppServices, body: RunIn, records: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        start, end = parse_iso_aware(body.start), parse_iso_aware(body.end)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if end <= start:
        raise HTTPException(422, "the end must be after the start")
    configs = [r["config"] for r in records]
    steps = sorted({Timeframe.parse(c["evaluation"]["step_timeframe"]) for c in configs}, key=lambda t: t.seconds)
    row = pick_series(gs.db, body.series)
    ds = research_dataset(gs.db, row, steps[0], start, end, body.lookback_days, what="Strategy Lab run")
    if ds.base.empty:
        raise HTTPException(422, "the dataset has no closed candles in that range")
    if any(t.seconds < ds.base_tf.seconds for t in steps):
        raise HTTPException(422, f"this range's base timeframe is {ds.base_tf.value}; a strategy steps on "
                                 f"{steps[0].value} ({ds.note})")
    total = sum(len(features.step_cutoffs(ds, t, start, end)) for t in steps)
    if total == 0:
        raise HTTPException(422, "no complete step candles between the chosen start and end")
    limit = int(gs.analysis_cfg["performance"]["max_steps"])
    if total > limit:
        raise HTTPException(422, f"{total} steps exceed the limit of {limit}; choose a shorter range or a coarser "
                                 "step timeframe (the range is never shortened automatically)")
    extension = max(engine.holding_extension(c) for c in configs)
    candles = research_outcome_candles(gs.db, row, ds.base_tf, ds.base.index[0].to_pydatetime(),
                                       end + extension.to_pytimedelta(), what="Strategy Lab run")
    window = ds.base[(ds.base.index >= pd.Timestamp(start)) & (ds.base.index < pd.Timestamp(end))]
    coverage = {"base_tf": ds.base_tf.value, "base_choice": ds.note, "base_candles_in_range": int(len(window)),
                "first_candle": iso(window.index[0].to_pydatetime()) if len(window) else None,
                "last_candle": iso(window.index[-1].to_pydatetime()) if len(window) else None,
                "history_before_start": {k: int(len(v)) for k, v in ds.history.items()},
                "gaps": gap_report(window.index, ds.base_tf, gs.spec.schedule) if len(window) else None,
                "candles_after_end_for_open_positions": int((candles.index >= pd.Timestamp(end)).sum())}
    return {"start": start, "end": end, "row": row, "ds": ds, "candles": candles, "configs": configs, "total": total,
            "coverage": coverage}


def _meta(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": r["id"], "version": r["version"], "fingerprint": r["fingerprint"], "name": r["config"]["name"]}
            for r in records]


def _launch(gs: AppServices, kind: str, body: RunIn, records: list[dict[str, Any]], prep: dict[str, Any],
            work: Any, extra_params: dict[str, Any]) -> dict[str, Any]:
    if gs.perf_jobs.busy():
        raise HTTPException(409, "another evaluation is still running; wait for it or cancel it")
    assumptions = _assumptions(gs, body.assumptions)
    run_id = "rr_" + secrets.token_hex(8)
    created = gs.clock.now()
    params = {"series": prep["row"]["key"], "start": iso(prep["start"]), "end": iso(prep["end"]),
              "lookback_days": body.lookback_days, "strategies": _meta(records), "assumptions": assumptions.to_mapping(),
              **extra_params}
    with gs.db.transaction() as con:
        performance_repo.save_config(con, gs.analysis_cfg.fingerprint, gs.analysis_cfg.data, created)
        research_repo.create(con, run_id, kind=kind, name=body.name, created_at=created, series_key=prep["row"]["key"],
                             params=params, total=prep["total"], fingerprint=gs.analysis_cfg.fingerprint)

    def target(cancel: threading.Event) -> None:
        with gs.db.transaction() as con:
            research_repo.set_status(con, run_id, "running", at=gs.clock.now())

        def progress(done: int, total: int) -> None:
            with gs.db.transaction() as con:
                research_repo.set_progress(con, run_id, done)
        try:
            result = work(assumptions, progress, cancel.is_set)
            result.pop("_results", None)
            result["coverage"] = prep["coverage"]
            result = clean(result)
            with gs.db.transaction() as con:
                research_repo.set_progress(con, run_id, prep["total"])
                research_repo.set_status(con, run_id, "complete", at=gs.clock.now(), result=result)
        except features.Cancelled:
            with gs.db.transaction() as con:
                research_repo.set_status(con, run_id, "cancelled", at=gs.clock.now(), error="cancelled")
        except Exception as exc:  # noqa: BLE001 - a failed run is recorded, never crashes the server
            with gs.db.transaction() as con:
                research_repo.set_status(con, run_id, "failed", at=gs.clock.now(), error=f"{type(exc).__name__}: {exc}")
    try:
        gs.perf_jobs.start(run_id, target)
    except RuntimeError as exc:
        with gs.db.transaction() as con:
            research_repo.set_status(con, run_id, "failed", at=gs.clock.now(), error="another run started first")
        raise HTTPException(409, "another evaluation is still running") from exc
    with gs.db.read() as con:
        return {"run": research_repo.get(con, run_id, with_result=False), "disclaimer": lab.DISCLAIMER}


def _strategy_run(request: Request, body: RunIn, kind: str) -> dict[str, Any]:
    gs = services(request)
    records = _strategies(gs, body.strategies)
    prep = _prepare(gs, body, records)

    def work(assumptions: engine.Assumptions, progress: Any, cancelled: Any) -> dict[str, Any]:
        return lab.run_strategies(prep["ds"], prep["candles"], prep["configs"], prep["start"], prep["end"],
                                  gs.analysis_cfg, assumptions, gs.spec, meta=_meta(records), progress=progress,
                                  cancelled=cancelled)
    return _launch(gs, kind, body, records, prep, work, {})


@router.get("/forward-guard")
def forward_guard_status() -> dict[str, Any]:
    """The forward-data boundary and lock state (read-only)."""
    return forward.status()


@router.post("/backtests", status_code=202)
def create_backtest(request: Request, body: RunIn) -> dict[str, Any]:
    if len(body.strategies) != 1:
        raise HTTPException(422, "a backtest runs one strategy; use /api/research/comparisons for several")
    return _strategy_run(request, body, "backtest")


@router.post("/comparisons", status_code=202)
def create_comparison(request: Request, body: RunIn) -> dict[str, Any]:
    if len(body.strategies) < 2:
        raise HTTPException(422, "a comparison needs at least two strategies")
    return _strategy_run(request, body, "comparison")


@router.post("/walk-forward", status_code=202)
def create_walk_forward(request: Request, body: WalkForwardIn) -> dict[str, Any]:
    gs = services(request)
    records = _strategies(gs, body.strategies)
    if body.mode == "fixed" and len(records) != 1:
        raise HTTPException(422, "fixed mode evaluates exactly one strategy; select mode chooses among several")
    prep = _prepare(gs, body, records)
    configs = prep["configs"]
    embargo = timedelta(minutes=body.embargo_minutes if body.embargo_minutes is not None
                        else max(int(c["exit"]["max_holding_minutes"] or 60) for c in configs))
    try:
        folds = walk_forward.make_folds(prep["start"], prep["end"], dev=timedelta(days=body.dev_days),
                                        evaluation=timedelta(days=body.eval_days),
                                        step=timedelta(days=body.step_days or body.eval_days), embargo=embargo,
                                        anchored=body.anchored)
    except walk_forward.WalkForwardError as exc:
        raise HTTPException(422, str(exc)) from exc

    def work(assumptions: engine.Assumptions, progress: Any, cancelled: Any) -> dict[str, Any]:
        tls = lab.timelines_for(prep["ds"], configs, prep["start"], prep["end"], gs.analysis_cfg, progress=progress,
                                cancelled=cancelled)
        result = walk_forward.run(configs, tls, prep["candles"], prep["ds"].base_tf, folds, mode=body.mode,
                                  objective=body.objective, min_dev_trades=body.min_dev_trades,
                                  assumptions=assumptions, spec=gs.spec, names=[c["name"] for c in configs])
        result.update(range={"start": iso(prep["start"]), "end": iso(prep["end"])}, series=prep["ds"].series_key,
                      base_tf=prep["ds"].base_tf.value, dataset_hash=features.dataset_hash(prep["ds"]),
                      analysis_config=gs.analysis_cfg.fingerprint, assumptions=assumptions.to_mapping(),
                      strategies_meta=_meta(records), disclaimer=lab.DISCLAIMER, limitations=lab.LIMITATIONS + [
                          "Walk-forward windows start flat and force-close at their end; an embargo separates "
                          "development and evaluation windows."])
        return result
    return _launch(gs, "walk_forward", body, records, prep, work,
                   {"mode": body.mode, "objective": body.objective, "min_dev_trades": body.min_dev_trades,
                    "dev_days": body.dev_days, "eval_days": body.eval_days, "step_days": body.step_days or body.eval_days,
                    "embargo_minutes": int(embargo.total_seconds() // 60), "anchored": body.anchored,
                    "folds": len(folds)})


@router.get("/runs")
def list_runs(request: Request, kind: str | None = Query(None, pattern="^(backtest|comparison|walk_forward)$"),
              saved: bool | None = Query(None)) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        return {"runs": research_repo.list_runs(con, kind=kind, saved=saved), "busy": gs.perf_jobs.busy()}


def _run(gs: AppServices, run_id: str, *, complete: bool = False) -> dict[str, Any]:
    with gs.db.read() as con:
        run = research_repo.get(con, run_id)
    if run is None:
        raise HTTPException(404, f"unknown research run {run_id!r}")
    if complete and run["status"] != "complete":
        raise HTTPException(409, f"the run is {run['status']}, not complete")
    return run


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str) -> dict[str, Any]:
    return {"run": _run(services(request), run_id), "disclaimer": lab.DISCLAIMER}


class MetaIn(BaseModel):
    name: str | None = Field(None, max_length=120)
    saved: bool | None = None


@router.patch("/runs/{run_id}")
def update_run(request: Request, run_id: str, body: MetaIn) -> dict[str, Any]:
    gs = services(request)
    with gs.db.transaction() as con:
        if not research_repo.update_meta(con, run_id, name=body.name, saved=body.saved):
            raise HTTPException(404, f"unknown research run {run_id!r}")
    with gs.db.read() as con:
        return {"run": research_repo.get(con, run_id, with_result=False)}


@router.post("/runs/{run_id}/cancel")
def cancel_run(request: Request, run_id: str) -> dict[str, Any]:
    if not services(request).perf_jobs.cancel(run_id):
        raise HTTPException(409, "this run is not running")
    return {"cancelling": run_id}


@router.delete("/runs/{run_id}")
def delete_run(request: Request, run_id: str) -> dict[str, Any]:
    gs = services(request)
    if gs.perf_jobs.busy() == run_id:
        raise HTTPException(409, "cancel the run before deleting it")
    with gs.db.transaction() as con:
        if not research_repo.delete(con, run_id):
            raise HTTPException(404, f"unknown research run {run_id!r}")
    return {"deleted": run_id}


def _download(content: str | bytes, media: str, name: str) -> Response:
    cls = PlainTextResponse if isinstance(content, str) else Response
    return cls(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/runs/{run_id}/trades.csv")
def export_trades(request: Request, run_id: str) -> Response:
    return _download(exports.trades_csv(_run(services(request), run_id, complete=True)), "text/csv",
                     f"goldscalp-{run_id}-trades.csv")


@router.get("/runs/{run_id}/summary.csv")
def export_summary(request: Request, run_id: str) -> Response:
    return _download(exports.summary_csv(_run(services(request), run_id, complete=True)), "text/csv",
                     f"goldscalp-{run_id}-summary.csv")


@router.get("/runs/{run_id}/report.pdf")
def export_pdf(request: Request, run_id: str) -> Response:
    return _download(exports.report_pdf(_run(services(request), run_id, complete=True)), "application/pdf",
                     f"goldscalp-{run_id}-report.pdf")


@router.get("/defaults")
def defaults(request: Request) -> dict[str, Any]:
    gs = services(request)
    return {"assumptions": {"configured_spread": str(gs.paper_costs.configured_spread or "0.30"),
                            "commission_per_lot_per_side": str(gs.paper_costs.commission_per_lot_per_side),
                            "slippage": str(gs.paper_costs.slippage), "intrabar": "stop_first",
                            "starting_balance": gs.settings.paper_starting_balance, "min_trades_for_ratios": 30,
                            "leverage": gs.settings.paper_leverage},
            "step_timeframes": list(STEP_TIMEFRAMES) + ["H1"], "max_steps": gs.analysis_cfg["performance"]["max_steps"],
            "busy": gs.perf_jobs.busy(), "disclaimer": lab.DISCLAIMER, "limitations": lab.LIMITATIONS,
            "now": iso(datetime.now().astimezone())}
