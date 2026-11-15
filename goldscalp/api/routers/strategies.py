"""Strategy Builder routes (strategy@1), the live strategy monitor and the strategy journal.

Paper integration is USER-INITIATED: POST /api/strategies/{id}/paper recomputes the setup on the server
and opens one SIMULATED paper position through the existing paper engine. Nothing opens automatically,
nothing reaches MT5, and no real orders, positions or account data are read or modified."""
from __future__ import annotations

import hashlib
from datetime import timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from goldscalp.analysis import intelligence, multi_timeframe, signals
from goldscalp.api.routers.analysis import live_context
from goldscalp.api.services import AppServices, active_series_key, clean, load_tf, pick_series, services
from goldscalp.backtesting import engine, features
from goldscalp.core.clock import iso, parse_iso_aware
from goldscalp.core.timeframes import ALL_TIMEFRAMES
from goldscalp.reports import exports
from goldscalp.simulation.portfolio import OrderRejected
from goldscalp.simulation.positions import Side
from goldscalp.storage import journal_repo, signal_repo, strategy_repo
from goldscalp.strategies import builder, conditions

router = APIRouter(prefix="/api/strategies")
journal_router = APIRouter(prefix="/api/journal")


def _validate(data: Any) -> dict[str, Any]:
    try:
        return builder.validate(data)
    except builder.StrategyError as exc:
        raise HTTPException(422, str(exc)) from exc


def _get(gs: AppServices, sid: int, version: int | None = None) -> dict[str, Any]:
    with gs.db.read() as con:
        record = strategy_repo.get(con, sid, version)
    if record is None:
        raise HTTPException(404, f"unknown strategy {sid}" + (f" version {version}" if version else ""))
    return record


@router.get("/catalogue")
def catalogue() -> dict[str, Any]:
    return builder.catalogue()


@router.post("/validate")
def validate(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    config = _validate(body)
    return {"valid": True, "config": config, "fingerprint": builder.fingerprint(config),
            "timeframes": sorted(builder.timeframes_used(config)), "conditions_text": builder.describe(config)}


@router.get("")
def list_strategies(request: Request, include_archived: bool = Query(False)) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        return {"strategies": strategy_repo.list_all(con, include_archived)}


@router.post("", status_code=201)
def create_strategy(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    gs = services(request)
    note = body.pop("note", None) if isinstance(body, dict) else None
    config = _validate(body)
    with gs.db.transaction() as con:
        sid = strategy_repo.create(con, config, gs.clock.now(), note)
    return _get(gs, sid)


@router.post("/import", status_code=201)
def import_strategy(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Import an exported JSON file (or a bare configuration) as a NEW strategy."""
    gs = services(request)
    config = _validate(body.get("config", body) if isinstance(body, dict) else body)
    with gs.db.transaction() as con:
        sid = strategy_repo.create(con, config, gs.clock.now(), "imported from JSON")
    return _get(gs, sid)


@router.get("/{sid}")
def get_strategy(request: Request, sid: int, version: int | None = Query(None, ge=1)) -> dict[str, Any]:
    return _get(services(request), sid, version)


@router.put("/{sid}")
def save_strategy(request: Request, sid: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    gs = services(request)
    note = body.pop("note", None) if isinstance(body, dict) else None
    config = _validate(body)
    try:
        with gs.db.transaction() as con:
            version, created = strategy_repo.save(con, sid, config, gs.clock.now(), note)
    except KeyError as exc:
        raise HTTPException(404, f"unknown strategy {sid}") from exc
    return {**_get(gs, sid), "created_version": created}


@router.delete("/{sid}")
def archive_strategy(request: Request, sid: int) -> dict[str, Any]:
    gs = services(request)
    with gs.db.transaction() as con:
        if not strategy_repo.archive(con, sid, gs.clock.now()):
            raise HTTPException(404, f"unknown strategy {sid}")
    return {"archived": sid, "note": "versions are kept because research runs and journal entries refer to them"}


@router.get("/{sid}/versions")
def strategy_versions(request: Request, sid: int) -> dict[str, Any]:
    gs = services(request)
    _get(gs, sid)
    with gs.db.read() as con:
        return {"versions": strategy_repo.versions(con, sid)}


@router.get("/{sid}/export.json")
def export_strategy(request: Request, sid: int, version: int | None = Query(None, ge=1)) -> JSONResponse:
    record = _get(services(request), sid, version)
    return JSONResponse(exports.strategy_json(record), headers={
        "Content-Disposition": f'attachment; filename="goldscalp-strategy-{sid}-v{record["version"]}.json"'})


# ------------------------------------------------------------------------------- live monitor and paper integration
def live_setup(gs: AppServices, record: dict[str, Any], series: str | None = None) -> dict[str, Any]:
    """The strategy evaluated NOW on live data with the backtest's own condition and planning code."""
    now = gs.clock.now()
    row = pick_series(gs.db, series, active_series_key(gs))
    if row is None:
        return {"setup": None, "gates": ["no candles stored yet"]}
    config = record["config"]
    state, spread, spread_source = live_context(gs, row)
    frames = {t.value: load_tf(gs.db, row, t, intelligence.FRAME_LIMITS[t.value], as_of=now) for t in ALL_TIMEFRAMES}
    detail = tuple(sorted(builder.detail_timeframes(config) | set(signals.DETAIL_TIMEFRAMES)))
    results = multi_timeframe.build(frames, now, gs.analysis_cfg, detail=detail)
    sig = clean(signals.evaluate(results, now, gs.analysis_cfg, mode="live", series=row["key"], market_state=state,
                                 spread=spread, spread_source=spread_source, source=row["source_id"], generated_at=now))
    snap = features.snapshot(results, now, gs.analysis_cfg, base=frames["M1"], signal=sig)
    snap["spread"] = spread
    decision = conditions.decide(config, snap)
    gates = [] if state == "LIVE" else [f"market data is {state}, not LIVE: no live strategy setup"]
    quote = gs.market.market_quote()
    plans = {}
    if quote is not None and quote.bid is not None and quote.ask is not None:
        bid, ask = Decimal(str(quote.bid)), Decimal(str(quote.ask))
        equity = Decimal(str(gs.engine.summary().get("equity") or gs.engine.summary()["balance"]))
        for side_name in conditions.sides(config):
            side = Side.BUY if side_name == "long" else Side.SELL
            entry = ask if side is Side.BUY else bid
            stop, stop_source, _ = engine.plan_stop_level(config["exit"]["stop"], side, snap, bid, ask)
            target = engine.plan_target_level(config["exit"]["target"], side, entry, stop, snap)
            risk_per_oz = abs(entry - stop) if stop is not None else None
            lots = engine.size_lots(config["risk"], equity, risk_per_oz, gs.spec)
            plans[side_name] = {"side": side.value, "entry_assumption": f"simulated market fill at the current "
                                f"{'ask' if side is Side.BUY else 'bid'} {entry} plus the paper slippage",
                                "stop": None if stop is None else str(round(stop, 2)), "stop_source": stop_source,
                                "target": None if target is None else str(round(target, 2)),
                                "lots": None if lots is None else str(lots),
                                "max_holding_minutes": config["exit"]["max_holding_minutes"],
                                "sizing": config["risk"]["sizing"]}
    else:
        gates.append("no current bid/ask quote")
    setup = decision["setup"] if not gates else None
    setup_id = None
    if setup:
        setup_id = "su_" + hashlib.sha256(f"{record['fingerprint']}|{snap['cutoff']}|{setup}".encode()).hexdigest()[:20]
    return clean({"strategy": {"id": record["id"], "name": record["name"], "version": record["version"],
                               "fingerprint": record["fingerprint"]},
                  "series": row["key"], "market_state": state, "as_of": iso(now), "setup": setup, "setup_id": setup_id,
                  "ambiguous": decision["ambiguous"], "gates": gates, "evaluations": decision["evaluations"],
                  "plan": plans.get(setup) if setup else None, "plans": plans, "signal": snap["signal"],
                  "note": "A live evaluation of the strategy's conditions. It is an observation, not advice; "
                          "a simulated paper position opens only when you ask for one."})


@router.get("/{sid}/live")
def strategy_live(request: Request, sid: int, series: str | None = Query(None)) -> dict[str, Any]:
    gs = services(request)
    return live_setup(gs, _get(gs, sid), series)


@router.post("/{sid}/paper", status_code=201)
def strategy_paper(request: Request, sid: int) -> dict[str, Any]:
    """Open ONE simulated paper position from the strategy's current live setup (user-initiated)."""
    gs = services(request)
    record = _get(gs, sid)
    live = live_setup(gs, record)
    if not live.get("setup"):
        reasons = live.get("gates") or [f"{e['side']}: " + "; ".join(c["evidence"] for c in e["conditions"] if not c["passed"])
                                         for e in (live.get("evaluations") or {}).values()]
        raise HTTPException(409, "no live strategy setup now: " + " | ".join(reasons or ["conditions not met"]))
    plan = live["plan"]
    if not plan or plan.get("lots") is None:
        raise HTTPException(409, "the planned size is below the minimum lot for this risk setting")
    try:
        position = gs.engine.open_position(plan["side"], plan["lots"], plan["stop"], plan["target"],
                                           signal_id=None)
    except OrderRejected as exc:
        raise HTTPException(422, str(exc)) from exc
    signal_id = None
    with gs.db.transaction() as con:
        stored = signal_repo.latest(con, "live", live["series"])
        if stored is not None and live["signal"] and stored["direction"] == live["signal"]["direction"]:
            signal_id = stored["id"]
        jid = journal_repo.insert(
            con, strategy_id=record["id"], strategy_version=record["version"], setup_id=live["setup_id"],
            signal_id=signal_id, signal_time=live["as_of"], position_id=position.id, opened_at=gs.clock.now(),
            planned={k: plan[k] for k in ("stop", "stop_source", "target", "max_holding_minutes", "sizing", "lots",
                                          "entry_assumption")},
            entry_costs={"fill_price": str(position.open_price), "spread": str(position.open_spread),
                         "spread_source": position.open_spread_source, "slippage": str(position.open_slippage),
                         "commission": str(position.open_commission), "price_state": position.open_price_state,
                         "source": position.open_source},
            conditions=live["evaluations"][live["setup"]]["conditions"])
    return {"journal_id": jid, "position_id": position.id, "setup": live["setup"], "plan": plan,
            "label": "SIMULATED paper position opened from a strategy setup (no broker order)"}


def _journal(gs: AppServices, strategy_id: int | None) -> list[dict[str, Any]]:
    now = gs.clock.now()
    with gs.db.read() as con:
        items = journal_repo.entries(con, strategy_id=strategy_id)
    for e in items:
        hold = e["planned"].get("max_holding_minutes")
        e["holding_exceeded"] = bool(e["status"] == "open" and hold and
                                     now > parse_iso_aware(e["opened_at"]) + timedelta(minutes=int(hold)))
        if e["holding_exceeded"]:
            e["note"] = f"open longer than the planned {hold} minutes: the strategy's time exit has passed"
    return items


@journal_router.get("")
def journal(request: Request, strategy_id: int | None = Query(None)) -> dict[str, Any]:
    return {"entries": _journal(services(request), strategy_id),
            "note": "Strategy-linked SIMULATED paper positions. Outcomes come from the paper simulator."}


@journal_router.get("/export.csv")
def journal_csv(request: Request, strategy_id: int | None = Query(None)) -> PlainTextResponse:
    return PlainTextResponse(exports.journal_csv(_journal(services(request), strategy_id)), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="goldscalp-strategy-journal.csv"'})
