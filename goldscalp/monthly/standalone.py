"""The research service: a SEPARATE process for the Phase 5 monthly backtests (PHASE5_ARCHITECTURE.md section 1).

Why separate: the live Phase 4 monitors run inside the main GoldScalp process, which must not be restarted, reloaded
or loaded with CPU-heavy backtests. This service
  - opens the application database READ-ONLY (strategies, candles, verification record) and never migrates it;
  - writes only to its own store (var/monthly/monthly.sqlite3);
  - never imports the market hub, providers, paper engine or live monitor, and never connects to MT5;
  - lowers its own process priority so backtests yield the CPU to the monitors;
  - applies the same security rules as the main app: 127.0.0.1 binding, CSRF header on state changes, strict CSP.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from goldscalp.analysis.config import AnalysisConfig
from goldscalp.core.clock import Clock, SystemClock
from goldscalp.core.config import PROJECT_ROOT, Settings, load_settings
from goldscalp.core.forward import ForwardDataLocked
from goldscalp.monthly import library, service
from goldscalp.monthly import store as st
from goldscalp.monthly.data import DataSources
from goldscalp.simulation.instrument import InstrumentSpec

log = logging.getLogger("goldscalp.research")
WEB_DIR = PROJECT_ROOT / "web"
CSRF_HEADER = "X-GoldScalp-Request"
LWC_STYLE_HASH = "'sha256-3pRED1tOXas1FXFoPb9TGCjmYe9XQsmO9OV23khV2nY='"
CSP = (f"default-src 'self'; script-src 'self'; style-src 'self' {LWC_STYLE_HASH}; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
       "object-src 'none'")
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def lower_priority() -> bool:
    """Backtests yield the CPU to the main GoldScalp process (Windows: BELOW_NORMAL_PRIORITY_CLASS)."""
    if sys.platform != "win32":
        return False
    try:
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = wintypes.HANDLE            # 64-bit pseudo-handle: never let ctypes truncate it
        k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.SetPriorityClass.restype = wintypes.BOOL
        return bool(k.SetPriorityClass(k.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS))
    except Exception:  # noqa: BLE001
        return False


def create_research_app(settings: Settings | None = None, *, clock: Clock | None = None,
                        app_db_path: Path | None = None, store_path: Path | None = None) -> FastAPI:
    settings = settings or load_settings()
    clock = clock or SystemClock()
    source = st.ReadOnlySource(app_db_path or settings.db_path)
    research_store = st.open_store(store_path or (Path(settings.db_path).parent / "monthly" / "monthly.sqlite3"))
    ms = service.MonthlyServices(
        sources=DataSources(source, research_store), store=research_store, clock=clock,
        app_spec=InstrumentSpec.load(settings.instrument_spec), analysis_cfg=AnalysisConfig.load(),
        defaults={"research_days": 60, "lookback_days": 5, "configured_spread": settings.paper_configured_spread,
                  "commission": settings.paper_commission_per_lot_per_side, "slippage": settings.paper_slippage,
                  "risk_percent_override": None})
    with research_store.transaction() as con:
        st.mark_interrupted(con, clock.now())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        ms.jobs.cancel_all()

    app = FastAPI(title="GoldScalp AI research service (monthly backtests)", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.monthly = ms

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/") \
                and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": f"missing {CSRF_HEADER} header"}, status_code=403)
        response = await call_next(request)
        path = request.url.path
        if not path.startswith("/api/docs"):
            response.headers.setdefault("Content-Security-Policy", CSP)
            response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif not path.startswith("/static/vendor/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.exception_handler(ForwardDataLocked)
    async def forward_locked(request: Request, exc: ForwardDataLocked) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "forward_from": exc.forward_from, "guard": "forward-guard@1"},
                            status_code=409)
    app.include_router(service.router)
    app.include_router(library.router)                    # read-only research library (GET only)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"service": "research", "status": "ok", "application_database": str(source.path), "access": "read-only",
                "store": str(research_store.path), "job": ms.jobs.busy()}

    @app.get("/api/ui-config", include_in_schema=False)
    def ui_config() -> dict[str, Any]:
        return {"display_timezone": settings.display_timezone, "main_app": f"http://{settings.host}:{settings.port}/"}

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def root() -> RedirectResponse:
            return RedirectResponse("/monthly-backtest")

        @app.get("/monthly-backtest", include_in_schema=False)
        def page() -> FileResponse:
            return FileResponse(WEB_DIR / "monthly.html")
    return app


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    lowered = lower_priority()
    app = create_research_app(settings)
    print(f"GoldScalp research service (monthly backtests, simulation only): http://{settings.host}:{settings.monthly_port}/"
          f"monthly-backtest  [application database read-only; priority {'below normal' if lowered else 'unchanged'}]")
    uvicorn.run(app, host=settings.host, port=settings.monthly_port, log_level="info")
