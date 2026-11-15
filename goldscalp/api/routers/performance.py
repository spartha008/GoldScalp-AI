"""Historical signal performance evaluation routes (perf@1). Runs execute in ONE background thread at a
time; results are stored in ``performance_runs`` and the signals they found in ``signals``.
Nothing here places orders or touches paper accounts or positions."""
from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from goldscalp.analysis import performance
from goldscalp.api.jobs import JobRunner
from goldscalp.api.research_data import research_dataset, research_outcome_candles
from goldscalp.api.services import AppServices, clean, pick_series, services
from goldscalp.core.clock import iso, parse_iso_aware
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.gaps import gap_report
from goldscalp.simulation.costs import CostModel
from goldscalp.storage import performance_repo, signal_repo

router = APIRouter(prefix="/api/performance")
OUTCOME_EXTENSION = timedelta(days=2)     # candles after `end` used ONLY to measure outcomes


class RunIn(BaseModel):
    series: str = Field(min_length=1, max_length=200)
    timeframes: list[str] = Field(default_factory=lambda: ["M5"], min_length=1, max_length=3)
    start: str
    end: str
    lookback_days: int = Field(5, ge=1, le=20)
    horizon_m5: int | None = Field(None, ge=1, le=288)
    target_r: float | None = Field(None, ge=0, le=10)
    stop_atr_fallback: float | None = Field(None, ge=0, le=10)
    flat_band_atr: float | None = Field(None, ge=0, le=5)
    lots: str | None = None
    spread: str | None = None
    commission_per_lot_per_side: str | None = None
    slippage: str | None = None


PerformanceJobs = JobRunner   # Phase 2 name, kept for compatibility


def _dec(value: str | None, name: str, fallback: Decimal | None) -> Decimal | None:
    if value is None or value == "":
        return fallback
    try:
        out = Decimal(str(value))
    except InvalidOperation as exc:
        raise HTTPException(422, f"{name} must be a number") from exc
    if not out.is_finite() or out < 0:
        raise HTTPException(422, f"{name} must be a non-negative number")
    return out


def _execute(gs: AppServices, run_id: str, ds: Any, outcome: pd.DataFrame, timeframes: list[Timeframe],
             start: datetime, end: datetime, params: dict[str, Any], costs: CostModel, created_at: datetime,
             coverage: dict[str, Any], cancel: threading.Event) -> None:
    db = gs.db
    with db.transaction() as con:
        performance_repo.set_status(con, run_id, "running", at=gs.clock.now())

    def progress(done: int, total: int) -> None:
        with db.transaction() as con:
            performance_repo.set_progress(con, run_id, done)

    try:
        result = performance.evaluate(ds, outcome, timeframes, start, end, gs.analysis_cfg, params, gs.spec, costs,
                                      run_id=run_id, generated_at=created_at, progress=progress,
                                      cancelled=cancel.is_set)
        items = result.pop("_items")
        result["coverage"] = coverage
        result = clean(result)
        with db.transaction() as con:
            for item in items:
                payload = clean(item["signal"])
                evaluation = clean({"performance_run": run_id, "analytical": item["analytical"],
                                    "trade": {k: v for k, v in item["trade"].items() if not k.startswith("_")},
                                    "session": item["session"], "volatility": item["volatility"]})
                signal_repo.insert_evaluated(con, payload, evaluation)
            performance_repo.set_progress(con, run_id, result["steps"])
            performance_repo.set_status(con, run_id, "complete", at=gs.clock.now(), result=result)
    except performance.EvaluationError as exc:
        status = "cancelled" if str(exc) == "cancelled" else "failed"
        with db.transaction() as con:
            performance_repo.set_status(con, run_id, status, at=gs.clock.now(), error=str(exc))
    except Exception as exc:  # noqa: BLE001 - a failed run is recorded, never crashes the server
        with db.transaction() as con:
            performance_repo.set_status(con, run_id, "failed", at=gs.clock.now(), error=f"{type(exc).__name__}: {exc}")


@router.get("/defaults")
def defaults(request: Request) -> dict[str, Any]:
    gs = services(request)
    spec = gs.spec
    return {"definition": performance.VERSION, "config": gs.analysis_cfg["performance"],
            "costs": gs.paper_costs.to_mapping(), "step_timeframes": list(performance.STEP_TIMEFRAMES),
            "instrument": {"name": spec.name, "contract_size": str(spec.contract_size), "unit": spec.unit,
                           "min_lot": str(spec.min_lot), "lot_step": str(spec.lot_step), "max_lot": str(spec.max_lot),
                           "is_hypothetical": spec.is_hypothetical},
            "busy": gs.perf_jobs.busy(), "disclaimer": performance.DISCLAIMER}


@router.post("/runs", status_code=202)
def create_run(request: Request, body: RunIn) -> dict[str, Any]:
    gs = services(request)
    try:
        timeframes = sorted({Timeframe.parse(t) for t in body.timeframes}, key=lambda t: t.seconds)
        start, end = parse_iso_aware(body.start), parse_iso_aware(body.end)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if any(tf.value not in performance.STEP_TIMEFRAMES for tf in timeframes):
        raise HTTPException(422, f"step timeframes must be among {list(performance.STEP_TIMEFRAMES)}")
    if end <= start:
        raise HTTPException(422, "the end must be after the start")
    if gs.perf_jobs.busy():
        raise HTTPException(409, "another evaluation is still running; wait for it or cancel it")
    pcfg = gs.analysis_cfg["performance"]
    params: dict[str, Any] = dict(pcfg)
    for key in ("horizon_m5", "target_r", "stop_atr_fallback", "flat_band_atr"):
        value = getattr(body, key)
        if value is not None:
            params[key] = value
    base_costs = gs.paper_costs
    lots = _dec(body.lots, "lots", Decimal(str(pcfg["lots"])))
    try:
        gs.spec.validate_lots(lots)
        costs = CostModel.from_mapping({
            "spread_mode": "market_else_configured",
            "configured_spread": str(_dec(body.spread, "spread", base_costs.configured_spread or Decimal("0.30"))),
            "commission_per_lot_per_side": str(_dec(body.commission_per_lot_per_side, "commission",
                                                    base_costs.commission_per_lot_per_side)),
            "slippage": str(_dec(body.slippage, "slippage", base_costs.slippage))})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    params["lots"] = str(lots)
    row = pick_series(gs.db, body.series)
    ds = research_dataset(gs.db, row, timeframes[0], start, end, body.lookback_days, what="Performance run")
    if ds.base.empty:
        raise HTTPException(422, "the dataset has no closed candles in that range")
    total = sum(len(performance.step_cutoffs(ds, tf, start, end)) for tf in timeframes)
    if total == 0:
        raise HTTPException(422, "no complete step candles between the chosen start and end")
    if total > int(params["max_steps"]):
        raise HTTPException(422, f"{total} steps exceed the limit of {params['max_steps']}; choose a shorter range or "
                                 "a coarser step timeframe (the range is never shortened automatically)")
    outcome = research_outcome_candles(gs.db, row, ds.base_tf, ds.base.index[0].to_pydatetime(), end + OUTCOME_EXTENSION,
                                       what="Performance run")
    window = ds.base[(ds.base.index >= pd.Timestamp(start)) & (ds.base.index < pd.Timestamp(end))]
    coverage = {"base_tf": ds.base_tf.value, "base_candles_in_range": int(len(window)),
                "first_candle": iso(window.index[0].to_pydatetime()) if len(window) else None,
                "last_candle": iso(window.index[-1].to_pydatetime()) if len(window) else None,
                "history_before_start": {k: int(len(v)) for k, v in ds.history.items()},
                "gaps": gap_report(window.index, ds.base_tf, gs.spec.schedule) if len(window) else None,
                "outcome_candles_after_end": int((outcome.index >= pd.Timestamp(end)).sum())}
    run_id = "pr_" + secrets.token_hex(8)
    created = gs.clock.now()
    run_params = {**params, "series": row["key"], "timeframes": [t.value for t in timeframes], "start": iso(start),
                  "end": iso(end), "lookback_days": body.lookback_days, "costs": costs.to_mapping()}
    with gs.db.transaction() as con:
        performance_repo.save_config(con, gs.analysis_cfg.fingerprint, gs.analysis_cfg.data, created)
        performance_repo.create_run(con, run_id, created_at=created, series_key=row["key"], params=run_params,
                                    total=total, fingerprint=gs.analysis_cfg.fingerprint)
    try:
        gs.perf_jobs.start(run_id, lambda cancel: _execute(gs, run_id, ds, outcome, timeframes, start, end, params,
                                                           costs, created, coverage, cancel))
    except RuntimeError as exc:
        with gs.db.transaction() as con:
            performance_repo.set_status(con, run_id, "failed", at=gs.clock.now(), error="another run started first")
        raise HTTPException(409, "another evaluation is still running") from exc
    with gs.db.read() as con:
        return {"run": performance_repo.get_run(con, run_id, with_result=False), "disclaimer": performance.DISCLAIMER}


@router.get("/runs")
def list_runs(request: Request) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        return {"runs": performance_repo.list_runs(con), "busy": gs.perf_jobs.busy()}


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        run = performance_repo.get_run(con, run_id)
        config = performance_repo.get_config(con, run["config_fingerprint"]) if run else None
    if run is None:
        raise HTTPException(404, f"unknown performance run {run_id!r}")
    return {"run": run, "config": config, "disclaimer": performance.DISCLAIMER}


@router.post("/runs/{run_id}/cancel")
def cancel_run(request: Request, run_id: str) -> dict[str, Any]:
    gs = services(request)
    if not gs.perf_jobs.cancel(run_id):
        raise HTTPException(409, "this run is not running")
    return {"cancelling": run_id}


@router.delete("/runs/{run_id}")
def delete_run(request: Request, run_id: str) -> dict[str, Any]:
    gs = services(request)
    if gs.perf_jobs.busy() == run_id:
        raise HTTPException(409, "cancel the run before deleting it")
    with gs.db.transaction() as con:
        removed, signals_deleted = performance_repo.delete_run(con, run_id)
    if not removed:
        raise HTTPException(404, f"unknown performance run {run_id!r}")
    return {"deleted": run_id, "signals_deleted": signals_deleted}


@router.get("/configs/{fingerprint}")
def get_config(request: Request, fingerprint: str) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        config = performance_repo.get_config(con, fingerprint)
    if config is None:
        raise HTTPException(404, "unknown configuration fingerprint")
    return config
