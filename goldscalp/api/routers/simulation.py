"""Paper-trading routes. Everything here is SIMULATED; nothing reaches a broker."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from goldscalp.api.services import clean, services
from goldscalp.core.clock import iso
from goldscalp.core.kinds import DataKind, envelope
from goldscalp.simulation.account import AccountConfig
from goldscalp.simulation.costs import CostModel
from goldscalp.simulation.instrument import InstrumentSpec, SpecError
from goldscalp.simulation.portfolio import SIMULATED_LABEL, OrderRejected
from goldscalp.storage import signal_repo

router = APIRouter(prefix="/api/sim")


class OrderIn(BaseModel):
    side: str = Field(pattern="^(buy|sell)$")
    lots: str
    stop_loss: str | None = None
    take_profit: str | None = None
    signal_id: str | None = Field(None, pattern="^sig_[0-9a-f]{20}$")


class EstimateIn(BaseModel):
    side: str = Field(pattern="^(buy|sell)$")
    lots: str


class ProtectionIn(BaseModel):
    stop_loss: str | None = None
    take_profit: str | None = None


class NewAccountIn(BaseModel):
    account: dict[str, Any]
    costs: dict[str, Any]
    instrument: dict[str, Any] | None = None


def _sim(request: Request, data: Any) -> dict[str, Any]:
    gs = services(request)
    return envelope(DataKind.SIMULATED, clean(data), generated_at=gs.clock.now(), notes=[SIMULATED_LABEL])


def _position_dict(p: Any) -> dict[str, Any]:
    out = {k: (format(v, "f") if isinstance(v, Decimal) else iso(v) if isinstance(v, datetime) else v)
           for k, v in asdict(p).items()}
    out["side"] = p.side.value
    return out


@router.get("/account")
def account(request: Request) -> dict[str, Any]:
    gs = services(request)
    acct = gs.engine.ensure_account()
    return _sim(request, {"summary": gs.engine.summary(), "instrument": acct.spec.to_mapping(),
                          "costs": acct.costs.to_mapping()})


@router.post("/accounts")
def new_account(request: Request, body: NewAccountIn) -> dict[str, Any]:
    """Start a NEW virtual account. The previous one is kept, inactive, with its history."""
    gs = services(request)
    try:
        config = AccountConfig.from_mapping(body.account)
        costs = CostModel.from_mapping(body.costs)
        spec = InstrumentSpec.from_mapping(body.instrument) if body.instrument is not None else gs.spec
    except (ValueError, SpecError) as exc:
        raise HTTPException(422, str(exc)) from exc
    acct = gs.engine.create_account(config, spec, costs)
    return _sim(request, {"account_id": acct.id, "summary": gs.engine.summary()})


@router.post("/estimate")
def estimate(request: Request, body: EstimateIn) -> dict[str, Any]:
    gs = services(request)
    try:
        return _sim(request, gs.engine.estimate(body.side, body.lots))
    except OrderRejected as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/orders")
def place_simulated_order(request: Request, body: OrderIn) -> dict[str, Any]:
    """Open a SIMULATED market position at the current simulated bid/ask."""
    gs = services(request)
    if body.signal_id is not None:
        with gs.db.read() as con:
            linked = signal_repo.get(con, body.signal_id)
        if linked is None:
            raise HTTPException(422, f"unknown signal {body.signal_id!r}")
        if linked["mode"] != "live":
            raise HTTPException(422, "only a live signal can be linked to a simulated position (replay signals are "
                                     "historical observations)")
    try:
        position = gs.engine.open_position(body.side, body.lots, body.stop_loss, body.take_profit,
                                           signal_id=body.signal_id)
    except OrderRejected as exc:
        raise HTTPException(422, str(exc)) from exc
    return _sim(request, {"position": _position_dict(position), "summary": gs.engine.summary()})


@router.post("/positions/{position_id}/close")
def close(request: Request, position_id: int) -> dict[str, Any]:
    gs = services(request)
    try:
        position = gs.engine.close_position(position_id)
    except OrderRejected as exc:
        raise HTTPException(422, str(exc)) from exc
    return _sim(request, {"position": _position_dict(position), "summary": gs.engine.summary()})


@router.patch("/positions/{position_id}")
def protect(request: Request, position_id: int, body: ProtectionIn) -> dict[str, Any]:
    gs = services(request)
    try:
        position = gs.engine.modify_protection(position_id, body.stop_loss, body.take_profit)
    except OrderRejected as exc:
        raise HTTPException(422, str(exc)) from exc
    return _sim(request, {"position": _position_dict(position)})


@router.get("/positions")
def positions(request: Request, status: str | None = Query(None, pattern="^(open|closed)$")) -> dict[str, Any]:
    gs = services(request)
    return _sim(request, gs.engine.positions_view(status))


@router.get("/ledger")
def ledger(request: Request, limit: int = Query(200, ge=1, le=5000)) -> dict[str, Any]:
    gs = services(request)
    return _sim(request, gs.engine.ledger_view(limit))


@router.get("/equity")
def equity(request: Request, limit: int = Query(2000, ge=1, le=20000)) -> dict[str, Any]:
    gs = services(request)
    return _sim(request, gs.engine.equity_history(limit))
