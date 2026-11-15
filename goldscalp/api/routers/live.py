"""Phase 4 live routes: Vantage MT5 terminal verification, feed health, candle integrity, the live strategy
monitor, signals, SIMULATED validation trades, live-paper performance and the journal.

Read-only towards MT5 (the verification uses the read-only provider). Every simulated item is labelled
SIMULATED; nothing here reaches a broker."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from goldscalp.analysis import sessions as sessions_mod
from goldscalp.api.services import AppServices, active_series_key, clean, pick_series, services
from goldscalp.core.timeframes import ALL_TIMEFRAMES, Timeframe
from goldscalp.data import integrity, mt5_verify
from goldscalp.data.providers.mt5_identity import environment, identify_broker, server_activity
from goldscalp.live import journal, monitor
from goldscalp.storage import candle_repo, live_repo, strategy_repo

router = APIRouter(prefix="/api/live")
_VERIFY_LOCK = asyncio.Lock()


def _provider(gs: AppServices) -> Any:
    return getattr(getattr(gs.market, "service", None), "provider", None)


# ------------------------------------------------------------------------------------------------ status
@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    gs = services(request)
    snap = gs.market.snapshot()
    details = snap.get("provider_details") or {}
    term = details.get("terminal") or {} if isinstance(details, dict) else {}
    ident = env = None
    if gs.market.active == "mt5" and term:
        env = environment(term.get("data_path"), enabled=gs.settings.mt5_detect_environment)
        activity = server_activity(term.get("data_path")) if gs.settings.mt5_detect_environment else None
        ident = identify_broker(term.get("broker"), connected=bool(term.get("connected")), env=env, activity=activity)
        env = {**env, "source": mt5_verify.redact_home(env.get("source"))}
    with gs.db.read() as con:
        ver = live_repo.verifications(con, limit=1)
        monitored = live_repo.monitored_list(con, include_stopped=False)
    now = gs.clock.now()
    active_sessions = sessions_mod.active_names(sessions_mod.definitions(gs.analysis_cfg), now)
    latest = ver[0] if ver else None
    return clean({
        "now": now, "state": snap.get("state"), "reason": snap.get("reason"), "quote": snap.get("quote"),
        "provider": (snap.get("provider") or {}).get("name"), "provider_id": gs.market.active,
        "market_hours": snap.get("market_hours"), "sessions": active_sessions,
        "terminal": {k: term.get(k) for k in ("connected", "build", "broker", "trade_allowed", "name")} if term else None,
        "symbol": details.get("symbol") if isinstance(details, dict) else None,
        "broker_identity": ident, "environment": env,
        "verification": None if latest is None else {k: latest[k] for k in (
            "id", "started_at", "overall", "adapter", "actual_terminal", "broker", "is_vantage", "environment", "symbol")},
        "verified": bool(latest and latest["overall"] == "VERIFIED" and latest["actual_terminal"]),
        "feed": gs.feed.stats(), "monitor": gs.monitor.status() if gs.monitor else None,
        "monitored_active": sum(1 for m in monitored if m["status"] == "active"),
        "label": "Market data is read-only; strategy trades are SIMULATED in the paper simulator."})


# ------------------------------------------------------------------------------------------------ verification
@router.post("/verifications", status_code=201)
async def run_verification(request: Request) -> dict[str, Any]:
    """Run the terminal checklist against the MT5 terminal (read-only) and store the evidence."""
    gs = services(request)
    if _VERIFY_LOCK.locked():
        raise HTTPException(409, "a verification is already running")
    async with _VERIFY_LOCK:
        provider = _provider(gs)
        temporary = False
        if gs.market.active != "mt5" or not hasattr(provider, "verification_facts"):
            option = gs.market.options.get("mt5")
            if option is None or not option.available:
                raise HTTPException(409, "MetaTrader 5 is not available here: "
                                         + (option.reason if option else "not configured"))
            from goldscalp.data.providers.mt5_provider import MT5Provider
            s = gs.settings
            provider = MT5Provider(clock=gs.clock, symbol=s.mt5_symbol, time_basis=s.mt5_time_basis,
                                   terminal_path=s.mt5_terminal_path, require_running=s.mt5_require_running_terminal,
                                   select_symbol=s.mt5_select_symbol, init_timeout_ms=s.mt5_init_timeout_ms)
            temporary = True
        try:
            record = await mt5_verify.run(provider, clock=gs.clock, schedule=gs.spec.schedule,
                                          detect_environment=gs.settings.mt5_detect_environment)
        finally:
            if temporary:
                await provider.close()
        record = clean(record)
        with gs.db.transaction() as con:
            live_repo.insert_verification(con, record)
            stored = live_repo.verification(con, record["id"])
        return stored


@router.get("/verifications")
def list_verifications(request: Request, limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        return {"verifications": live_repo.verifications(con, limit),
                "note": "Only records with adapter 'MetaTrader5' come from the actual terminal."}


@router.get("/verifications/{vid}")
def get_verification(request: Request, vid: str) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        v = live_repo.verification(con, vid)
    if v is None:
        raise HTTPException(404, f"unknown verification {vid}")
    return v


# ------------------------------------------------------------------------------------------------ feed / integrity
@router.get("/feed")
def feed(request: Request, limit: int = Query(240, ge=1, le=5000)) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        samples = live_repo.feed_samples(con, limit=limit)
        events = live_repo.feed_events(con, 50)
    return {"stats": gs.feed.stats(), "samples": samples, "events": events}


@router.get("/integrity")
def candle_integrity(request: Request, series: str | None = Query(None), bars: int = Query(1000, ge=50, le=20000)) -> dict[str, Any]:
    gs = services(request)
    row = pick_series(gs.db, series, active_series_key(gs))
    if row is None:
        raise HTTPException(404, "no candles stored yet")
    now = gs.clock.now()
    frames = {}
    with gs.db.read() as con:
        for tf in ALL_TIMEFRAMES:
            frames[tf.value] = candle_repo.load_candles(con, row["id"], row["price_decimals"], tf, limit=bars)
    reports = [integrity.check_candles(frames[tf.value], tf, now=now, schedule=gs.spec.schedule) for tf in ALL_TIMEFRAMES]
    point = 10 ** -int(row["price_decimals"]) if row["price_decimals"] else 0.01
    cross = [integrity.cross_timeframe(frames["M1"], frames[tf.value], tf, tolerance=max(point, 0.01))
             for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1)]
    return clean({"series": row["key"], "checked_at": now, "timeframes": reports, "consistency": cross})


# ------------------------------------------------------------------------------------------------ monitor
class MonitorIn(BaseModel):
    strategy_id: int
    version: int | None = Field(None, ge=1)
    mode: str = Field("paper", pattern="^(observe|paper)$")
    note: str | None = Field(None, max_length=500)


class MonitorPatch(BaseModel):
    status: str | None = Field(None, pattern="^(active|paused|stopped)$")
    mode: str | None = Field(None, pattern="^(observe|paper)$")


def _record(gs: AppServices, sid: int, version: int | None) -> dict[str, Any]:
    with gs.db.read() as con:
        r = strategy_repo.get(con, sid, version)
    if r is None:
        raise HTTPException(404, f"unknown strategy {sid}" + (f" version {version}" if version else ""))
    return r


@router.get("/eligibility/{sid}")
def strategy_eligibility(request: Request, sid: int, version: int | None = Query(None, ge=1)) -> dict[str, Any]:
    gs = services(request)
    record = _record(gs, sid, version)
    with gs.db.read() as con:
        return {"strategy": {"id": sid, "version": record["version"], "fingerprint": record["fingerprint"],
                             "name": record["name"]}, **monitor.eligibility(con, record)}


def _ensure_account(gs: AppServices, m: dict[str, Any]) -> int:
    if m.get("account_id"):
        return m["account_id"]
    cfg = monitor.validation_account_config(gs, f"Validation: {m['strategy_name']} v{m['version']} (SIMULATED)")
    return gs.engine.create_account(cfg, gs.spec, gs.paper_costs, purpose="validation").id


@router.post("/monitor", status_code=201)
def add_monitored(request: Request, body: MonitorIn) -> dict[str, Any]:
    gs = services(request)
    record = _record(gs, body.strategy_id, body.version)
    with gs.db.read() as con:
        elig = monitor.eligibility(con, record)
        existing = live_repo.find_monitored(con, record["id"], record["version"])
    if not elig["eligible"]:
        raise HTTPException(409, elig["reason"])
    if existing is not None:
        raise HTTPException(409, f"{record['name']} v{record['version']} is already monitored (id {existing['id']})")
    account_id = None
    if body.mode == "paper":
        account_id = _ensure_account(gs, {"strategy_name": record["name"], "version": record["version"]})
    with gs.db.transaction() as con:
        mid = live_repo.insert_monitored(con, strategy_id=record["id"], version=record["version"],
                                         fingerprint=record["fingerprint"], mode=body.mode, account_id=account_id,
                                         eligibility=elig, now=gs.clock.now(), note=body.note)
        return {"monitored": live_repo.monitored(con, mid),
                "note": "Monitoring starts with the next step-candle close. Paper mode simulates every qualifying "
                        "signal in this version's own validation paper account (SIMULATED, no broker)."}


@router.get("/monitor")
def list_monitored(request: Request) -> dict[str, Any]:
    gs = services(request)
    out = []
    with gs.db.read() as con:
        items = live_repo.monitored_list(con)
        for m in items:
            m = {k: v for k, v in m.items() if k != "config"} | {"step_timeframe": m["config"]["evaluation"]["step_timeframe"],
                                                                  "conditions": len(m["config"]["conditions"])}
            m["evaluations"] = live_repo.evaluation_counts(con, m["id"])
            m["open_trades"] = len(live_repo.trades(con, status="open", monitored_id=m["id"]))
            out.append(m)
    for m in out:
        m["account"] = gs.engine.summary(m["account_id"]) if m["account_id"] else None
    return clean({"monitored": out, "monitor": gs.monitor.status() if gs.monitor else None})


@router.patch("/monitor/{mid}")
def patch_monitored(request: Request, mid: int, body: MonitorPatch) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        m = live_repo.monitored(con, mid)
    if m is None:
        raise HTTPException(404, f"unknown monitored strategy {mid}")
    if m["status"] == "stopped":
        raise HTTPException(409, "a stopped monitor cannot be changed; add the version again")
    fields: dict[str, Any] = {}
    if body.mode and body.mode != m["mode"]:
        fields["mode"] = body.mode
        if body.mode == "paper":
            fields["account_id"] = _ensure_account(gs, m)
    if body.status and body.status != m["status"]:
        fields["status"] = body.status
        if body.status == "stopped":
            fields["stopped_at"] = gs.clock.now().isoformat()
    with gs.db.transaction() as con:
        if fields:
            live_repo.update_monitored(con, mid, gs.clock.now(), **fields)
        out = live_repo.monitored(con, mid)
    return {"monitored": out, "note": "Open SIMULATED positions keep being managed (stop, target, time exit)."}


@router.get("/monitor/{mid}/evaluations")
def monitor_evaluations(request: Request, mid: int, limit: int = Query(200, ge=1, le=5000)) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        if live_repo.monitored(con, mid) is None:
            raise HTTPException(404, f"unknown monitored strategy {mid}")
        return {"evaluations": live_repo.evaluations(con, mid, limit), "counts": live_repo.evaluation_counts(con, mid)}


# ------------------------------------------------------------------------------------------------ signals / trades
@router.get("/signals")
def list_signals(request: Request, strategy_id: int | None = None, version: int | None = None,
                 start: str | None = None, end: str | None = None, limit: int = Query(100, ge=1, le=2000)) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        items = live_repo.signals(con, strategy_id=strategy_id, version=version, start=start, end=end, limit=limit)
    return {"signals": [{k: v for k, v in s.items() if k != "snapshot"} for s in items], "label": monitor.LABEL}


@router.get("/signals/{sid}")
def get_signal(request: Request, sid: str) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        s = live_repo.signal(con, sid)
        t = live_repo.trade_by_signal(con, sid)
    if s is None:
        raise HTTPException(404, f"unknown signal {sid}")
    return {"signal": s, "trade_id": None if t is None else t["id"]}


@router.patch("/signals/{sid}/notes")
def signal_notes(request: Request, sid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    gs = services(request)
    notes = body.get("notes")
    if notes is not None and (not isinstance(notes, str) or len(notes) > 4000):
        raise HTTPException(422, "notes must be text of at most 4000 characters")
    with gs.db.transaction() as con:
        if not live_repo.set_signal_notes(con, sid, notes or None):
            raise HTTPException(404, f"unknown signal {sid}")
    return {"signal_id": sid, "notes": notes}


def _with_unrealized(gs: AppServices, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    views: dict[int, dict[int, Any]] = {}
    for t in trades:
        if t["position_status"] == "open":
            if t["account_id"] not in views:
                views[t["account_id"]] = {p["id"]: p for p in gs.engine.positions_view("open", t["account_id"])}
            p = views[t["account_id"]].get(t["position_id"]) or {}
            t["unrealized_pnl"] = p.get("unrealized_pnl")
            t["mark_price"] = p.get("mark_price")
        t["holding_minutes"] = (t["exit"] or {}).get("holding_minutes")
    return trades


@router.get("/trades")
def list_trades(request: Request, status: str | None = Query(None, pattern="^(open|closed)$"),
                strategy_id: int | None = None, version: int | None = None,
                limit: int = Query(200, ge=1, le=5000)) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        items = live_repo.trades(con, status=status, strategy_id=strategy_id, version=version, limit=limit)
    return clean({"trades": _with_unrealized(gs, items), "label": "SIMULATED paper positions (no broker orders)"})


@router.get("/trades/{tid}")
def get_trade(request: Request, tid: int) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        t = live_repo.trade(con, tid)
        if t is None:
            raise HTTPException(404, f"unknown trade {tid}")
        ev = live_repo.trade_evaluation(con, tid)
        sig = live_repo.signal(con, t["signal_id"])
    return clean({"trade": _with_unrealized(gs, [t])[0], "evaluation": ev,
                  "signal": None if sig is None else {k: v for k, v in sig.items() if k != "snapshot"}})


@router.get("/equity")
def equity(request: Request) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        items = live_repo.monitored_list(con)
    lines = []
    for m in items:
        if not m["account_id"]:
            continue
        hist = gs.engine.equity_history(5000, m["account_id"])
        lines.append({"monitored_id": m["id"], "name": f"{m['strategy_name']} v{m['version']}",
                      "account_id": m["account_id"],
                      "points": [{"time": p["ts"], "equity": float(p["equity"])} for p in hist]})
    return {"lines": lines, "label": "SIMULATED live-paper equity (validation accounts)"}


@router.get("/performance")
def performance(request: Request) -> dict[str, Any]:
    gs = services(request)
    start_bal = float(gs.settings.paper_starting_balance)
    out = []
    with gs.db.read() as con:
        for m in live_repo.monitored_list(con):
            trades = live_repo.trades(con, monitored_id=m["id"], limit=10000)
            sigs = live_repo.signals(con, strategy_id=m["strategy_id"], version=m["version"], limit=10000)
            measured = [s for s in sigs if (s.get("outcome") or {}).get("status") == "complete"]
            runs = (m["eligibility"] or {}).get("runs") or []
            base = next((r for r in runs if r.get("sufficient")), runs[0] if runs else None)
            out.append({"monitored_id": m["id"], "strategy_id": m["strategy_id"], "name": m["strategy_name"],
                        "version": m["version"], "mode": m["mode"], "status": m["status"],
                        "live": journal.live_metrics(trades, starting_balance=start_bal, min_trades=30),
                        "signals": {"total": len(sigs), "measured": len(measured),
                                    "false": sum(1 for s in measured if s["outcome"].get("false_signal")),
                                    "target_first": sum(1 for s in measured if s["outcome"].get("first_touch") == "target"),
                                    "stop_first": sum(1 for s in measured if s["outcome"].get("first_touch") == "stop"),
                                    "blocked": sum(1 for s in sigs if s["status"] == "blocked")},
                        "backtest": base})
    return clean({"strategies": out, "label": "SIMULATED live-paper results vs historical (backtest) results",
                  "note": "Ratios are withheld below 30 trades; a short live period says little about a strategy."})


@router.get("/journal")
def journal_entries(request: Request, start: str | None = None, end: str | None = None,
                    strategy_id: int | None = None, version: int | None = None, session: str | None = None,
                    timeframe: str | None = None, direction: str | None = Query(None, pattern="^(long|short)$"),
                    outcome: str | None = Query(None, pattern="^(win|loss|breakeven|open|not_traded|invalidated)$"),
                    limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
    gs = services(request)
    with gs.db.read() as con:
        items = journal.entries(con, start=start, end=end, strategy_id=strategy_id, version=version, session=session,
                                timeframe=timeframe, direction=direction, outcome=outcome, limit=limit)
    return clean({"entries": items, "filters": {"outcomes": list(journal.OUTCOMES)},
                  "label": "SIMULATED strategy journal (signals and paper trades; no broker orders)"})


@router.get("/journal/export.csv")
def journal_export(request: Request, start: str | None = None, end: str | None = None, strategy_id: int | None = None,
                   session: str | None = None, timeframe: str | None = None, outcome: str | None = None) -> PlainTextResponse:
    gs = services(request)
    with gs.db.read() as con:
        items = journal.entries(con, start=start, end=end, strategy_id=strategy_id, session=session,
                                timeframe=timeframe, outcome=outcome, limit=2000)
    return PlainTextResponse(journal.to_csv(clean(items)), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="goldscalp-live-journal.csv"'})
