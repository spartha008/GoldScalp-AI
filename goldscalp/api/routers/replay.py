"""Replay routes. The server holds the cursor; the browser asks it to step (play, pause and speed are
browser-side timers). Analysis at each step sees only candles closed by the replay timestamp."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from goldscalp.analysis import intelligence, signals
from goldscalp.analysis.replay import ReplayError, ReplaySession, build_session
from goldscalp.api.services import AppServices, candles_payload, clean, load_dataset, pick_series, services
from goldscalp.core.clock import ensure_aware, iso, parse_iso_aware
from goldscalp.core.kinds import DataKind, envelope
from goldscalp.core.timeframes import Timeframe
from goldscalp.storage import signal_repo

router = APIRouter(prefix="/api/replay")
CHART_CANDLES = 600


class ReplayIn(BaseModel):
    series: str = Field(min_length=1, max_length=200)
    timeframe: str = "M5"
    start: str
    end: str
    lookback_days: int = Field(5, ge=1, le=20)
    swing_strength: int | None = Field(None, ge=1, le=10)


class StepIn(BaseModel):
    n: int = Field(1, ge=1, le=500)
    swing_strength: int | None = Field(None, ge=1, le=10)


class SeekIn(BaseModel):
    position: int = Field(ge=0)
    swing_strength: int | None = Field(None, ge=1, le=10)


def _parse_time(value: str, name: str) -> datetime:
    try:
        return parse_iso_aware(value)
    except ValueError as exc:
        raise HTTPException(422, f"{name}: {exc}") from exc


def _state(gs: AppServices, session: ReplaySession) -> dict[str, Any]:
    """Analysis at the cursor + chart candles + this replay's signal history (with forward outcomes)."""
    cursor = ensure_aware(session.cursor)
    frames = session.frames()
    base_visible = frames.get(session.dataset.base_tf.value)
    spread = None
    if base_visible is not None and len(base_visible):
        value = base_visible["spread"].iloc[-1]
        spread = float(value) if value is not None and np.isfinite(value) else None
    result = intelligence.run(frames, cursor, gs.analysis_cfg, chart_tf=session.step_tf, mode="replay",
                              series=session.dataset.series_key, market_state=None, spread=spread,
                              spread_source="historical candle spread" if spread is not None else "unknown in this dataset",
                              source=session.dataset.source_id, generated_at=gs.clock.now(), replay_id=session.id,
                              display_swing_strength=session.display_swing_strength)
    result = clean(result)
    cfg = gs.analysis_cfg
    with gs.db.transaction() as con:
        result["signal"]["recorded"] = signal_repo.record_if_changed(con, result["signal"])
        result["signal"]["stored_id"] = signal_repo.stored_id_for(con, result["signal"])
        for item in signal_repo.history(con, replay_id=session.id, limit=200):
            done = item.get("evaluation") and all(h["status"] == "complete" for h in item["evaluation"].get("horizons", []))
            if item["direction"] not in ("BULLISH", "BEARISH") or done or item["cutoff"] > iso(cursor):
                continue
            base = session.dataset.base
            after = base[(base.index >= pd.Timestamp(item["cutoff"]))
                         & ((base.index + pd.Timedelta(seconds=session.dataset.base_tf.seconds)) <= pd.Timestamp(cursor))]
            outcome = signals.forward_outcome(item, after, session.dataset.base_tf,
                                              list(cfg["signals"]["evaluation_horizons_m5"]))
            signal_repo.set_evaluation(con, item["id"], clean(outcome))
        history = signal_repo.history(con, replay_id=session.id, limit=100)
    chart = frames.get(session.step_tf.value)
    return {"replay": session.view(), "cursor": iso(cursor),
            "candles": candles_payload(chart.tail(CHART_CANDLES)) if chart is not None else [],
            "analysis": result, "signal_history": history,
            "note": ("Replay of stored historical candles. The analysis at each step uses only candles closed by "
                     "the replay timestamp; forward outcomes are historical observations.")}


def _session(gs: AppServices, replay_id: str) -> ReplaySession:
    session = gs.replays.get(replay_id)
    if session is None:
        raise HTTPException(404, f"unknown or expired replay {replay_id!r}")
    return session


def _respond(gs: AppServices, payload: dict[str, Any], session: ReplaySession) -> dict[str, Any]:
    return envelope(DataKind.HISTORICAL, payload, generated_at=gs.clock.now(), source=session.dataset.source_id,
                    as_of=ensure_aware(session.cursor))


@router.post("")
def create_replay(request: Request, body: ReplayIn) -> dict[str, Any]:
    gs = services(request)
    try:
        step_tf = Timeframe.parse(body.timeframe)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    start, end = _parse_time(body.start, "start"), _parse_time(body.end, "end")
    row = pick_series(gs.db, body.series)
    dataset = load_dataset(gs.db, row, step_tf, start, end, body.lookback_days)
    try:
        session = build_session(dataset, step_tf, start, end, gs.clock.now())
    except ReplayError as exc:
        raise HTTPException(422, str(exc)) from exc
    session.display_swing_strength = body.swing_strength
    gs.replays.add(session)
    with session.lock:
        return _respond(gs, _state(gs, session), session)


@router.get("")
def list_replays(request: Request) -> dict[str, Any]:
    return {"replays": services(request).replays.list()}


@router.get("/{replay_id}")
def get_replay(request: Request, replay_id: str,
               swing_strength: int | None = Query(None, ge=1, le=10)) -> dict[str, Any]:
    gs = services(request)
    session = _session(gs, replay_id)
    with session.lock:
        session.display_swing_strength = swing_strength
        return _respond(gs, _state(gs, session), session)


@router.post("/{replay_id}/step")
def step_replay(request: Request, replay_id: str, body: StepIn) -> dict[str, Any]:
    gs = services(request)
    session = _session(gs, replay_id)
    with session.lock:
        session.display_swing_strength = body.swing_strength
        try:
            session.step(body.n)
        except ReplayError as exc:
            raise HTTPException(422, str(exc)) from exc
        return _respond(gs, _state(gs, session), session)


@router.post("/{replay_id}/seek")
def seek_replay(request: Request, replay_id: str, body: SeekIn) -> dict[str, Any]:
    gs = services(request)
    session = _session(gs, replay_id)
    with session.lock:
        session.display_swing_strength = body.swing_strength
        try:
            session.seek(body.position)
        except ReplayError as exc:
            raise HTTPException(422, str(exc)) from exc
        return _respond(gs, _state(gs, session), session)


@router.delete("/{replay_id}")
def delete_replay(request: Request, replay_id: str) -> dict[str, Any]:
    gs = services(request)
    removed = gs.replays.remove(replay_id)
    with gs.db.transaction() as con:
        deleted = signal_repo.delete_replay(con, replay_id)
    if not removed and not deleted:
        raise HTTPException(404, f"unknown replay {replay_id!r}")
    return {"deleted": removed, "signals_deleted": deleted}
