"""Historical data import (Mode B)."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from goldscalp.api.services import clean, services
from goldscalp.core.kinds import DataKind, envelope
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.ingestion import ImportRefused, import_csv
from goldscalp.data.providers.csv_file import CsvFormatError
from goldscalp.storage import candle_repo
from goldscalp.storage.candle_repo import SeriesConflict

router = APIRouter(prefix="/api/data")


@router.post("/import")
async def import_file(request: Request, file: UploadFile = File(...), dialect: str = Form(...),
                      timeframe: str = Form(...), time_basis: str = Form(...), series_label: str = Form(...),
                      source_symbol: str = Form("XAUUSD"), candle_side: str = Form(...),
                      price_decimals: int = Form(...), point: str | None = Form(None)) -> dict[str, Any]:
    gs = services(request)
    limit = gs.settings.max_import_mb * 1024 * 1024
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(413, f"file is larger than {gs.settings.max_import_mb} MB")
    if candle_side not in ("bid", "ask", "mid", "last", "unknown"):
        raise HTTPException(422, "candle_side must be bid, ask, mid, last or unknown")
    try:
        tf = Timeframe.parse(timeframe)
        point_d = Decimal(point) if point not in (None, "") else None
        result = import_csv(gs.db, gs.clock, gs.spec.schedule, content=content, file_name=file.filename or "upload.csv",
                            dialect=dialect, tf=tf, time_basis=time_basis, series_label=series_label,
                            symbol=gs.spec.symbol, source_symbol=source_symbol, candle_side=candle_side,
                            price_decimals=price_decimals, point=point_d)
    except (ImportRefused, CsvFormatError, SeriesConflict, ValueError, InvalidOperation) as exc:
        raise HTTPException(422, str(exc)) from exc
    return envelope(DataKind.HISTORICAL, clean(result.to_dict()), generated_at=gs.clock.now(),
                    source=f"csv:{dialect}")


@router.get("/imports")
def imports(request: Request) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        rows = candle_repo.list_imports(con)
    return envelope(DataKind.HISTORICAL, clean(rows), generated_at=gs.clock.now())
