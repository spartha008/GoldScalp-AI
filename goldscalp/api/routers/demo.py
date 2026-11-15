"""Optional MT5 DEMO-account execution: status, per-version links and the kill switch (/api/live/demo).

Off by default (GOLDSCALP_DEMO_EXECUTION_ENABLED). Orders go only to an account that reports itself as a DEMO
account; the check runs before every order (goldscalp/execution/mt5_demo.py)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from goldscalp.api.services import clean, services
from goldscalp.execution.mirror import DemoUnavailable

router = APIRouter(prefix="/api/live/demo")


class LinkIn(BaseModel):
    enabled: bool
    confirm: str | None = Field(None, max_length=16)


class StopIn(BaseModel):
    close_positions: bool = False


def _mirror(request: Request) -> Any:
    mirror = services(request).demo
    if mirror is None:
        raise HTTPException(503, "demo execution is not available in this process")
    return mirror


@router.get("")
def status(request: Request) -> dict[str, Any]:
    return clean(_mirror(request).status())


@router.post("/links/{mid}")
def set_link(request: Request, mid: int, body: LinkIn) -> dict[str, Any]:
    try:
        out = _mirror(request).link(mid, enable=body.enabled, confirm=body.confirm)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except DemoUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    return clean(out)


@router.post("/stop")
def stop_all(request: Request, body: StopIn) -> dict[str, Any]:
    return clean(_mirror(request).stop_all(close_positions=body.close_positions))
