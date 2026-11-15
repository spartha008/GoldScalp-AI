"""Analysis routes. Results are HISTORICAL observations computed from closed bars as of now.

Granular routes (one engine each) and /intelligence (everything for one chart timeframe) share the same
frames and the same configuration (config/analysis.json)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request

from goldscalp.analysis import (
    fvg,
    intelligence,
    liquidity,
    market_structure,
    multi_timeframe,
    order_blocks,
    signals,
    volatility,
)
from goldscalp.analysis import sessions as sessions_mod
from goldscalp.analysis import structure as structure_v1
from goldscalp.analysis.config import AnalysisConfig, AnalysisConfigError
from goldscalp.api.services import AppServices, active_series_key, clean, load_tf, pick_series, services
from goldscalp.core.kinds import DataKind, envelope
from goldscalp.core.timeframes import ALL_TIMEFRAMES, Timeframe
from goldscalp.storage import signal_repo

router = APIRouter(prefix="/api/analysis")
signals_router = APIRouter(prefix="/api/signals")
NOTE = "Historical observations from closed candles; not predictions or advice."


def _tf(value: str) -> Timeframe:
    try:
        return Timeframe.parse(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _frames(gs: AppServices, row: Any, now: datetime) -> dict[str, pd.DataFrame]:
    return {t.value: load_tf(gs.db, row, t, intelligence.FRAME_LIMITS[t.value], as_of=now) for t in ALL_TIMEFRAMES}


def _setup(request: Request, series: str | None) -> tuple[AppServices, datetime, Any]:
    gs = services(request)
    return gs, gs.clock.now(), pick_series(gs.db, series, active_series_key(gs))


def _display_cfg(gs: AppServices, tf: Timeframe, swing_strength: int | None) -> AnalysisConfig:
    if swing_strength is None:
        return gs.analysis_cfg
    try:
        return gs.analysis_cfg.with_swing_strength(tf.value, swing_strength)
    except AnalysisConfigError as exc:
        raise HTTPException(422, str(exc)) from exc


def _empty(now: datetime) -> dict[str, Any]:
    return envelope(DataKind.HISTORICAL, {"message": "no candles stored yet"}, generated_at=now)


def _respond(data: dict[str, Any], row: Any, now: datetime, cfg: AnalysisConfig) -> dict[str, Any]:
    payload = {"series": row["key"], **clean(data), "config_fingerprint": cfg.fingerprint, "note": NOTE}
    return envelope(DataKind.HISTORICAL, payload, generated_at=now, source=row["source_id"])


@router.get("/overview")
def overview(request: Request, tf: str = Query("M5"), series: str | None = Query(None),
             swing_strength: int = Query(3, ge=1, le=10)) -> dict[str, Any]:
    """Phase 1 summary (structure@1, sessions@1, vol@1). Kept for compatibility; see /intelligence."""
    gs, now, row = _setup(request, series)
    timeframe = _tf(tf)
    if row is None:
        return _empty(now)
    frames = {t.value: load_tf(gs.db, row, t, 400, as_of=now) for t in ALL_TIMEFRAMES}
    chart = frames[timeframe.value]
    session_frame = frames["M5"] if len(frames["M5"]) else frames["M1"]
    hourly = load_tf(gs.db, row, Timeframe.H1, 24 * 40, as_of=now)
    data = {
        "series": row["key"], "tf": timeframe.value,
        "structure": structure_v1.analyse(chart, timeframe.seconds, swing_strength, swing_strength),
        "sessions": sessions_mod.summary(session_frame, now),
        "volatility": volatility.summary(frames, hourly, now),
        "note": NOTE,
    }
    return envelope(DataKind.HISTORICAL, clean(data), generated_at=now, source=row["source_id"])


def live_context(gs: AppServices, row: Any) -> tuple[str | None, float | None, str]:
    """Market state and spread for signals. Only the ACTIVE live series has a current market state;
    any other series is HISTORICAL (and therefore never produces a live signal)."""
    if not (row["kind"] == "live" and row["key"] == active_series_key(gs)):
        return "HISTORICAL", None, "not a live series"
    state, _ = gs.market.state()
    quote = gs.market.market_quote()
    spread = None
    if quote is not None and quote.bid is not None and quote.ask is not None:
        spread = float(quote.ask - quote.bid)
    return state.value, spread, "provider bid/ask" if spread is not None else "unknown (no bid/ask)"


def _record(gs: AppServices, row: Any, signal: dict[str, Any]) -> None:
    signal["stored_id"] = None
    if row["kind"] == "live" and row["key"] == active_series_key(gs):
        with gs.db.transaction() as con:
            signal["recorded"] = signal_repo.record_if_changed(con, signal)
            signal["stored_id"] = signal_repo.stored_id_for(con, signal)


@router.get("/intelligence")
def intelligence_view(request: Request, tf: str = Query("M5"), series: str | None = Query(None),
                      swing_strength: int | None = Query(None, ge=1, le=10)) -> dict[str, Any]:
    """Overlays for one timeframe plus multi-timeframe, sessions, volatility and the current signal, all
    computed as of now from CLOSED candles. ``swing_strength`` changes the CHART overlays only."""
    gs, now, row = _setup(request, series)
    timeframe = _tf(tf)
    if row is None:
        return _empty(now)
    state, spread, spread_source = live_context(gs, row)
    result = intelligence.run(_frames(gs, row, now), now, gs.analysis_cfg, chart_tf=timeframe, mode="live",
                              series=row["key"], market_state=state, spread=spread, spread_source=spread_source,
                              source=row["source_id"], generated_at=now, display_swing_strength=swing_strength)
    result = clean(result)
    _record(gs, row, result["signal"])
    result["series"] = row["key"]
    return envelope(DataKind.HISTORICAL, result, generated_at=now, source=row["source_id"])


@router.get("/structure")
def structure_view(request: Request, tf: str = Query("M5"), series: str | None = Query(None),
                   swing_strength: int | None = Query(None, ge=1, le=10),
                   limit: int = Query(40, ge=0, le=1000)) -> dict[str, Any]:
    """structure@2: confirmed swings (HH/HL/LH/LL), BOS/CHoCH, state and developing observations."""
    gs, now, row = _setup(request, series)
    timeframe = _tf(tf)
    if row is None:
        return _empty(now)
    cfg = _display_cfg(gs, timeframe, swing_strength)
    frame = load_tf(gs.db, row, timeframe, intelligence.FRAME_LIMITS[timeframe.value], as_of=now)
    return _respond({"structure": market_structure.analyse(frame, timeframe, now, cfg, limit=limit)}, row, now, cfg)


@router.get("/liquidity")
def liquidity_view(request: Request, tf: str = Query("M5"), series: str | None = Query(None),
                   swing_strength: int | None = Query(None, ge=1, le=10)) -> dict[str, Any]:
    """liquidity@1: EQH/EQL, PDH/PDL, PWH/PWL, session levels and sweep / break observations."""
    gs, now, row = _setup(request, series)
    timeframe = _tf(tf)
    if row is None:
        return _empty(now)
    cfg = _display_cfg(gs, timeframe, swing_strength)
    frame = load_tf(gs.db, row, timeframe, intelligence.FRAME_LIMITS[timeframe.value], as_of=now)
    h1 = load_tf(gs.db, row, Timeframe.H1, intelligence.FRAME_LIMITS["H1"], as_of=now)
    return _respond({"liquidity": liquidity.analyse(frame, timeframe, now, cfg, reference=h1,
                                                    reference_tf=Timeframe.H1)}, row, now, cfg)


@router.get("/fvg")
def fvg_view(request: Request, tf: str = Query("M5"), series: str | None = Query(None),
             limit: int = Query(30, ge=0, le=1000)) -> dict[str, Any]:
    """fvg@2: fair value gaps with mitigation."""
    gs, now, row = _setup(request, series)
    timeframe = _tf(tf)
    if row is None:
        return _empty(now)
    frame = load_tf(gs.db, row, timeframe, intelligence.FRAME_LIMITS[timeframe.value], as_of=now)
    return _respond({"fvg": fvg.analyse(frame, timeframe, now, gs.analysis_cfg, limit=limit)}, row, now, gs.analysis_cfg)


@router.get("/order-blocks")
def order_blocks_view(request: Request, tf: str = Query("M5"), series: str | None = Query(None),
                      swing_strength: int | None = Query(None, ge=1, le=10),
                      limit: int = Query(20, ge=0, le=1000)) -> dict[str, Any]:
    """ob@1: algorithmic order-block candidates linked to their structure break."""
    gs, now, row = _setup(request, series)
    timeframe = _tf(tf)
    if row is None:
        return _empty(now)
    cfg = _display_cfg(gs, timeframe, swing_strength)
    frame = load_tf(gs.db, row, timeframe, intelligence.FRAME_LIMITS[timeframe.value], as_of=now)
    return _respond({"order_blocks": order_blocks.analyse(frame, timeframe, now, cfg, limit=limit)}, row, now, cfg)


@router.get("/multi-timeframe")
def multi_timeframe_view(request: Request, series: str | None = Query(None)) -> dict[str, Any]:
    """mtf@1: H1/M15/M5/M1 side by side with disagreement explanations."""
    gs, now, row = _setup(request, series)
    if row is None:
        return _empty(now)
    results = multi_timeframe.build(_frames(gs, row, now), now, gs.analysis_cfg)
    return _respond({"multi_timeframe": multi_timeframe.summarise(results, now)}, row, now, gs.analysis_cfg)


@router.get("/sessions")
def sessions_view(request: Request, series: str | None = Query(None)) -> dict[str, Any]:
    """sessions@2: current session, overlaps, ranges, volatility comparison and breakouts."""
    gs, now, row = _setup(request, series)
    if row is None:
        return _empty(now)
    m5 = load_tf(gs.db, row, Timeframe.M5, intelligence.FRAME_LIMITS["M5"], as_of=now)
    h1 = load_tf(gs.db, row, Timeframe.H1, intelligence.FRAME_LIMITS["H1"], as_of=now)
    atr = volatility.wilder(m5[m5["closed"]], int(gs.analysis_cfg["volatility"]["atr_period"])) if len(m5) else None
    data = sessions_mod.analyse(m5, Timeframe.M5, now, gs.analysis_cfg, history=h1, history_tf=Timeframe.H1,
                                atr_value=atr)
    return _respond({"sessions": data}, row, now, gs.analysis_cfg)


@router.get("/volatility")
def volatility_view(request: Request, series: str | None = Query(None)) -> dict[str, Any]:
    """vol@2: ATR, ADR, rolling volatility, expansion/contraction, candle ranges per timeframe."""
    gs, now, row = _setup(request, series)
    if row is None:
        return _empty(now)
    frames = _frames(gs, row, now)
    data = volatility.analyse(frames, now, gs.analysis_cfg, daily_source=frames["H1"], daily_tf=Timeframe.H1)
    return _respond({"volatility": data}, row, now, gs.analysis_cfg)


@router.get("/config")
def analysis_config(request: Request) -> dict[str, Any]:
    gs = services(request)
    return {**gs.analysis_cfg.view(), "definitions": signals.DEFINITIONS, "score_label": signals.SCORE_LABEL,
            "source": "config/analysis.json"}


@signals_router.get("")
def signal_history(request: Request, mode: str | None = Query(None, pattern="^(live|replay)$"),
                   series: str | None = Query(None), replay_id: str | None = Query(None),
                   limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        items = signal_repo.history(con, mode=mode, series_key=series, replay_id=replay_id, limit=limit)
    return {"signals": items, "note": "Stored when the classification changes. " + signals.DISCLAIMER}


@signals_router.get("/current")
def current_signal(request: Request, series: str | None = Query(None)) -> dict[str, Any]:
    """Signal centre: the current rule-based signal as of now (stored when the classification changes)."""
    gs, now, row = _setup(request, series)
    if row is None:
        return _empty(now)
    state, spread, spread_source = live_context(gs, row)
    results = multi_timeframe.build(_frames(gs, row, now), now, gs.analysis_cfg, detail=signals.DETAIL_TIMEFRAMES)
    signal = clean(signals.evaluate(results, now, gs.analysis_cfg, mode="live", series=row["key"], market_state=state,
                                    spread=spread, spread_source=spread_source, source=row["source_id"],
                                    generated_at=now))
    _record(gs, row, signal)
    return envelope(DataKind.HISTORICAL, {"series": row["key"], "signal": signal}, generated_at=now,
                    source=row["source_id"])


@signals_router.get("/{signal_id}")
def signal_detail(request: Request, signal_id: str) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        item = signal_repo.get(con, signal_id)
    if item is None:
        raise HTTPException(404, f"unknown signal {signal_id!r}")
    return item
