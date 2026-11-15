"""Phase 4 routes: improvement studies, proposals (approve / reject = the ONLY way a proposal becomes a strategy
version) and the research assistant."""
from __future__ import annotations

import secrets
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from goldscalp.api.routers.research import AssumptionsIn, _assumptions
from goldscalp.api.research_data import research_dataset, research_outcome_candles
from goldscalp.api.services import AppServices, clean, pick_series, services
from goldscalp.assistant import answer as assistant
from goldscalp.assistant import llm
from goldscalp.assistant import tools as assistant_tools
from goldscalp.backtesting import features
from goldscalp.core import forward
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import ALL_TIMEFRAMES, Timeframe
from goldscalp.research import improvement
from goldscalp.storage import improvement_repo, live_repo, strategy_repo
from goldscalp.strategies import builder

router = APIRouter(prefix="/api/research/studies")
proposals_router = APIRouter(prefix="/api/proposals")
assistant_router = APIRouter(prefix="/api/assistant")


class StudyIn(BaseModel):
    strategy_id: int
    version: int | None = Field(None, ge=1)
    series: str | None = Field(None, max_length=200)
    discovery_pct: float = Field(0.60, ge=0.3, le=0.8)
    embargo_pct: float = Field(0.05, ge=0.0, le=0.2)
    use_live_evidence: bool = True
    lookback_days: int = Field(5, ge=1, le=20)
    assumptions: AssumptionsIn = Field(default_factory=AssumptionsIn)


def _coverage(gs: AppServices, row: Any, step: Timeframe) -> tuple[datetime, datetime, Timeframe]:
    """The stored base timeframe (dividing the step) with the longest closed history."""
    best = None
    with gs.db.read() as con:
        for tf in ALL_TIMEFRAMES:
            if tf.seconds > step.seconds or step.seconds % tf.seconds:
                continue
            lo, hi, n = con.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM candles WHERE series_id = ? AND tf = ? "
                                    "AND closed = 1", (row["id"], tf.value)).fetchone()
            if n and (best is None or lo < best[0]):
                best = (lo, hi, tf)
    if best is None:
        raise HTTPException(422, f"series {row['key']} has no closed candles usable for a {step.value} strategy")
    lo, hi, tf = best
    return datetime.fromtimestamp(lo, tz=UTC), datetime.fromtimestamp(hi + tf.seconds, tz=UTC), tf


def _live_evidence(gs: AppServices, sid: int, version: int) -> tuple[datetime | None, dict[str, Any] | None]:
    with gs.db.read() as con:
        trades = live_repo.trades(con, strategy_id=sid, version=version, limit=10000)
        sigs = live_repo.signals(con, strategy_id=sid, version=version, limit=10000)
    times = [t["opened_at"] for t in trades] + [s["cutoff"] for s in sigs]
    if not times:
        return None, None
    forward.check_index(pd.DatetimeIndex([datetime.fromisoformat(x.replace("Z", "+00:00")) for x in times]),
                        "Phase 4 live evidence (monitoring records)")     # forward records never enter a study
    first = datetime.fromisoformat(min(times).replace("Z", "+00:00"))
    comp = assistant_tools.backtest_vs_live(gs, sid, version)["data"]["strategies"]
    measured = [s for s in sigs if (s.get("outcome") or {}).get("status") == "complete"]
    false = sum(1 for s in measured if s["outcome"].get("false_signal"))
    row = comp[0] if comp else {}
    live = row.get("live") or {}
    return first, {"n": live.get("trades", 0), "signals": len(sigs), "measured_signals": len(measured),
                   "false_signals": false, "live": live, "backtest": row.get("backtest"),
                   "same_exit_rate": row.get("same_exit_rate"), "mean_entry_diff": row.get("mean_entry_diff"),
                   "detail": (f"live-paper: {live.get('trades', 0)} closed SIMULATED trade(s), net {live.get('net')} USD; "
                              f"{false} of {len(measured)} measured signals were false; backtest replica same-exit rate "
                              f"{row.get('same_exit_rate')}")}


@router.post("", status_code=202)
def create_study(request: Request, body: StudyIn) -> dict[str, Any]:
    gs = services(request)
    if gs.perf_jobs.busy():
        raise HTTPException(409, "another evaluation is still running; wait for it or cancel it")
    with gs.db.read() as con:
        record = strategy_repo.get(con, body.strategy_id, body.version)
    if record is None:
        raise HTTPException(404, f"unknown strategy {body.strategy_id}")
    config = record["config"]
    step = Timeframe.parse(config["evaluation"]["step_timeframe"])
    row = pick_series(gs.db, body.series)
    if row is None:
        raise HTTPException(422, "no candles stored yet")
    cov_start, cov_end, _ = _coverage(gs, row, step)
    live_first, live = _live_evidence(gs, record["id"], record["version"]) if body.use_live_evidence else (None, None)
    try:
        windows = improvement.plan_windows(cov_start, cov_end, discovery_pct=body.discovery_pct,
                                           embargo_pct=body.embargo_pct, live_first=live_first,
                                           use_live=body.use_live_evidence)
    except improvement.StudyError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not windows["live_evidence_used"]:
        live = None
    d, h = windows["discovery"], windows["holdout"]
    ds = research_dataset(gs.db, row, step, d["start"], h["end"], body.lookback_days,
                          what="Improvement study (discovery to hold-out, computed from the stored coverage)")
    if ds.base.empty:
        raise HTTPException(422, "the dataset has no closed candles in the study range")
    steps = len(features.step_cutoffs(ds, step, d["start"], h["end"]))
    limit = int(gs.analysis_cfg["performance"]["max_steps"])
    if steps == 0 or steps > limit:
        raise HTTPException(422, f"{steps} step candles in the study range (limit {limit}); the range is never "
                                 "shortened automatically")
    candles = research_outcome_candles(gs.db, row, ds.base_tf, ds.base.index[0].to_pydatetime(),
                                       h["end"] + timedelta(minutes=1), what="Improvement study")
    assumptions = _assumptions(gs, body.assumptions)
    study_id = "st_" + secrets.token_hex(8)
    now = gs.clock.now()
    params = {"strategy": {"id": record["id"], "version": record["version"], "fingerprint": record["fingerprint"],
                           "name": record["name"]},
              "series": row["key"], "coverage": {"start": iso(cov_start), "end": iso(cov_end)},
              "windows": improvement._iso_windows(windows), "assumptions": assumptions.to_mapping(),
              "lookback_days": body.lookback_days, "use_live_evidence": body.use_live_evidence, "steps": steps}
    with gs.db.transaction() as con:
        improvement_repo.create_study(con, study_id, strategy_id=record["id"], version=record["version"], now=now,
                                      params=params)

    def target(cancel: threading.Event) -> None:
        with gs.db.transaction() as con:
            improvement_repo.set_study(con, study_id, "running", at=gs.clock.now())

        def progress(done: int, total: int) -> None:
            with gs.db.transaction() as con:
                improvement_repo.set_progress(con, study_id, done, total)
        try:
            result = improvement.run_study(record=record, ds=ds, candles=candles, windows=windows, cfg=gs.analysis_cfg,
                                           assumptions=assumptions, spec=gs.spec, live=live, progress=progress,
                                           cancelled=cancel.is_set)
            with gs.db.transaction() as con:
                h_iso = result["windows"]["holdout"]
                reused = improvement_repo.overlapping_holdouts(con, record["id"], h_iso["start"], h_iso["end"], study_id)
                summary = []
                for p in result["proposals"]:
                    pid = "sp_" + secrets.token_hex(8)
                    lim = improvement.limitations(result, p, reused_holdout=reused)
                    improvement_repo.insert_proposal(con, pid, study_id=study_id, strategy_id=record["id"],
                                                     base_version=record["version"],
                                                     base_fingerprint=record["fingerprint"], p=clean(p),
                                                     candidate_fingerprint=builder.fingerprint(p["candidate"]),
                                                     limitations=lim, now=gs.clock.now())
                    summary.append({"id": pid, "kind": p["kind"], "title": p["title"],
                                    "verdict": p["validation"]["verdict"]})
                result["proposals"] = summary
                result["holdout_reuse"] = reused
                improvement_repo.set_study(con, study_id, "complete", at=gs.clock.now(), result=clean(result))
        except features.Cancelled:
            with gs.db.transaction() as con:
                improvement_repo.set_study(con, study_id, "cancelled", at=gs.clock.now(), error="cancelled")
        except Exception as exc:  # noqa: BLE001 - a failed study is recorded, never crashes the server
            with gs.db.transaction() as con:
                improvement_repo.set_study(con, study_id, "failed", at=gs.clock.now(),
                                           error=f"{type(exc).__name__}: {exc}")
    try:
        gs.perf_jobs.start(study_id, target)
    except RuntimeError as exc:
        with gs.db.transaction() as con:
            improvement_repo.set_study(con, study_id, "failed", at=gs.clock.now(), error="another job started first")
        raise HTTPException(409, "another evaluation is still running") from exc
    with gs.db.read() as con:
        return {"study": improvement_repo.study(con, study_id, with_result=False), "disclaimer": improvement.DISCLAIMER}


@router.get("")
def list_studies(request: Request, strategy_id: int | None = None) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        return {"studies": improvement_repo.studies(con, strategy_id=strategy_id)}


@router.get("/{study_id}")
def get_study(request: Request, study_id: str) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        s = improvement_repo.study(con, study_id)
    if s is None:
        raise HTTPException(404, f"unknown study {study_id}")
    return s


@router.post("/{study_id}/cancel")
def cancel_study(request: Request, study_id: str) -> dict[str, Any]:
    gs = services(request)
    return {"cancelled": gs.perf_jobs.cancel(study_id)}


# ------------------------------------------------------------------------------------------------ proposals
class DecisionIn(BaseModel):
    note: str | None = Field(None, max_length=1000)
    acknowledge_insufficient_sample: bool = False


@proposals_router.get("")
def list_proposals(request: Request, strategy_id: int | None = None,
                   status: str | None = Query(None, pattern="^(validated|inconclusive|not_improved|approved|rejected)$"),
                   study_id: str | None = None) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        items = improvement_repo.proposals(con, strategy_id=strategy_id, status=status, study_id=study_id)
    return {"proposals": items, "note": "Proposals are hypotheses validated once on unseen data. None is adopted "
                                        "without your approval."}


@proposals_router.get("/{pid}")
def get_proposal(request: Request, pid: str) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        p = improvement_repo.proposal(con, pid)
        if p is None:
            raise HTTPException(404, f"unknown proposal {pid}")
        p["versions"] = strategy_repo.versions(con, p["strategy_id"])
    return p


@proposals_router.post("/{pid}/approve")
def approve(request: Request, pid: str, body: DecisionIn) -> dict[str, Any]:
    """The user's decision: save the validated candidate as a NEW strategy version (monitoring is unchanged)."""
    gs = services(request)
    with gs.db.read() as con:
        p = improvement_repo.proposal(con, pid)
        current = strategy_repo.get(con, p["strategy_id"]) if p else None
    if p is None:
        raise HTTPException(404, f"unknown proposal {pid}")
    if p["status"] in ("approved", "rejected"):
        raise HTTPException(409, f"proposal already {p['status']}")
    if p["status"] == "not_improved":
        raise HTTPException(409, "the candidate did not improve on the unseen holdout; it cannot be adopted")
    if p["status"] == "inconclusive" and not body.acknowledge_insufficient_sample:
        raise HTTPException(409, "the holdout sample is too small to support this change; approving needs "
                                 "acknowledge_insufficient_sample=true")
    if current is None or current["archived"]:
        raise HTTPException(409, "the strategy no longer exists or is archived")
    if current["current_version"] != p["base_version"]:
        raise HTTPException(409, f"the strategy changed since the study (now v{current['current_version']}, proposal "
                                 f"based on v{p['base_version']}); run a new study")
    v = p["validation"] or {}
    note = (f"approved proposal {pid} ({p['title']}): {p['diff']}. Holdout {v.get('holdout', {}).get('start')} to "
            f"{v.get('holdout', {}).get('end')}: {v.get('why')}" + (f". User note: {body.note}" if body.note else ""))
    now = gs.clock.now()
    with gs.db.transaction() as con:
        version, created = strategy_repo.save(con, p["strategy_id"], p["candidate"], now, note[:2000])
        improvement_repo.decide(con, pid, "approved", at=now, note=body.note, resulting_version=version)
        out = improvement_repo.proposal(con, pid)
        out["versions"] = strategy_repo.versions(con, p["strategy_id"])
    return {"proposal": out, "new_version": version, "created": created,
            "note": "Saved as a new version. Live monitoring still evaluates the versions you chose; add the new "
                    "version to the monitor to validate it live."}


@proposals_router.post("/{pid}/reject")
def reject(request: Request, pid: str, body: DecisionIn) -> dict[str, Any]:
    gs = services(request)
    with gs.db.transaction() as con:
        if not improvement_repo.decide(con, pid, "rejected", at=gs.clock.now(), note=body.note):
            if improvement_repo.proposal(con, pid) is None:
                raise HTTPException(404, f"unknown proposal {pid}")
            raise HTTPException(409, "proposal already decided")
        return {"proposal": improvement_repo.proposal(con, pid)}


# ------------------------------------------------------------------------------------------------ assistant
class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    mode: str = Field("auto", pattern="^(auto|local)$")


@assistant_router.get("/status")
def assistant_status(request: Request) -> dict[str, Any]:
    gs = services(request)
    return {"llm": llm.status(gs.settings), "questions": assistant.QUESTIONS,
            "note": "Answers come from stored GoldScalp records and cite them. The assistant cannot change strategies "
                    "or place orders."}


@assistant_router.post("/ask")
def ask(request: Request, body: AskIn) -> dict[str, Any]:
    gs = services(request)
    return clean(assistant.ask(gs, body.question, mode=body.mode, client=getattr(request.app.state, "assistant_client", None)))


@assistant_router.get("/log")
def assistant_log(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        return {"log": live_repo.assistant_log(con, limit)}
