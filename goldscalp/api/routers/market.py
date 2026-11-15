"""Market data routes: quote/status (current), candles (historical or current), provider control."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from goldscalp import __version__
from goldscalp.api.services import active_series_key, candles_payload, clean, load_tf, pick_series, services
from goldscalp.core.clock import parse_iso_aware
from goldscalp.core.kinds import DataKind, MarketState, envelope
from goldscalp.core.timeframes import Timeframe
from goldscalp.data import gaps
from goldscalp.data.providers.base import ProviderError
from goldscalp.storage import candle_repo

router = APIRouter(prefix="/api")


class ProviderIn(BaseModel):
    provider: str = Field(pattern="^(mt5|tiingo|gold-api|none)$")
    symbol: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._#!+\-]{1,32}$")


def _current(request: Request, snap: dict[str, Any]) -> dict[str, Any]:
    gs = services(request)
    q = snap["quote"]
    return envelope(DataKind.CURRENT, clean(snap), generated_at=gs.clock.now(),
                    source=snap["provider"]["id"] if snap["provider"] else None,
                    as_of=parse_iso_aware(q["quote_time"]) if q else None, state=MarketState(snap["state"]))


@router.get("/health")
def health() -> dict[str, str]:
    """Process liveness only. Says nothing about data freshness."""
    return {"status": "ok"}


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    gs = services(request)
    now = gs.clock.now()
    snap = gs.market.snapshot()
    with gs.db.read() as con:
        series = candle_repo.list_series(con)
    data = {"version": __version__, "schema_version": gs.db.schema_version(), "market": snap, "series": series,
            "instrument": gs.spec.to_mapping(), "live_provider_setting": gs.settings.live_provider,
            "active_provider": gs.market.providers_view()["active"],
            "safety": ("Educational paper trading. No real-money orders. Optional MT5 DEMO-account execution is off by "
                       "default and refuses any account that is not a DEMO account.")}
    return envelope(DataKind.CURRENT, clean(data), generated_at=now, source=snap["provider"]["id"]
                    if snap["provider"] else None, state=MarketState(snap["state"]))


@router.get("/market/quote")
def quote(request: Request) -> dict[str, Any]:
    return _current(request, services(request).market.snapshot())


@router.get("/market/stream")
async def stream(request: Request) -> StreamingResponse:
    """Server-Sent Events: the quote snapshot whenever it changes (heartbeat every 15 s)."""
    gs = services(request)

    async def events():
        last, idle = None, 0
        while not await request.is_disconnected():
            snap = gs.market.snapshot()
            payload = json.dumps(_current(request, snap))
            key = (snap["state"], json.dumps(snap["quote"]), snap["reason"][:60],
                   json.dumps(snap.get("latest_source_change")))
            if key != last or idle >= 15:
                last, idle = key, 0
                yield f"event: quote\ndata: {payload}\n\n"
            else:
                idle += 1
            await asyncio.sleep(1.0)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/market/providers")
def providers(request: Request) -> dict[str, Any]:
    gs = services(request)
    snap = gs.market.snapshot()
    return envelope(DataKind.CURRENT, clean(gs.market.providers_view()), generated_at=gs.clock.now(),
                    state=MarketState(snap["state"]))


@router.post("/market/provider")
async def select_provider(request: Request, body: ProviderIn) -> dict[str, Any]:
    """Switch the market-data provider (and, for MT5, optionally the broker symbol). Recorded as an event."""
    gs = services(request)
    try:
        await gs.market.select(body.provider, symbol=body.symbol if body.provider == "mt5" else None)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _current(request, gs.market.snapshot())


@router.post("/market/reconnect")
async def reconnect(request: Request) -> dict[str, Any]:
    gs = services(request)
    await gs.market.reconnect()
    return _current(request, gs.market.snapshot())


@router.get("/market/mt5/symbols")
async def mt5_symbols(request: Request, query: str = Query("", max_length=32)) -> dict[str, Any]:
    """Gold candidates (ranked) and a symbol search in the connected MT5 terminal."""
    gs = services(request)
    provider = gs.market.service.provider
    if provider is None or getattr(provider, "info", None) is None or provider.info.id != "mt5":
        raise HTTPException(409, "MetaTrader 5 is not the active provider")
    try:
        result = await provider.list_symbols(query)
    except ProviderError as exc:
        raise HTTPException(503, str(exc)) from exc
    return envelope(DataKind.CURRENT, clean(result), generated_at=gs.clock.now(), source="mt5")


@router.get("/market/series")
def series(request: Request) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        rows = candle_repo.list_series(con)
    return envelope(DataKind.HISTORICAL, clean({"series": rows, "active": active_series_key(gs)}),
                    generated_at=gs.clock.now())


CALENDAR_TF = {"MN1": ("MS", "month"), "Y1": ("YS", "year")}   # chart-only: calendar periods in UTC
CALENDAR_SOURCE_LIMIT = 50_000                                     # H1 bars (about eight years of trading)


def _calendar_candles(gs: Any, tf: str, series: str | None) -> dict[str, Any]:
    """Monthly / yearly candles for the chart, built from the stored H1 bars (no new data source, no analysis
    timeframe). A period the stored history only partly covers is flagged ``partial``; the current one is forming."""
    rule, unit = CALENDAR_TF[tf]
    row = pick_series(gs.db, series, active_series_key(gs))
    now = gs.clock.now()
    if row is None:
        return envelope(DataKind.HISTORICAL, {"series": None, "tf": tf, "candles": [], "gaps": None,
                                              "message": "no candles stored yet"}, generated_at=now)
    src = load_tf(gs.db, row, Timeframe.H1, CALENDAR_SOURCE_LIMIT, as_of=now)
    src = src[src["closed"]] if not src.empty else src
    out: list[dict[str, Any]] = []
    if not src.empty:
        g = src.resample(rule, label="left", closed="left")
        frame = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                              "close": g["close"].last(), "tick_volume": g["tick_volume"].sum(min_count=1),
                              "spread": g["spread"].mean(), "sub_bars": g["open"].count()}).dropna(subset=["open"])
        step = pd.offsets.MonthBegin(1) if tf == "MN1" else pd.offsets.YearBegin(1)
        frame["closed"] = [(ts + step) <= pd.Timestamp(now) for ts in frame.index]
        frame["origin"] = "derived_from_H1"
        out = candles_payload(frame)
        first_source = src.index.min()
        for c, ts in zip(out, frame.index, strict=True):     # history starting after the period began: partial
            c["partial"] = bool(ts == frame.index[0] and first_source > ts + pd.Timedelta(days=3))
    is_active_live = row["kind"] == "live" and row["key"] == active_series_key(gs)
    data = {"series": {k: row[k] for k in ("key", "kind", "symbol", "source_id", "source_symbol", "candle_side",
                                           "description")},
            "tf": tf, "candles": out, "gaps": None, "source_tf": "H1",
            "source_first": src.index.min().isoformat() if not src.empty else None,
            "note": f"calendar {unit}s in UTC, built from stored H1 candles; display only (not an analysis timeframe)"}
    return envelope(DataKind.CURRENT if is_active_live else DataKind.HISTORICAL, clean(data), generated_at=now,
                    source=row["source_id"], as_of=src.index.max().to_pydatetime() if not src.empty else None,
                    state=MarketState(gs.market.snapshot()["state"]) if is_active_live else MarketState.HISTORICAL)


@router.get("/market/candles")
def candles(request: Request, tf: str = Query("M5"), series: str | None = Query(None),
            limit: int = Query(500, ge=10, le=5000),
            before: str | None = Query(None, description="page back: only bars opening before this time "
                                                         "(epoch seconds or ISO-8601 with offset)")) -> dict[str, Any]:
    gs = services(request)
    if tf.upper() in CALENDAR_TF:
        return _calendar_candles(gs, tf.upper(), series)
    try:
        timeframe = Timeframe.parse(tf)
        end = None
        if before is not None:
            end = (datetime.fromtimestamp(int(before), UTC) if before.strip().lstrip("-").isdigit()
                   else parse_iso_aware(before))
            if end is None:
                raise ValueError(f"invalid 'before' time: {before!r}")
    except (ValueError, OverflowError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    row = pick_series(gs.db, series, active_series_key(gs))
    now = gs.clock.now()
    if row is None:
        return envelope(DataKind.HISTORICAL, {"series": None, "tf": timeframe.value, "candles": [],
                                              "gaps": None, "message": "no candles stored yet"},
                        generated_at=now)
    frame = load_tf(gs.db, row, timeframe, limit, as_of=now, end=end)
    closed = frame[frame["closed"]] if not frame.empty else frame
    gap = gaps.gap_report(closed.index, timeframe, gs.spec.schedule) if len(closed) else None
    is_active_live = row["kind"] == "live" and row["key"] == active_series_key(gs)
    kind = DataKind.CURRENT if is_active_live and end is None else DataKind.HISTORICAL   # a page back is history
    data = {"series": {k: row[k] for k in ("key", "kind", "symbol", "source_id", "source_symbol", "candle_side",
                                           "description")},
            "tf": timeframe.value, "candles": candles_payload(frame), "gaps": gap}
    state = MarketState(gs.market.snapshot()["state"]) if is_active_live else MarketState.HISTORICAL
    return envelope(kind, clean(data), generated_at=now, source=row["source_id"],
                    as_of=frame.index.max().to_pydatetime() if len(frame) else None, state=state)
