"""FastAPI application factory.

Run:  python -m goldscalp            (reads GOLDSCALP_* settings / .env)
"""
from __future__ import annotations

import hmac
import importlib.util
import logging
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from goldscalp.api.routers import analysis as analysis_routes
from goldscalp.api.routers import data as data_routes
from goldscalp.api.routers import demo as demo_routes
from goldscalp.api.routers import manual as manual_routes
from goldscalp.api.routers import improvement as improvement_routes
from goldscalp.api.routers import live as live_routes
from goldscalp.api.routers import market as market_routes
from goldscalp.api.routers import performance as performance_routes
from goldscalp.api.routers import replay as replay_routes
from goldscalp.api.routers import research as research_routes
from goldscalp.api.routers import strategies as strategy_routes
from goldscalp.api.routers import simulation as simulation_routes
from goldscalp.analysis.config import AnalysisConfig
from goldscalp.analysis.replay import ReplayStore
from goldscalp.api.services import AppServices
from goldscalp.core.clock import Clock, SystemClock
from goldscalp.core.config import PROJECT_ROOT, Settings, load_settings
from goldscalp.core.forward import ForwardDataLocked
from goldscalp.core.timeframes import Timeframe
from goldscalp.data.hub import NAMES, MarketHub, ProviderOption
from goldscalp.data.live import MarketDataService, RequestBudget
from goldscalp.execution.manual import ManualTicket
from goldscalp.execution.mirror import DemoMirror
from goldscalp.live.feed import FeedMonitor
from goldscalp.live.monitor import LiveMonitor
from goldscalp.simulation.account import AccountConfig
from goldscalp.simulation.costs import CostModel
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.simulation.portfolio import PaperTradingEngine
from goldscalp.storage import improvement_repo, performance_repo, research_repo
from goldscalp.storage.db import Database

WEB_DIR = PROJECT_ROOT / "web"
# style-src hash: the exact <style> Lightweight Charts v5.2.1 injects for its attribution logo.
LWC_STYLE_HASH = "'sha256-3pRED1tOXas1FXFoPb9TGCjmYe9XQsmO9OV23khV2nY='"
CSP = (f"default-src 'self'; script-src 'self'; style-src 'self' {LWC_STYLE_HASH}; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
       "object-src 'none'")
# The TradingView page is a separate document. Its CSP allows ONLY TradingView hosts (connect-src has no
# 'self'), so widget code cannot call the GoldScalp API. GOLDSCALP_TRADINGVIEW_SANDBOX=true additionally
# frames it in an opaque-origin sandbox. GoldScalp never reads the widget's data.
TV_HOSTS = "https://*.tradingview.com https://*.tradingview-widget.com"
TV_CSP = (f"default-src 'none'; script-src 'self' {TV_HOSTS}; style-src 'self' 'unsafe-inline' {TV_HOSTS}; "
          f"img-src data: blob: {TV_HOSTS}; font-src data: {TV_HOSTS}; "
          f"connect-src {TV_HOSTS} wss://*.tradingview.com wss://*.tradingview-widget.com; "
          f"frame-src {TV_HOSTS}; worker-src blob: {TV_HOSTS}; frame-ancestors 'self'; base-uri 'none'; "
          "form-action 'none'; object-src 'none'")
log = logging.getLogger("goldscalp")
#: State-changing API calls must carry this header. Browsers cannot add it cross-site without a CORS
#: preflight (which GoldScalp never grants), so other websites cannot drive this local app (CSRF).
CSRF_HEADER = "X-GoldScalp-Request"


def _package_installed(name: str) -> bool:
    """True if ``name`` can be imported. Never imports it (the MT5 provider does that lazily)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def provider_options(settings: Settings) -> list[ProviderOption]:
    """Every provider with whether it can run here and, if not, why."""
    if not settings.mt5_enabled:
        mt5_reason = "disabled (GOLDSCALP_MT5_ENABLED=false)"
    elif sys.platform != "win32":
        mt5_reason = "MetaTrader 5's Python package runs on Windows only"
    elif not _package_installed("MetaTrader5"):
        mt5_reason = "the MetaTrader5 package is not installed (pip install -r requirements-mt5.txt)"
    else:
        mt5_reason = None
    tiingo_reason = None if settings.tiingo_api_key is not None else "no GOLDSCALP_TIINGO_API_KEY configured"
    return [ProviderOption("mt5", NAMES["mt5"], mt5_reason is None, mt5_reason),
            ProviderOption("tiingo", NAMES["tiingo"], tiingo_reason is None, tiingo_reason),
            ProviderOption("gold-api", NAMES["gold-api"], True, None),
            ProviderOption("none", NAMES["none"], True, None)]


def service_factory(settings: Settings, db: Database, clock: Clock,
                    spec: InstrumentSpec) -> Callable[[str, dict[str, Any]], MarketDataService]:
    def build(provider_id: str, options: dict[str, Any]) -> MarketDataService:
        provider: Any = None
        budget = None
        live_window = settings.live_window_seconds
        if provider_id == "mt5":
            from goldscalp.data.providers.mt5_provider import MT5Provider
            provider = MT5Provider(clock=clock, symbol=options.get("symbol") or settings.mt5_symbol,
                                   time_basis=settings.mt5_time_basis,
                                   poll_seconds=settings.mt5_poll_interval_seconds,
                                   terminal_path=settings.mt5_terminal_path,
                                   require_running=settings.mt5_require_running_terminal,
                                   select_symbol=settings.mt5_select_symbol,
                                   init_timeout_ms=settings.mt5_init_timeout_ms,
                                   backfill_bars={Timeframe.M1: settings.mt5_backfill_bars_m1,
                                                  Timeframe.M5: settings.mt5_backfill_bars_m5,
                                                  Timeframe.M15: settings.mt5_backfill_bars_m15,
                                                  Timeframe.H1: settings.mt5_backfill_bars_h1})
            if options.get("symbol"):
                provider.symbol_source = "user"
            live_window = settings.mt5_stale_after_seconds
        elif provider_id == "tiingo":
            from goldscalp.data.providers.tiingo import TiingoProvider
            if settings.tiingo_api_key is None:
                raise ValueError("Tiingo needs GOLDSCALP_TIINGO_API_KEY")
            provider = TiingoProvider(settings.tiingo_api_key.get_secret_value(), settings.tiingo_ticker,
                                      settings.tiingo_threshold_level)
            budget = RequestBudget(settings.tiingo_requests_per_hour, settings.tiingo_requests_per_day)
        elif provider_id == "gold-api":
            from goldscalp.data.providers.gold_api import GoldApiProvider
            provider = GoldApiProvider()
        return MarketDataService(provider=provider, db=db, clock=clock, schedule=spec.schedule, symbol=spec.symbol,
                                 price_decimals=5, live_window=live_window,
                                 disconnect_after=settings.disconnect_after_seconds, budget=budget,
                                 monthly_stream_mb=settings.tiingo_monthly_stream_mb if budget else None)
    return build


def create_app(settings: Settings | None = None, *, clock: Clock | None = None, provider: Any = "from-settings",
               start_market_data: bool = True, hub: MarketHub | None = None) -> FastAPI:
    """``provider`` (tests): a fixed provider object or None instead of the configured hub."""
    settings = settings or load_settings()
    clock = clock or SystemClock()
    db = Database(settings.db_path)
    db.migrate()
    spec = InstrumentSpec.load(settings.instrument_spec)
    if hub is None:
        if provider == "from-settings":
            hub = MarketHub(clock=clock, options=provider_options(settings),
                            build=service_factory(settings, db, clock, spec), selected=settings.live_provider,
                            fallback_mode=settings.provider_fallback,
                            fallback_after=settings.provider_fallback_after_seconds)
        else:
            fixed_id = provider.info.id if provider is not None else "none"

            def fixed(pid: str, _options: dict[str, Any]) -> MarketDataService:
                return MarketDataService(provider=provider if pid == fixed_id else None, db=db, clock=clock,
                                         schedule=spec.schedule, symbol=spec.symbol, price_decimals=5,
                                         live_window=settings.live_window_seconds,
                                         disconnect_after=settings.disconnect_after_seconds)
            options = [ProviderOption(fixed_id, NAMES.get(fixed_id, fixed_id), True, None)]
            if fixed_id != "none":
                options.append(ProviderOption("none", NAMES["none"], True, None))
            hub = MarketHub(clock=clock, options=options, build=fixed, selected=fixed_id)
    market = hub
    costs = CostModel.from_mapping({"spread_mode": settings.paper_spread_mode,
                                    "configured_spread": settings.paper_configured_spread,
                                    "commission_per_lot_per_side": settings.paper_commission_per_lot_per_side,
                                    "slippage": settings.paper_slippage})
    account = AccountConfig.from_mapping({"name": settings.paper_account_name, "currency": "USD",
                                          "starting_balance": settings.paper_starting_balance,
                                          "leverage": settings.paper_leverage,
                                          "max_quote_age_seconds": settings.paper_max_quote_age_seconds,
                                          "allow_delayed_fills": settings.paper_allow_delayed_fills})
    engine = PaperTradingEngine(db, clock, market.market_quote, spec, costs, account)
    market.listeners.append(engine.on_quote)          # first: SL/TP of every simulated account
    feed = FeedMonitor(db, clock, market)
    market.listeners.append(feed.on_quote)            # Phase 4: spread / freshness statistics

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine.ensure_account()
        if start_market_data:
            await market.start()
            if settings.live_monitor_enabled:
                await app.state.gs.monitor.start()
        yield
        app.state.gs.perf_jobs.cancel_all()
        if start_market_data:
            await app.state.gs.monitor.stop()
            await market.stop()

    app = FastAPI(title="GoldScalp AI", version="0.1.0", lifespan=lifespan, docs_url="/api/docs",
                  redoc_url=None, openapi_url="/api/openapi.json")
    analysis_cfg = AnalysisConfig.load()
    with db.transaction() as con:
        performance_repo.save_config(con, analysis_cfg.fingerprint, analysis_cfg.data, clock.now())
        performance_repo.mark_interrupted(con, clock.now())
        research_repo.mark_interrupted(con, clock.now())
        improvement_repo.mark_interrupted(con, clock.now())
    app.state.gs = AppServices(settings=settings, db=db, clock=clock, spec=spec, market=market, engine=engine,
                               analysis_cfg=analysis_cfg, replays=ReplayStore(), paper_costs=costs,
                               perf_jobs=performance_routes.PerformanceJobs(), feed=feed)
    app.state.gs.monitor = LiveMonitor(app.state.gs, feed)
    # Optional MT5 DEMO-account execution: follows the monitor's simulated trades of LINKED versions only; inert unless
    # GOLDSCALP_DEMO_EXECUTION_ENABLED=true and the terminal's account is a DEMO account (checked before every order).
    app.state.gs.demo = DemoMirror(app.state.gs, lambda: getattr(getattr(market, "service", None), "provider", None)
                                   if market.active == "mt5" else None)
    app.state.gs.monitor.demo = app.state.gs.demo
    # Dashboard order ticket: manual orders on the same DEMO account, through the same demo-only gateway, with their own
    # magic number so the mirror above never touches them (goldscalp/execution/manual.py).
    app.state.gs.manual = ManualTicket(app.state.gs.demo)
    market.listeners.append(app.state.gs.monitor.on_quote)   # Phase 4: excursions of open simulated trades

    token = settings.auth_token.get_secret_value() if settings.auth_token else None

    @app.middleware("http")
    async def security(request: Request, call_next):
        if token and request.url.path.startswith("/api/") and request.url.path != "/api/health":
            supplied = request.headers.get("authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {token}"):
                return JSONResponse({"detail": "authentication required"}, status_code=401)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/") \
                and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": f"missing {CSRF_HEADER} header"}, status_code=403)
        response = await call_next(request)
        path = request.url.path
        if path in ("/tv-widget", "/static/js/tv-widget-page.js"):
            response.headers.setdefault("Content-Security-Policy", TV_CSP)
            response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        elif not path.startswith("/api/docs"):  # the interactive API docs page needs its own assets
            response.headers.setdefault("Content-Security-Policy", CSP)
            response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif not path.startswith("/static/vendor/"):
            # app code changes between versions: always revalidate (ETag) so an upgrade never runs stale scripts
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    for module in (market_routes, analysis_routes, replay_routes, performance_routes, strategy_routes, research_routes,
                   simulation_routes, data_routes, live_routes, improvement_routes, demo_routes, manual_routes):
        app.include_router(module.router)
    @app.exception_handler(ForwardDataLocked)
    async def forward_locked(request: Request, exc: ForwardDataLocked) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "forward_from": exc.forward_from, "guard": "forward-guard@1"},
                            status_code=409)
    app.include_router(analysis_routes.signals_router)
    app.include_router(strategy_routes.journal_router)
    app.include_router(improvement_routes.proposals_router)
    app.include_router(improvement_routes.assistant_router)

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

        @app.get("/paper", include_in_schema=False)
        def paper() -> FileResponse:
            return FileResponse(WEB_DIR / "paper.html")

        @app.get("/lab", include_in_schema=False)
        def lab_page() -> FileResponse:
            return FileResponse(WEB_DIR / "lab.html")

        @app.get("/live", include_in_schema=False)
        def live_page() -> FileResponse:
            return FileResponse(WEB_DIR / "live.html")

        @app.get("/performance", include_in_schema=False)
        def performance_page() -> FileResponse:
            return FileResponse(WEB_DIR / "performance.html")

        @app.get("/replay", include_in_schema=False)
        def replay_page() -> FileResponse:
            return FileResponse(WEB_DIR / "replay.html")

        @app.get("/tv-widget", include_in_schema=False)
        def tv_widget() -> FileResponse:
            if not settings.tradingview_widget:
                raise HTTPException(404, "the TradingView widget is disabled (GOLDSCALP_TRADINGVIEW_WIDGET=false)")
            return FileResponse(WEB_DIR / "tv-widget.html")

    @app.get("/api/ui-config", include_in_schema=False)
    def ui_config() -> dict[str, Any]:
        """Display settings for the browser. Contains no secrets."""
        return {"tradingview_widget": settings.tradingview_widget,
                "tradingview_symbol": settings.tradingview_symbol,
                "tradingview_sandbox": settings.tradingview_sandbox,
                "instrument_symbol": spec.symbol, "display_timezone": settings.display_timezone}

    return app

