"""Monthly backtest API (/api/monthly/*) and jobs. Served by the separate research service (standalone.py).

Workflow: select month -> availability -> research (data before the month) -> review candidates -> freeze
(A/B/C exact versions + frozen candidates) -> review risk and TP -> run (nine runs + candidate runs) -> reports ->
exports. Historical and simulation-only: nothing here reaches a broker, the paper simulator or the application
database (read-only)."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from goldscalp.analysis.config import AnalysisConfig
from goldscalp.api.jobs import JobRunner
from goldscalp.backtesting import engine, features
from goldscalp.core import forward
from goldscalp.core.clock import Clock, iso
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.ingestion import ImportRefused, import_csv
from goldscalp.data.providers.csv_file import CsvFormatError
from goldscalp.monthly import closure, discovery, discovery_report, importing, report, risk, robustness, runner
from goldscalp.monthly import sealed, sealed_report, validation, validation_report
from goldscalp.monthly import store as st
from goldscalp.monthly.data import RUN_TIMEFRAMES, DataSources, availability, default_month, month_range
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.storage import candle_repo
from goldscalp.storage.candle_repo import SeriesConflict
from goldscalp.storage.db import Database
from goldscalp.strategies import builder

router = APIRouter(prefix="/api/monthly")
DEFAULT_SERIES = "live:mt5:XAUUSD"


@dataclass
class MonthlyServices:
    sources: DataSources
    store: Database
    clock: Clock
    app_spec: InstrumentSpec
    analysis_cfg: AnalysisConfig
    defaults: dict[str, Any]
    jobs: JobRunner = field(default_factory=JobRunner)


def ms_of(request: Request) -> MonthlyServices:
    return request.app.state.monthly


def _key(name: str, sid: int) -> str:
    m = re.match(r"^Example ([A-Z])\b", name)
    return m.group(1) if m else f"S{sid}"


def _eval(ms: MonthlyServices, eid: str) -> dict[str, Any]:
    with ms.store.read() as con:
        ev = st.evaluation(con, eid)
    if ev is None:
        raise HTTPException(404, f"unknown evaluation {eid}")
    return ev


def _series_default(ms: MonthlyServices) -> tuple[str, str]:
    series = ms.sources.series()
    if any(s["key"] == DEFAULT_SERIES and s["store"] == "app" for s in series):
        return "app", DEFAULT_SERIES
    if series:
        best = max(series, key=lambda s: s["timeframes"].get("M5", {}).get("bars", 0))
        return best["store"], best["key"]
    return "app", DEFAULT_SERIES


# ------------------------------------------------------------------ setup views
@router.get("/defaults")
def defaults(request: Request) -> dict[str, Any]:
    ms = ms_of(request)
    store, key = _series_default(ms)
    d = ms.defaults
    dm = default_month(ms.sources, store, key, ms.app_spec.schedule, ms.clock.now(), research_days=d["research_days"],
                       lookback_days=d["lookback_days"])
    strategies = []
    for sid, version in ms.sources.default_versions():
        rec = ms.sources.strategy(sid, version)
        if rec:
            strategies.append({"id": sid, "version": version, "key": _key(rec["name"], sid), "name": rec["name"],
                               "fingerprint": rec["fingerprint"], "readiness": runner.readiness(rec["config"], d)})
    return {"store": store, "series": key, "default_month": dm["month"], "skipped_months": dm["skipped"],
            "assumptions": {**d, "starting_balance": runner.STARTING_BALANCE, "leverage": runner.LEVERAGE,
                            "timeframes": list(RUN_TIMEFRAMES), "instrument": "XAUUSD",
                            "execution_model": "historical candle-based simulation (bt@1)", "mode": "backtest only"},
            "contract": runner.contract_spec(ms.app_spec, ms.sources.verified_spec())[1],
            "strategies": strategies, "series_list": ms.sources.series(),
            "risk_scenarios": list(runner.RISK_SCENARIOS), "risk_banner": runner.RISK_BANNER,
            "import_defaults": {"instrument": "XAUUSD", "timeframe": "M1", "dialect": "mt5_export",
                                "time_basis": "rule:ny_close", "point": "0.01", "series_label": "vantage-mt5-xauusd",
                                "label": "Vantage XAUUSD M1 October 2026", "month": "2026-10",
                                "required_period": "26 September - 1 November 2026", "store": "research"},
            "label": "Historical, simulation-only research. No orders are placed and nothing reaches MT5."}


@router.get("/forward-guard")
def forward_guard_status() -> dict[str, Any]:
    """The forward-data boundary and lock state (read-only)."""
    return forward.status()


@router.get("/availability")
def get_availability(request: Request, month: str, store: str = "app", series: str = DEFAULT_SERIES) -> dict[str, Any]:
    ms = ms_of(request)
    try:
        return availability(ms.sources, store, series, month, ms.app_spec.schedule,
                            research_days=ms.defaults["research_days"], lookback_days=ms.defaults["lookback_days"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/strategies")
def strategies(request: Request) -> dict[str, Any]:
    ms = ms_of(request)
    return {"strategies": [{"id": s["id"], "version": s["version"], "name": s["name"], "key": _key(s["name"], s["id"]),
                            "fingerprint": s["fingerprint"], "readiness": runner.readiness(s["config"], ms.defaults)}
                           for s in ms.sources.strategies()]}


# ------------------------------------------------------------------ evaluations
class StrategyRef(BaseModel):
    id: int
    version: int = Field(ge=1)


class EvaluationIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    store: str = Field("app", pattern="^(app|research)$")
    series: str = Field(DEFAULT_SERIES, max_length=200)
    strategies: list[StrategyRef] | None = Field(None, max_length=6)
    research_days: int = Field(60, ge=1, le=180)      # fewer than MIN_RESEARCH_DAYS of data is refused at research time
    lookback_days: int = Field(5, ge=2, le=20)
    configured_spread: str = Field("0.30", max_length=12)
    commission: str = Field("3.50", max_length=12)
    slippage: str = Field("0.05", max_length=12)
    risk_percent_override: float | None = Field(None, gt=0, le=5)


@router.post("/evaluations", status_code=201)
def create_evaluation(request: Request, body: EvaluationIn) -> dict[str, Any]:
    ms = ms_of(request)
    try:
        start, end = month_range(body.month)
        for v in (body.configured_spread, body.commission, body.slippage):
            if Decimal(v) < 0:
                raise ValueError("costs must not be negative")
    except (ValueError, InvalidOperation) as exc:
        raise HTTPException(422, str(exc)) from exc
    if end > ms.clock.now():
        raise HTTPException(422, f"{body.month} is not a completed calendar month yet")
    forward.check_range(start, end, f"Monthly evaluation of {body.month}")
    if start < discovery.SEALED[1] and end > discovery.SEALED[0]:
        raise HTTPException(409, f"{body.month} lies in the Phase 6 SEALED block ({discovery.SEALED_EVAL[0]:%b %Y} to "
                                 f"{discovery.SEALED_EVAL[1]:%b %Y}): it is opened only for the frozen validation of a "
                                 "Phase 6 hypothesis, never for an evaluation")
    refs = [(s.id, s.version) for s in body.strategies] if body.strategies else ms.sources.default_versions()
    if not refs:
        raise HTTPException(422, "no strategy versions to evaluate")
    params = {**body.model_dump(exclude={"strategies"}), "strategies": [{"id": i, "version": v} for i, v in refs],
              "starting_balance": runner.STARTING_BALANCE, "leverage": runner.LEVERAGE,
              "timeframes": list(RUN_TIMEFRAMES)}
    av = availability(ms.sources, body.store, body.series, body.month, ms.app_spec.schedule,
                      research_days=body.research_days, lookback_days=body.lookback_days)
    eid = "me_" + secrets.token_hex(6)
    with ms.store.transaction() as con:
        st.create_evaluation(con, eid, month=body.month, start=start, end=end, params=params, availability=av,
                             app_version=runner.app_version(), now=ms.clock.now())
    return _detail(ms, eid)


@router.get("/evaluations")
def list_evaluations(request: Request) -> dict[str, Any]:
    ms = ms_of(request)
    with ms.store.read() as con:
        return {"evaluations": st.evaluations(con)}


def _detail(ms: MonthlyServices, eid: str) -> dict[str, Any]:
    with ms.store.read() as con:
        ev = st.evaluation(con, eid)
        if ev is None:
            raise HTTPException(404, f"unknown evaluation {eid}")
        ev["candidates"] = st.candidates(con, eid)
        ev["runs"] = st.runs(con, eid)
    ev["running_job"] = ms.jobs.busy() == eid
    return ev


@router.get("/evaluations/{eid}")
def get_evaluation(request: Request, eid: str) -> dict[str, Any]:
    return _detail(ms_of(request), eid)


def _start_job(ms: MonthlyServices, eid: str, status: str, work: Any) -> None:
    if ms.jobs.busy():
        raise HTTPException(409, "another monthly job is running; wait for it or cancel it")

    def target(cancel: threading.Event) -> None:
        try:
            work(cancel)
        except features.Cancelled:
            with ms.store.transaction() as con:
                st.update_evaluation(con, eid, ms.clock.now(), status="FAILED", error="cancelled", phase=None)
        except Exception as exc:  # noqa: BLE001 - a failed job is recorded, never crashes the service
            with ms.store.transaction() as con:
                st.update_evaluation(con, eid, ms.clock.now(), status="FAILED", error=f"{type(exc).__name__}: {exc}",
                                     phase=None, finished_at=iso(ms.clock.now()))
    with ms.store.transaction() as con:
        st.update_evaluation(con, eid, ms.clock.now(), status=status, error=None, progress=0, total=0)
    try:
        ms.jobs.start(eid, target)
    except RuntimeError as exc:
        raise HTTPException(409, "another monthly job is running") from exc


def _progress(ms: MonthlyServices, eid: str, phase: str, total: int) -> Any:
    def tick(n: int) -> None:
        with ms.store.transaction() as con:
            st.update_evaluation(con, eid, ms.clock.now(), phase=phase, progress=n, total=total)
    return tick


@router.post("/evaluations/{eid}/research", status_code=202)
def start_research(request: Request, eid: str) -> dict[str, Any]:
    ms = ms_of(request)
    ev = _eval(ms, eid)
    if ev["status"] not in ("NOT STARTED", "FAILED") or ev["frozen"]:
        raise HTTPException(409, f"research can run only before freezing (status {ev['status']})")
    av = ev["availability"]
    if not (av.get("research") or {}).get("sufficient"):
        raise HTTPException(409, "research needs data before the month: " + str((av.get("research") or {}).get("reason")))
    p = ev["params"]
    start, _ = month_range(ev["month"])
    window_start = runner.parse_iso(av["research"]["window"]["start"])
    spec, _info = runner.contract_spec(ms.app_spec, ms.sources.verified_spec())

    def work(cancel: threading.Event) -> None:
        row = ms.sources.series_row(p["store"], p["series"])
        res = runner.research(ms.sources, p["store"], row, month_start=start, window_start=window_start,
                              lookback_days=p["lookback_days"], cfg=ms.analysis_cfg,
                              assumptions=runner.make_assumptions(p), spec=spec,
                              progress=_progress(ms, eid, "research timeline (M5, before the month)", 0),
                              cancelled=cancel.is_set)
        now = ms.clock.now()
        with ms.store.transaction() as con:
            con.execute("DELETE FROM candidates WHERE evaluation_id = ?", (eid,))
            for c in res["candidates"]:
                st.insert_candidate(con, f"{eid}_{c['key']}", eid, c, now)
            st.update_evaluation(con, eid, now, status="NOT STARTED", research_done=1, phase=None,
                                 research={k: v for k, v in res.items() if k != "candidates"})
    _start_job(ms, eid, "RESEARCHING", work)
    return _detail(ms, eid)


@router.post("/evaluations/{eid}/freeze")
def freeze(request: Request, eid: str) -> dict[str, Any]:
    """Freeze the exact strategy versions (read from the application database, fingerprints re-checked) and the
    research candidates. Nothing is written to the application database."""
    ms = ms_of(request)
    ev = _eval(ms, eid)
    if ev["status"] != "NOT STARTED" or not ev["research_done"]:
        raise HTTPException(409, "freeze after the research stage completed (and before running)")
    p = ev["params"]
    frozen, problems = [], []
    for ref in p["strategies"]:
        rec = ms.sources.strategy(ref["id"], ref["version"])
        if rec is None:
            problems.append(f"strategy {ref['id']} v{ref['version']} does not exist")
            continue
        if builder.fingerprint(rec["config"]) != rec["fingerprint"]:
            problems.append(f"strategy {ref['id']} v{ref['version']}: stored fingerprint does not match its configuration")
            continue
        ready = runner.readiness(rec["config"], p)
        frozen.append({"key": _key(rec["name"], rec["id"]), "strategy_id": rec["id"], "version": rec["version"],
                       "name": rec["name"], "fingerprint": rec["fingerprint"], "config": rec["config"],
                       "version_created_at": rec["version_created_at"], "readiness": ready})
        if not ready["ready"]:
            problems.append(f"{rec['name']} v{rec['version']}: NOT READY - {ready.get('reason')}")
    if problems:
        raise HTTPException(409, "; ".join(problems))
    with ms.store.read() as con:
        cands = st.candidates(con, eid)
    now = ms.clock.now()
    assumptions = runner.make_assumptions(p)
    spec, contract = runner.contract_spec(ms.app_spec, ms.sources.verified_spec())
    stored_assumptions = {"engine": assumptions.to_mapping(), "spec": spec.to_mapping(), "contract": contract}
    with ms.store.transaction() as con:
        con.execute("DELETE FROM runs WHERE evaluation_id = ?", (eid,))
        items = [(f["key"], "existing", f["config"], f["fingerprint"]) for f in frozen] + \
                [(c["key"], "candidate", c["config"], c["fingerprint"]) for c in cands]
        for key, kind, config, fp in items:
            for tf in RUN_TIMEFRAMES:
                st.upsert_run(con, {"id": f"{eid}_{key}_{tf}", "evaluation_id": eid, "strategy_key": key, "kind": kind,
                                    "timeframe": tf, "status": "NOT STARTED", "config": config, "fingerprint": fp,
                                    "assumptions": stored_assumptions})
        st.update_evaluation(con, eid, now, status="READY",
                             frozen={"at": iso(now), "strategies": frozen, "candidates": [c["key"] for c in cands],
                                     "assumptions": stored_assumptions})
    return _detail(ms, eid)


@router.post("/evaluations/{eid}/run", status_code=202)
def start_runs(request: Request, eid: str) -> dict[str, Any]:
    ms = ms_of(request)
    ev = _eval(ms, eid)
    if ev["status"] != "READY":
        raise HTTPException(409, f"runs start from READY (status {ev['status']})")
    if ev["params"].get("risk_study"):
        raise HTTPException(409, f"this is a risk-scenario evaluation; run it from its risk study {ev['params']['risk_study']}")
    p = ev["params"]
    start, end = month_range(ev["month"])

    def work(cancel: threading.Event) -> None:
        av = availability(ms.sources, p["store"], p["series"], ev["month"], ms.app_spec.schedule,
                          research_days=p["research_days"], lookback_days=p["lookback_days"])
        with ms.store.read() as con:
            planned = st.runs(con, eid, with_result=False)
        row = ms.sources.series_row(p["store"], p["series"])
        for tf in RUN_TIMEFRAMES:
            mine = [r for r in planned if r["timeframe"] == tf]
            tf_av = av["timeframes"][tf]
            if not tf_av["sufficient"]:
                with ms.store.transaction() as con:
                    for r in mine:
                        st.upsert_run(con, {**r, "status": "INCOMPLETE", "reason": "; ".join(tf_av["problems"]),
                                            "dataset": None, "result": None})
                continue
            run_tf = Timeframe.parse(tf)
            detail = set().union(*(builder.detail_timeframes(r["config"]) for r in mine))
            prepared = runner.prepare(ms.sources, p["store"], row, run_tf, start, end, lookback_days=p["lookback_days"],
                                      detail=detail, cfg=ms.analysis_cfg,
                                      progress=_progress(ms, eid, f"{tf} timeline", 0), cancelled=cancel.is_set)
            for r in mine:
                started = iso(ms.clock.now())
                a = r["assumptions"]
                res = runner.run_one(r["config"], prepared, run_tf, start, end,
                                     engine.Assumptions.from_mapping(a["engine"]), InstrumentSpec.from_mapping(a["spec"]))
                built = report.build_run(res, starting_balance=float(a["engine"]["starting_balance"]),
                                         leverage=int(a["engine"]["leverage"]), contract=a["contract"])
                with ms.store.transaction() as con:
                    st.upsert_run(con, {**r, "status": "COMPLETED", "dataset": prepared["dataset"], "result": built,
                                        "result_hash": res["result_hash"], "reason": None, "started_at": started,
                                        "finished_at": iso(ms.clock.now())})
        with ms.store.read() as con:
            final = st.runs(con, eid)
        status = "COMPLETED" if all(r["status"] == "COMPLETED" for r in final) else "INCOMPLETE"
        with ms.store.transaction() as con:
            st.update_evaluation(con, eid, ms.clock.now(), status=status, phase=None, availability=av,
                                 finished_at=iso(ms.clock.now()))
    _start_job(ms, eid, "RUNNING", work)
    return _detail(ms, eid)


@router.post("/evaluations/{eid}/cancel")
def cancel(request: Request, eid: str) -> dict[str, Any]:
    return {"cancelled": ms_of(request).jobs.cancel(eid)}


# ------------------------------------------------------------------ reports
def _run(ms: MonthlyServices, rid: str) -> dict[str, Any]:
    with ms.store.read() as con:
        r = st.run(con, rid)
    if r is None:
        raise HTTPException(404, f"unknown run {rid}")
    return r


@router.get("/runs/{rid}")
def get_run(request: Request, rid: str) -> dict[str, Any]:
    ms = ms_of(request)
    r = _run(ms, rid)
    ev = _eval(ms, r["evaluation_id"])
    return {"run": r, "evaluation": {k: ev[k] for k in ("id", "month", "range_start", "range_end", "status",
                                                          "app_version")}, "disclaimer": report.DISCLAIMER}


def _download(content: bytes | str, media: str, name: str) -> Response:
    return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/runs/{rid}/report.pdf")
def run_pdf(request: Request, rid: str) -> Response:
    ms = ms_of(request)
    r = _run(ms, rid)
    ev = _eval(ms, r["evaluation_id"])
    return _download(report.run_pdf(r, ev, iso(ms.clock.now())), "application/pdf", f"{rid}.pdf")


@router.get("/runs/{rid}/ledger.csv")
def run_csv(request: Request, rid: str) -> Response:
    r = _run(ms_of(request), rid)
    return _download(report.ledger_csv(r), "text/csv", f"{rid}-ledger.csv")


@router.get("/runs/{rid}/run.json")
def run_json(request: Request, rid: str) -> JSONResponse:
    ms = ms_of(request)
    r = _run(ms, rid)
    return JSONResponse(report.run_json(r, _eval(ms, r["evaluation_id"])),
                        headers={"Content-Disposition": f'attachment; filename="{rid}.json"'})


@router.post("/runs/{rid}/reproduce")
def reproduce(request: Request, rid: str) -> dict[str, Any]:
    """Re-run a completed run from its STORED configuration, assumptions and dataset definition."""
    ms = ms_of(request)
    r = _run(ms, rid)
    if r["status"] != "COMPLETED":
        raise HTTPException(409, "only completed runs can be reproduced")
    ds = r["dataset"]
    start, end = runner.parse_iso(ds["range"]["start"]), runner.parse_iso(ds["range"]["end"])
    row = ms.sources.series_row(ds["store"], ds["series"])
    run_tf = Timeframe.parse(r["timeframe"])
    prepared = runner.prepare(ms.sources, ds["store"], row, run_tf, start, end, lookback_days=ds["lookback_days"],
                              detail=set(ds["detail"]), cfg=ms.analysis_cfg)
    a = r["assumptions"]
    res = runner.run_one(r["config"], prepared, run_tf, start, end, engine.Assumptions.from_mapping(a["engine"]),
                         InstrumentSpec.from_mapping(a["spec"]))
    same_data = prepared["dataset"]["dataset_hash"] == ds["dataset_hash"] and \
        prepared["dataset"]["execution_hash"] == ds["execution_hash"]
    return {"run_id": rid, "stored_result_hash": r["result_hash"], "reproduced_result_hash": res["result_hash"],
            "identical": res["result_hash"] == r["result_hash"], "same_input_data": same_data,
            "note": None if same_data else "the stored candles changed since the run (for example a re-download)"}


@router.get("/evaluations/{eid}/strategies/{key}")
def strategy_report(request: Request, eid: str, key: str) -> dict[str, Any]:
    ms = ms_of(request)
    _eval(ms, eid)
    with ms.store.read() as con:
        runs = [r for r in st.runs(con, eid, with_result=True) if r["strategy_key"] == key]
    if not runs:
        raise HTTPException(404, f"no runs for {key}")
    return {"comparison": report.strategy_comparison(key, runs), "name": runs[0]["config"]["name"],
            "kind": runs[0]["kind"], "disclaimer": report.DISCLAIMER}


@router.get("/evaluations/{eid}/strategies/{key}/report.pdf")
def strategy_pdf(request: Request, eid: str, key: str) -> Response:
    ms = ms_of(request)
    ev = _eval(ms, eid)
    data = strategy_report(request, eid, key)
    return _download(report.strategy_pdf(data["comparison"], data["name"], ev, iso(ms.clock.now())), "application/pdf",
                     f"{eid}-{key}-comparison.pdf")


def _combined(ms: MonthlyServices, eid: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    ev = _eval(ms, eid)
    with ms.store.read() as con:
        runs = st.runs(con, eid, with_result=True)
        cands = st.candidates(con, eid)
    names = {r["strategy_key"]: r["config"]["name"] for r in runs}
    return report.combined(ev, runs, cands), ev, names


@router.get("/evaluations/{eid}/combined")
def combined_report(request: Request, eid: str) -> dict[str, Any]:
    comb, _ev, names = _combined(ms_of(request), eid)
    return {**comb, "names": names}


@router.get("/evaluations/{eid}/report.pdf")
def combined_pdf(request: Request, eid: str) -> Response:
    ms = ms_of(request)
    comb, ev, names = _combined(ms, eid)
    return _download(report.combined_pdf(comb, names, iso(ms.clock.now()), ev), "application/pdf", f"{eid}-combined.pdf")


@router.get("/evaluations/{eid}/summary.csv")
def combined_csv(request: Request, eid: str) -> PlainTextResponse:
    comb, _ev, _names = _combined(ms_of(request), eid)
    return PlainTextResponse(report.summary_csv(comb), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{eid}-summary.csv"'})


# ------------------------------------------------------------------ history import into the ISOLATED store
@router.post("/import")
async def import_history(request: Request, file: UploadFile = File(...), dialect: str = Form(...),
                         timeframe: str = Form(...), time_basis: str = Form(...), series_label: str = Form(...),
                         candle_side: str = Form("bid"), price_decimals: int = Form(2),
                         point: str | None = Form(None), label: str = Form(""),
                         instrument: str = Form("XAUUSD")) -> dict[str, Any]:
    """CSV history (for example an MT5 export) into the research store - never into the application database.
    ``point`` is the symbol's point size (0.01 for Vantage XAUUSD); an MT5 export's <SPREAD> column is in points, so
    it is required for those files (never assumed). Idempotent: the same file (hash) is never stored twice."""
    ms = ms_of(request)
    content = await file.read()
    if len(content) > 200 * 1024 * 1024:
        raise HTTPException(413, "file too large (200 MB limit)")
    if instrument.upper() not in SUPPORTED_INSTRUMENTS:
        raise HTTPException(422, f"instrument must be one of {', '.join(SUPPORTED_INSTRUMENTS)}")
    try:
        point_d = Decimal(point) if point not in (None, "") else None
        result = import_csv(ms.store, ms.clock, ms.app_spec.schedule, content=content,
                            file_name=file.filename or "upload.csv", dialect=dialect, tf=Timeframe.parse(timeframe),
                            time_basis=time_basis, series_label=series_label, symbol="XAUUSD", source_symbol="XAUUSD",
                            candle_side=candle_side, price_decimals=price_decimals, point=point_d)
    except (ImportRefused, CsvFormatError, SeriesConflict, ValueError, InvalidOperation) as exc:
        raise HTTPException(422, str(exc)) from exc
    if result.import_id is not None:
        with ms.store.transaction() as con:
            st.set_import_label(con, result.import_id, (label or file.filename or "import")[:200], instrument.upper(),
                                ms.clock.now())
    return {"store": "research", "label": label, **result.to_dict()}


SUPPORTED_INSTRUMENTS = ("XAUUSD",)


@router.post("/import/preview")
async def import_preview(request: Request, file: UploadFile = File(...), dialect: str = Form("mt5_export"),
                         timeframe: str = Form("M1"), time_basis: str = Form("rule:ny_close"),
                         instrument: str = Form("XAUUSD"), point: str | None = Form(None), month: str = Form(...),
                         series_label: str = Form(""), lookback_days: int = Form(5)) -> dict[str, Any]:
    """Validate a CSV WITHOUT storing it: structure, timestamps and time basis (incl. DST), OHLC, ordering,
    duplicates, timeframe, spread, missing bars, the month's coverage and the pre-month warm-up."""
    ms = ms_of(request)
    content = await file.read()
    if len(content) > 200 * 1024 * 1024:
        raise HTTPException(413, "file too large (200 MB limit)")
    if instrument.upper() not in SUPPORTED_INSTRUMENTS:
        raise HTTPException(422, f"instrument must be one of {', '.join(SUPPORTED_INSTRUMENTS)}")
    try:
        tf = Timeframe.parse(timeframe)
        month_range(month)
        point_d = Decimal(point) if point not in (None, "") else None
        already = False
        if series_label.strip():
            sha = hashlib.sha256(content).hexdigest()
            with ms.store.read() as con:
                row = candle_repo.series_row(con, f"csv:{_series_label(series_label)}")
                already = row is not None and candle_repo.find_import(con, row["id"], tf, sha) is not None
        return importing.preview(content, file_name=file.filename or "upload.csv", instrument=instrument.upper(),
                                 dialect=dialect, tf=tf, time_basis=time_basis, point=point_d,
                                 schedule=ms.app_spec.schedule, month=month, lookback_days=int(lookback_days),
                                 now=ms.clock.now(), already_imported=already)
    except (CsvFormatError, ValueError, InvalidOperation) as exc:
        raise HTTPException(422, str(exc)) from exc


def _series_label(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in str(text).strip())


@router.get("/imports")
def list_imports(request: Request) -> dict[str, Any]:
    with ms_of(request).store.read() as con:
        return {"imports": st.imports(con), "store": "research"}


@router.get("/coverage")
def coverage_view(request: Request, month: str, timeframe: str = "M1", store: str = "research",
                  series: str = DEFAULT_SERIES, lookback_days: int = 5) -> dict[str, Any]:
    """One timeframe's coverage of a month: expected / present bars, every missing interval and date, history before
    the month and the warm-up rule. Reads only."""
    ms = ms_of(request)
    try:
        tf = Timeframe.parse(timeframe)
        start, end = month_range(month)
        row = ms.sources.series_row(store, series)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if row is None:
        raise HTTPException(404, f"series {series!r} not found in the {store} store")
    frame = ms.sources.load(store, row, tf, start - timedelta(days=int(lookback_days) + 7), end)
    allbars = ms.sources.load(store, row, tf, None, None, limit=2_000_000)
    out = importing.month_coverage(frame.index, tf, ms.app_spec.schedule, month, int(lookback_days))
    return {**out, "store": store, "series": series, "total_candles": int(len(allbars)),
            "earliest": iso(allbars.index[0].to_pydatetime()) if len(allbars) else None,
            "latest": iso(allbars.index[-1].to_pydatetime()) if len(allbars) else None,
            "status": "READY" if out["ready"] else "INCOMPLETE"}


# ------------------------------------------------------------------ Phase 5.1 percentage-risk scenario studies
class RiskStudyIn(BaseModel):
    baseline_evaluation: str = Field(min_length=3, max_length=40)
    scenarios: list[float] = Field(default_factory=lambda: [s["risk_percent"] for s in runner.RISK_SCENARIOS],
                                   min_length=1, max_length=5)
    candidates: list[str] = Field(default_factory=list, max_length=20)


class StudyRerunIn(BaseModel):
    timeframes: list[str] | None = Field(None, max_length=3)
    reason: str = Field("", max_length=500)


def _start_study(ms: MonthlyServices, sid: str, timeframes: tuple[str, ...] | None = None) -> None:
    if ms.jobs.busy():
        raise HTTPException(409, "another monthly job is running; wait for it or cancel it")
    try:
        ms.jobs.start(sid, lambda cancel: risk.run_study(ms, sid, cancel, timeframes))
    except RuntimeError as exc:
        raise HTTPException(409, "another monthly job is running") from exc


@router.post("/risk-studies", status_code=201)
def create_risk_study(request: Request, body: RiskStudyIn) -> dict[str, Any]:
    """Create one evaluation per hypothetical risk scenario from a finished fixed-lot baseline (never modified) and
    run them all (each run its own $1,000 account)."""
    ms = ms_of(request)
    if ms.jobs.busy():
        raise HTTPException(409, "another monthly job is running; wait for it or cancel it")
    base_start, base_end = month_range(_eval(ms, body.baseline_evaluation)["month"])
    forward.check_range(base_start, base_end, "Risk study (its baseline month)")
    try:
        created = risk.create(ms, body.baseline_evaluation, scenarios=body.scenarios, candidate_keys=body.candidates)
    except risk.StudyRefused as exc:
        raise HTTPException(409, str(exc)) from exc
    _start_study(ms, created["id"])
    return risk_study(request, created["id"])


@router.get("/risk-studies")
def list_risk_studies(request: Request) -> dict[str, Any]:
    with ms_of(request).store.read() as con:
        return {"studies": st.studies(con), "banner": runner.RISK_BANNER}


@router.get("/risk-studies/{sid}")
def risk_study(request: Request, sid: str) -> dict[str, Any]:
    ms = ms_of(request)
    try:
        data = risk.load(ms, sid)
    except risk.StudyRefused as exc:
        raise HTTPException(404, str(exc)) from exc
    return {**risk.master(data), "running_job": ms.jobs.busy() == sid,
            "evaluations": [{k: e[k] for k in ("id", "status", "phase", "error", "finished_at")} |
                            {"risk_scenario": e["params"]["risk_scenario"]} for e in data["evaluations"]],
            "baseline": {k: data["baseline"][k] for k in ("id", "status", "month", "app_version")},
            "phase": data["study"]["phase"], "progress": data["study"]["progress"], "total": data["study"]["total"]}


@router.post("/risk-studies/{sid}/run", status_code=202)
def rerun_risk_study(request: Request, sid: str, body: StudyRerunIn | None = None) -> dict[str, Any]:
    """Run a READY / FAILED study, or re-run selected timeframes of a finished one. A re-run is recorded in the
    study (time, timeframes, reason, every replaced result hash); other timeframes stay untouched."""
    ms = ms_of(request)
    body = body or StudyRerunIn()
    with ms.store.read() as con:
        s = st.study(con, sid)
    if s is None:
        raise HTTPException(404, f"unknown risk study {sid}")
    tfs = tuple(body.timeframes) if body.timeframes else None
    if tfs and any(tf not in RUN_TIMEFRAMES for tf in tfs):
        raise HTTPException(422, f"timeframes must be among {', '.join(RUN_TIMEFRAMES)}")
    if s["status"] not in ("READY", "FAILED") and not (tfs and s["status"] in ("COMPLETED", "INCOMPLETE")):
        raise HTTPException(409, f"a study runs from READY or FAILED, or re-runs named timeframes when finished "
                                 f"(status {s['status']})")
    if tfs and s["status"] in ("COMPLETED", "INCOMPLETE"):
        if not body.reason.strip():
            raise HTTPException(422, "a re-run of a finished study needs a reason (it is recorded)")
        with ms.store.read() as con:
            replaced = {r["id"]: r["result_hash"] for eid in s["evaluation_ids"] for r in st.runs(con, eid)
                        if r["timeframe"] in tfs}
        params = {**s["params"], "reruns": [*s["params"].get("reruns", []),
                                            {"at": iso(ms.clock.now()), "timeframes": list(tfs), "reason": body.reason,
                                             "replaced_result_hashes": replaced}]}
        with ms.store.transaction() as con:
            st.update_study(con, sid, ms.clock.now(), params=params)
    _start_study(ms, sid, tfs)
    return risk_study(request, sid)


@router.post("/risk-studies/{sid}/cancel")
def cancel_risk_study(request: Request, sid: str) -> dict[str, Any]:
    return {"cancelled": ms_of(request).jobs.cancel(sid)}


def _study_data(ms: MonthlyServices, sid: str) -> dict[str, Any]:
    try:
        return risk.load(ms, sid)
    except risk.StudyRefused as exc:
        raise HTTPException(404, str(exc)) from exc


def _name(data: dict[str, Any], key: str) -> str:
    return next((r["config"]["name"] for ev in data["evaluations"] for r in ev["runs_full"] if r["strategy_key"] == key), key)


@router.get("/risk-studies/{sid}/strategies/{key}/scenarios")
def risk_scenarios_view(request: Request, sid: str, key: str) -> dict[str, Any]:
    data = _study_data(ms_of(request), sid)
    return {**risk.scenario_comparison(data, key), "name": _name(data, key)}


@router.get("/risk-studies/{sid}/strategies/{key}/scenarios.pdf")
def risk_scenarios_pdf(request: Request, sid: str, key: str) -> Response:
    ms = ms_of(request)
    data = _study_data(ms, sid)
    pdf = report.scenario_pdf(risk.scenario_comparison(data, key), risk.master(data)["study"], _name(data, key),
                              iso(ms.clock.now()))
    return _download(pdf, "application/pdf", f"{sid}-{key}-risk-scenarios.pdf")


@router.get("/risk-studies/{sid}/strategies/{key}/timeframes")
def risk_timeframes_view(request: Request, sid: str, key: str) -> dict[str, Any]:
    data = _study_data(ms_of(request), sid)
    return {**risk.strategy_comparison(data, key), "name": _name(data, key)}


@router.get("/risk-studies/{sid}/strategies/{key}/timeframes.pdf")
def risk_timeframes_pdf(request: Request, sid: str, key: str) -> Response:
    ms = ms_of(request)
    data = _study_data(ms, sid)
    pdf = report.study_strategy_pdf(risk.strategy_comparison(data, key), risk.master(data)["study"], _name(data, key),
                                    iso(ms.clock.now()))
    return _download(pdf, "application/pdf", f"{sid}-{key}-timeframes.pdf")


@router.get("/risk-studies/{sid}/master.pdf")
def risk_master_pdf(request: Request, sid: str) -> Response:
    ms = ms_of(request)
    return _download(report.master_pdf(risk.master(_study_data(ms, sid)), iso(ms.clock.now())), "application/pdf",
                     f"{sid}-master.pdf")


@router.get("/risk-studies/{sid}/master.csv")
def risk_master_csv(request: Request, sid: str) -> PlainTextResponse:
    m = risk.master(_study_data(ms_of(request), sid))
    return PlainTextResponse(report.master_csv(m), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{sid}-master.csv"'})


def research_window_default(month: str, days: int) -> dict[str, str]:
    start, _ = month_range(month)
    return {"start": iso(start - timedelta(days=days)), "end": iso(start)}


# ------------------------------------------------------------------ Phase 5.2 strategy redesign and robustness research
class RobustnessIn(BaseModel):
    baseline_evaluation: str | None = Field(None, max_length=40)
    start: str | None = Field(None, max_length=40)
    end: str | None = Field(None, max_length=40)
    development_end: str | None = Field(None, max_length=40)
    stage: str = Field("full", pattern="^(diagnostic|full)$")
    diagnostic_study: str | None = Field(None, max_length=40)


ROBUSTNESS_SOURCES = {"M15": ("app", DEFAULT_SERIES), "M5": ("app", DEFAULT_SERIES),
                      "M1": ("research", "csv:vantage-mt5-xauusd")}


def _robustness(ms: MonthlyServices, rid: str) -> dict[str, Any]:
    with ms.store.read() as con:
        r = con.execute("SELECT * FROM robustness_studies WHERE id = ?", (rid,)).fetchone()
    if r is None:
        raise HTTPException(404, f"unknown robustness study {rid}")
    return {k: r[k] for k in ("id", "status", "phase", "progress", "total", "app_version", "error", "created_at",
                              "updated_at", "finished_at")} | {"params": json.loads(r["params"]),
                                                                "analysis": json.loads(r["analysis"]) if r["analysis"] else None}


@router.post("/robustness-studies", status_code=201)
def create_robustness(request: Request, body: RobustnessIn | None = None) -> dict[str, Any]:
    """Phase 5.2: benchmarks, exit experiments and redesign variants of every strategy over every month, with
    development / walk-forward / hold-out analysis. Historical simulation only."""
    ms = ms_of(request)
    body = body or RobustnessIn()
    if ms.jobs.busy():
        raise HTTPException(409, "another monthly job is running; wait for it or cancel it")
    start, end, dev_end = robustness.default_range(ms.clock.now())
    try:
        start = runner.parse_iso(body.start) if body.start else start
        end = runner.parse_iso(body.end) if body.end else end
        dev_end = runner.parse_iso(body.development_end) if body.development_end else dev_end
        forward.check_range(start, end, "Robustness study (its months, from the request or the default range)")
        base = body.baseline_evaluation
        if base is None:
            with ms.store.read() as con:
                done = [e for e in st.evaluations(con, 200) if e["status"] in ("COMPLETED", "INCOMPLETE") and e["frozen"]
                        and not e["params"].get("risk_scenario") and not e["params"].get("risk_percent_override")]
            if not done:
                raise robustness.RobustnessError("no finished fixed-lot evaluation to take the research candidates from")
            base = max(done, key=lambda e: (len(e["frozen"].get("candidates") or []), e["created_at"]))["id"]
        created = robustness.create(ms, start=start, end=end, development_end=dev_end, baseline_evaluation=base,
                                    sources=ROBUSTNESS_SOURCES, stage=body.stage,
                                    diagnostic_study=body.diagnostic_study)
    except (robustness.RobustnessError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    rid = created["id"]
    try:
        ms.jobs.start(rid, lambda cancel: robustness.run_study(ms, rid, cancel))
    except RuntimeError as exc:
        raise HTTPException(409, "another monthly job is running") from exc
    return get_robustness(request, rid)


@router.get("/robustness-studies")
def list_robustness(request: Request) -> dict[str, Any]:
    ms = ms_of(request)
    with ms.store.read() as con:
        rows = con.execute("SELECT id, status, phase, progress, total, created_at, finished_at, error FROM "
                           "robustness_studies ORDER BY created_at DESC LIMIT 50").fetchall()
    return {"studies": [dict(r) for r in rows], "running": ms.jobs.busy()}


@router.get("/robustness-studies/{rid}")
def get_robustness(request: Request, rid: str) -> dict[str, Any]:
    ms = ms_of(request)
    out = _robustness(ms, rid)
    out["running_job"] = ms.jobs.busy() == rid
    out["params"] = {k: v for k, v in out["params"].items() if k != "configs"} | \
        {"config_count": len(out["params"]["configs"])}
    return out


@router.post("/robustness-studies/{rid}/cancel")
def cancel_robustness(request: Request, rid: str) -> dict[str, Any]:
    return {"cancelled": ms_of(request).jobs.cancel(rid)}


@router.get("/robustness-studies/{rid}/configs")
def robustness_configs(request: Request, rid: str) -> dict[str, Any]:
    """Every pre-declared configuration with its exact rules (benchmarks, exit experiments, redesign variants)."""
    ms = ms_of(request)
    params = _robustness(ms, rid)["params"]
    with ms.store.read() as con:
        rules = {r["key"]: json.loads(r["config"]) for r in con.execute(
            "SELECT key, config FROM robustness_configs WHERE study_id = ?", (rid,))}
    return {"configs": [{**c, "config": rules.get(c["key"])} for c in params["configs"]]}


@router.get("/robustness-studies/{rid}/results.csv")
def robustness_csv(request: Request, rid: str) -> PlainTextResponse:
    ms = ms_of(request)
    params = _robustness(ms, rid)["params"]
    meta = {c["key"]: c for c in params["configs"]}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# SIMULATED historical research - separate hypothetical $1,000 accounts per row; not trading advice", rid])
    cols = ["trades", "wins", "losses", "win_rate", "net_usd", "sum_r", "expectancy_r", "profit_factor", "max_dd_r",
            "max_dd_usd", "avg_hold_minutes", "costs_usd", "cost_r"]
    w.writerow(["config", "strategy", "role", "label", "timeframe", "window", "role_of_window", "status", *cols, "reason"])
    roles = {x["key"]: x["role"] for x in params["windows"]}
    with ms.store.read() as con:
        for r in con.execute("SELECT config_key, timeframe, window, status, reason, metrics FROM robustness_results "
                             "WHERE study_id = ? ORDER BY config_key, timeframe, window", (rid,)):
            m = json.loads(r["metrics"]) if r["metrics"] else {}
            c = meta.get(r["config_key"], {})
            w.writerow([r["config_key"], c.get("strategy"), c.get("role"), c.get("label"), r["timeframe"], r["window"],
                        roles.get(r["window"]), r["status"], *[m.get(k) for k in cols], r["reason"]])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{rid}-results.csv"'})


@router.get("/robustness-studies/{rid}/report.pdf")
def robustness_pdf(request: Request, rid: str) -> Response:
    ms = ms_of(request)
    s = _robustness(ms, rid)
    if not s["analysis"]:
        raise HTTPException(409, f"the study is {s['status']}; the report exists once it has completed")
    return _download(report.robustness_pdf(s, iso(ms.clock.now())), "application/pdf", f"{rid}-strategy-research.pdf")


@router.get("/research-versions")
def research_versions(request: Request) -> dict[str, Any]:
    with ms_of(request).store.read() as con:
        rows = con.execute("SELECT id, study_id, strategy, name, fingerprint, chosen_variant, verdict, evidence, created_at "
                           "FROM research_versions ORDER BY created_at DESC, strategy").fetchall()
    return {"versions": [dict(r) | {"evidence": json.loads(r["evidence"])} for r in rows],
            "note": "Research versions only: never written to the application's strategies. Adopting one is a separate, "
                    "explicit decision."}



# ------------------------------------------------------------------ Phase 5.3 frozen pre-June historical validation
@router.post("/validation-studies", status_code=201)
def create_validation(request: Request) -> dict[str, Any]:
    """Run the four locked Phase 5.2 candidates and their originals on January-May 2026. No selection. Refused unless
    the validation protocol is frozen and unchanged."""
    ms = ms_of(request)
    if ms.jobs.busy():
        raise HTTPException(409, "another monthly job is running; wait for it or cancel it")
    if closure.closed():
        raise HTTPException(409, "Phase 5.3 is closed and frozen (var/monthly/reports/phase5-research-cycle-closed.json): "
                                 "its candidates are retired and January-May 2026 is spent; no further validation@1 run")
    chk = validation_report.check()
    if not chk.get("frozen") or not chk.get("unchanged"):
        raise HTTPException(409, f"the validation protocol must be frozen and unchanged before it runs: {chk}")
    try:
        created = validation.create(ms)
    except validation.ValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    vid = created["id"]
    try:
        ms.jobs.start(vid, lambda cancel: validation.run(ms, vid, cancel))
    except RuntimeError as exc:
        raise HTTPException(409, "another monthly job is running") from exc
    return get_validation(request, vid)


@router.get("/validation-studies")
def list_validation(request: Request) -> dict[str, Any]:
    with ms_of(request).store.read() as con:
        rows = con.execute("SELECT id, status, phase, progress, total, created_at, finished_at, error FROM "
                           "validation_studies ORDER BY created_at DESC LIMIT 50").fetchall()
    return {"studies": [dict(r) for r in rows], "protocol": validation_report.check()}


@router.get("/validation-studies/{vid}")
def get_validation(request: Request, vid: str) -> dict[str, Any]:
    ms = ms_of(request)
    with ms.store.read() as con:
        r = con.execute("SELECT * FROM validation_studies WHERE id = ?", (vid,)).fetchone()
    if r is None:
        raise HTTPException(404, f"unknown validation study {vid}")
    return {k: r[k] for k in ("id", "status", "phase", "progress", "total", "app_version", "error", "created_at",
                              "finished_at")} | {"params": json.loads(r["params"]), "running_job": ms.jobs.busy() == vid,
                                                 "analysis": json.loads(r["analysis"]) if r["analysis"] else None}


# ------------------------------------------------------------------ Phase 6 new strategy discovery (development only)
class TrialIn(BaseModel):
    hypothesis: str = Field(pattern=r"^H6\.[0-9]$")
    kind: str = Field(default="baseline", pattern="^(baseline|refinement)$")
    rationale: str = Field(default="", max_length=4000)
    parent: str | None = Field(default=None, max_length=200)
    refinement: dict[str, Any] | None = None


@router.get("/discovery/protocol")
def discovery_protocol() -> dict[str, Any]:
    spec = discovery.load_spec()
    return {"check": discovery_report.check(), "hypotheses": spec["hypotheses"], "development": spec["development"],
            "sealed_validation": spec["sealed_validation"], "windows": discovery.dev_windows(),
            "sealed": [discovery.SEALED[0].isoformat(), discovery.SEALED[1].isoformat()],
            "forward_from": discovery.FORWARD_FROM.isoformat(), "max_trials": discovery.MAX_TRIALS}


@router.post("/discovery/trials", status_code=201)
def create_discovery_trial(request: Request, body: TrialIn) -> dict[str, Any]:
    """A development trial: a hypothesis's baseline, or one of its pre-declared refinements (built by the tool, with a
    written rationale). Refused unless the Phase 6 protocol is frozen and unchanged; never reads sealed or forward data."""
    ms = ms_of(request)
    if ms.jobs.busy():
        raise HTTPException(409, "another monthly job is running; wait for it or cancel it")
    chk = discovery_report.check()
    if not chk.get("frozen") or not chk.get("unchanged"):
        raise HTTPException(409, f"the Phase 6 protocol must be frozen and unchanged before any trial: {chk}")
    try:
        created = discovery.create_trial(ms, body.hypothesis, body.kind, rationale=body.rationale, parent=body.parent,
                                         refinement=body.refinement)
    except discovery.DiscoveryError as exc:
        raise HTTPException(409, str(exc)) from exc
    tid = created["id"]
    try:
        ms.jobs.start(tid, lambda cancel: discovery.run_trial(ms, tid, cancel))
    except RuntimeError as exc:
        raise HTTPException(409, "another monthly job is running") from exc
    return get_discovery_trial(request, tid)


@router.post("/discovery/baselines", status_code=201)
def create_discovery_baselines(request: Request) -> dict[str, Any]:
    """The baseline trial of every hypothesis that has none yet, run as ONE job: each development month's feature
    timeline is computed once and shared; every trial is still recorded and counted on its own."""
    ms = ms_of(request)
    if ms.jobs.busy():
        raise HTTPException(409, "another monthly job is running; wait for it or cancel it")
    chk = discovery_report.check()
    if not chk.get("frozen") or not chk.get("unchanged"):
        raise HTTPException(409, f"the Phase 6 protocol must be frozen and unchanged before any trial: {chk}")
    spec = discovery.load_spec()
    with ms.store.read() as con:
        have = {r[0] for r in con.execute("SELECT hypothesis FROM discovery_trials WHERE kind = 'baseline'")}
    todo = [h["id"] for h in spec["hypotheses"] if h["id"] not in have]
    if not todo:
        raise HTTPException(409, "every hypothesis already has its baseline trial")
    try:
        tids = [discovery.create_trial(ms, hid, "baseline", spec=spec)["id"] for hid in todo]
    except discovery.DiscoveryError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        ms.jobs.start("+".join(tids), lambda cancel: discovery.run_trials(ms, tids, cancel))
    except RuntimeError as exc:
        raise HTTPException(409, "another monthly job is running") from exc
    return {"trials": [get_discovery_trial(request, t) for t in tids]}


@router.get("/discovery/trials")
def list_discovery_trials(request: Request) -> dict[str, Any]:
    with ms_of(request).store.read() as con:
        rows = con.execute("SELECT id, hypothesis, kind, status, phase, progress, total, created_at, finished_at, error "
                           "FROM discovery_trials ORDER BY created_at").fetchall()
    return {"trials": [dict(r) for r in rows], "protocol": discovery_report.check()}


@router.get("/discovery/trials/{tid}")
def get_discovery_trial(request: Request, tid: str) -> dict[str, Any]:
    ms = ms_of(request)
    with ms.store.read() as con:
        r = con.execute("SELECT * FROM discovery_trials WHERE id = ?", (tid,)).fetchone()
    if r is None:
        raise HTTPException(404, f"unknown discovery trial {tid}")
    return {k: r[k] for k in ("id", "hypothesis", "kind", "status", "phase", "progress", "total", "app_version", "error",
                              "created_at", "finished_at")} | {
        "params": json.loads(r["params"]), "running_job": tid in (ms.jobs.busy() or "").split("+"),
        "analysis": json.loads(r["analysis"]) if r["analysis"] else None}


# ------------------------------------------------------------------ Phase 6 H6.4: development run and the one sealed test
class SealedRunIn(BaseModel):
    purpose: str = Field(pattern="^(development|sealed)$")


@router.post("/h64/runs", status_code=201)
def create_h64_run(request: Request, body: SealedRunIn) -> dict[str, Any]:
    """H6.4 candidate and its VOL-C control. 'development' runs on January-13 November 2026 only; 'sealed' runs once on
    November 2023 - October 2025 and is refused unless the H6.4 registration, code and data checks are frozen and
    unchanged."""
    ms = ms_of(request)
    if ms.jobs.busy():
        raise HTTPException(409, "another monthly job is running; wait for it or cancel it")
    protocol = sealed_report.check() if body.purpose == "sealed" else None
    try:
        created = sealed.create_run(ms, body.purpose, protocol=protocol)
    except (sealed.SealedError, discovery.DiscoveryError) as exc:
        raise HTTPException(409, str(exc)) from exc
    rid = created["id"]
    try:
        ms.jobs.start(rid, lambda cancel: sealed.run(ms, rid, cancel))
    except RuntimeError as exc:
        raise HTTPException(409, "another monthly job is running") from exc
    return get_h64_run(request, rid)


@router.get("/h64/runs")
def list_h64_runs(request: Request) -> dict[str, Any]:
    return {"runs": sealed.runs_overview(ms_of(request).store), "protocol": sealed_report.check()}


@router.get("/h64/runs/{rid}")
def get_h64_run(request: Request, rid: str) -> dict[str, Any]:
    ms = ms_of(request)
    with ms.store.read() as con:
        r = con.execute("SELECT * FROM sealed_runs WHERE id = ?", (rid,)).fetchone()
    if r is None:
        raise HTTPException(404, f"unknown H6.4 run {rid}")
    return {k: r[k] for k in ("id", "hypothesis", "purpose", "status", "phase", "progress", "total", "app_version", "error",
                              "created_at", "finished_at")} | {
        "params": json.loads(r["params"]), "running_job": ms.jobs.busy() == rid,
        "analysis": json.loads(r["analysis"]) if r["analysis"] else None}
