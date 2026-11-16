"""Dashboard order ticket: manual market orders on the MT5 DEMO account (/api/live/demo/manual).

Uses the optional demo execution (GOLDSCALP_DEMO_EXECUTION_ENABLED) and its demo-only gateway: the account is re-read
before every order and anything that is not a DEMO account is refused (goldscalp/execution/manual.py)."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from goldscalp.api.services import clean, services
from goldscalp.execution.manual import TicketRefused
from goldscalp.execution.mirror import DemoUnavailable

router = APIRouter(prefix="/api/live/demo/manual")

Number = float | str


class OrderIn(BaseModel):
    side: Literal["buy", "sell"]
    volume: Number
    sl: Number | None = None            # required: a missing stop-loss is refused with a readable reason
    tp: Number | None = None
    confirm: str | None = Field(None, max_length=16)
    account_hash: str | None = Field(None, max_length=32)


class ProtectionIn(BaseModel):
    sl: Number
    tp: Number | None = None
    account_hash: str | None = Field(None, max_length=32)


class CloseIn(BaseModel):
    account_hash: str | None = Field(None, max_length=32)


def _ticket(request: Request) -> Any:
    ticket = getattr(services(request), "manual", None)
    if ticket is None:
        raise HTTPException(503, "the order ticket is not available in this process")
    return ticket


def _run(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return clean(fn(*args, **kwargs))
    except TicketRefused as exc:
        raise HTTPException(422, str(exc)) from exc
    except DemoUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("")
def status(request: Request) -> dict[str, Any]:
    return clean(_ticket(request).status())


@router.post("/orders", status_code=201)
def open_order(request: Request, body: OrderIn) -> dict[str, Any]:
    return _run(_ticket(request).place, side=body.side, volume=body.volume, sl=body.sl, tp=body.tp,
                confirm=body.confirm, account_hash=body.account_hash)


@router.post("/positions/{ticket}/protection")
def change_protection(request: Request, ticket: int, body: ProtectionIn) -> dict[str, Any]:
    return _run(_ticket(request).modify, ticket, sl=body.sl, tp=body.tp, account_hash=body.account_hash)


@router.post("/positions/{ticket}/close")
def close_position(request: Request, ticket: int, body: CloseIn) -> dict[str, Any]:
    return _run(_ticket(request).close, ticket, account_hash=body.account_hash)
