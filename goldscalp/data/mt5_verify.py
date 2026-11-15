"""MT5 terminal verification checklist (definition mt5v@1). PHASE4_ARCHITECTURE.md section 3.3.

The checks run against whatever MetaTrader5 module the provider holds. The RECORD names that adapter:
only the real package ('MetaTrader5') can produce an overall VERIFIED - a test double is labelled as such
and capped at PARTIAL, so a mocked run can never be mistaken for an actual-terminal verification.

Overall: VERIFIED = every check PASS (incl. Vantage, DEMO, a fresh tick, real adapter); FAILED = any FAIL;
PARTIAL otherwise (e.g. the market is closed so the latest tick is old, or the environment is unknown).
"""
from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from goldscalp.core.clock import Clock, iso, parse_iso_aware
from goldscalp.core.market_hours import TradingSchedule
from goldscalp.core.timeframes import ALL_TIMEFRAMES, Timeframe
from goldscalp.data import integrity
from goldscalp.data.providers.mt5_identity import environment, identify_broker, server_activity

VERSION = "mt5v@1"
REAL_ADAPTER = "MetaTrader5"
TICK_MAX_AGE_SECONDS = 60
MIN_BARS = 100


def redact_home(path: str | None) -> str | None:
    """Show terminal paths without the Windows user-profile folder."""
    if not path:
        return path
    home = os.path.expanduser("~")
    if home and path.lower().startswith(home.lower()):
        return "%USERPROFILE%" + path[len(home):]
    return path


def evaluate(facts: dict[str, Any], frames: dict[str, pd.DataFrame], *, now: datetime, schedule: TradingSchedule,
             env: dict[str, Any], activity: dict[str, Any] | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(key: str, label: str, status: str, detail: str, data: Any = None) -> None:
        checks.append({"key": key, "label": label, "status": status, "detail": detail, "data": data})

    adapter = facts.get("adapter")
    term = facts.get("terminal") or {}
    if facts.get("init_error"):
        err = facts["init_error"]
        add("terminal", "MT5 terminal detected and attached", "FAIL", f"{err['kind']}: {err['message']}",
            {"process": facts.get("process")})
        for key, label in (("broker_connection", "Terminal connected to its broker"),
                           ("broker_is_vantage", "Broker is Vantage"), ("environment", "Demo environment"),
                           ("symbol", "Gold symbol discovered"), ("live_tick", "Live bid/ask tick"),
                           ("reconnect", "Disconnect and reconnect")):
            add(key, label, "SKIPPED", "not checked: the terminal could not be attached")
        return _finish(checks, adapter, facts, env, None)

    add("terminal", "MT5 terminal detected and attached", "PASS",
        f"{term.get('name') or 'MetaTrader 5'} build {term.get('build')}, terminal version {facts.get('version')}; "
        "initialize() attached to the running terminal without any login, password or server",
        {"process": facts.get("process"), "path": redact_home(term.get("path")),
         "trade_allowed_in_terminal": term.get("trade_allowed"), "ping_last_us": term.get("ping_last_us")})
    add("broker_connection", "Terminal connected to its broker", "PASS" if term.get("connected") else "FAIL",
        "terminal_info().connected is true" if term.get("connected") else "the terminal is not connected to a server")
    ident = identify_broker(term.get("broker"), connected=bool(term.get("connected")), env=env, activity=activity)
    add("broker_is_vantage", "Broker is Vantage", ident["status"], f"identified by {ident['basis']}: {ident['evidence']}",
        {"company": ident["company"], "basis": ident["basis"], "server": ident.get("server"),
         "server_folders": [{"server": s["server"], "newest_write": None if s["newest_write_epoch"] is None else
                             iso(datetime.fromtimestamp(s["newest_write_epoch"], tz=UTC))}
                            for s in (activity or {}).get("servers") or []]})
    env_status = {"DEMO": "PASS", "LIVE": "WARN", "UNKNOWN": "WARN"}[env["environment"]]
    add("environment", "Demo environment", env_status, f"{env['environment']}: {env['evidence']}",
        {"server": env.get("server"), "source": redact_home(env.get("source"))})
    sym = facts.get("symbol") or {}
    spec = sym.get("spec") or {}
    add("symbol", "Gold symbol discovered", "PASS" if sym.get("name") else "FAIL",
        f"{sym.get('name')} ({sym.get('source')}: "
        + ("ranked best of the terminal's gold/USD symbols" if sym.get("source") == "auto" else "as configured")
        + f"); digits {spec.get('digits')}, contract size {spec.get('contract_size')}, "
          f"lots {spec.get('volume_min')}-{spec.get('volume_max')} step {spec.get('volume_step')}",
        {"candidates": sym.get("candidates"), "spec": spec})
    point = float(spec.get("point") or 0.01)

    tick = facts.get("tick")
    if tick is None:
        err = facts.get("tick_error") or {}
        add("live_tick", "Live bid/ask tick", "FAIL", f"{err.get('kind')}: {err.get('message')}")
    else:
        age = (now - parse_iso_aware(tick["quote_time"])).total_seconds()
        spread = float(tick["ask"]) - float(tick["bid"])
        detail = (f"bid {tick['bid']} ask {tick['ask']} spread {spread:.{max(0, int(spec.get('digits') or 2))}f}; "
                  f"tick time {tick['quote_time']} ({age:.0f} s old); time basis {tick['time_basis']} "
                  f"({tick['time_basis_source']})")
        if age <= TICK_MAX_AGE_SECONDS:
            status = "PASS"
        else:
            status = "WARN"
            detail += ("; the market is closed now, so the last tick is old" if not schedule.is_open(now)
                       else "; the tick is stale although the market should be open")
        add("live_tick", "Live bid/ask tick", status, detail, {**tick, "age_seconds": round(age, 1), "spread": spread})

    for tf in ALL_TIMEFRAMES:
        frame = frames.get(tf.value)
        if frame is None:
            add(f"candles_{tf.value}", f"{tf.value} candles", "FAIL",
                (facts.get("candle_errors") or {}).get(tf.value, "no candles returned"))
            continue
        report = integrity.check_candles(frame, tf, now=now, schedule=schedule)
        status = report["status"]
        if report["bars"] < MIN_BARS:
            status = "FAIL"
        problems = [f"{e['check']} x{e['count']}" for e in report["errors"]] + \
                   [f"{w['check']} x{w['count']}" for w in report["warnings"]]
        add(f"candles_{tf.value}", f"{tf.value} candles", status,
            f"{report['bars']} bars {report['first']} .. {report['last']}, last closed {report['last_closed']}"
            + (f"; {', '.join(problems)}" if problems else "; integrity clean"),
            {k: report[k] for k in ("bars", "first", "last", "last_closed", "forming_bars", "errors", "warnings")})
    m1 = frames.get("M1")
    for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1):
        if m1 is None or frames.get(tf.value) is None:
            add(f"consistency_{tf.value}", f"{tf.value} matches M1", "SKIPPED", "candles missing")
            continue
        cmp = integrity.cross_timeframe(m1, frames[tf.value], tf, tolerance=point)
        add(f"consistency_{tf.value}", f"{tf.value} matches M1", cmp["status"], cmp["detail"],
            {"compared": cmp["compared"], "mismatches": cmp["mismatches"], "examples": cmp["examples"]})
    rc = facts.get("reconnect") or {}
    add("reconnect", "Disconnect and reconnect", "PASS" if rc.get("ok") else "FAIL",
        (f"shutdown() then initialize() again in {rc.get('seconds')} s; new tick at {rc.get('tick_time')}"
         if rc.get("ok") else f"reconnect failed: {rc.get('error')}"), rc)
    return _finish(checks, adapter, facts, env, ident)


def _finish(checks: list[dict[str, Any]], adapter: str | None, facts: dict[str, Any], env: dict[str, Any],
            ident: dict[str, Any] | None) -> dict[str, Any]:
    if adapter != REAL_ADAPTER:
        checks.append({"key": "adapter", "label": "Real MetaTrader5 package", "status": "WARN",
                       "detail": f"adapter {adapter!r} is not the MetaTrader5 package: this is NOT an actual-terminal "
                                 "verification", "data": None})
    statuses = [c["status"] for c in checks]
    if "FAIL" in statuses:
        overall = "FAILED"
    elif all(s == "PASS" for s in statuses):
        overall = "VERIFIED"
    else:
        overall = "PARTIAL"
    counts = {s: statuses.count(s) for s in ("PASS", "WARN", "FAIL", "SKIPPED")}
    return {"definition": VERSION, "overall": overall, "adapter": adapter, "counts": counts, "checks": checks,
            "broker": ((f"{ident['server']} (trade server)" if ident.get("basis") == "trade server" else ident.get("company"))
                       if ident else (facts.get("terminal") or {}).get("broker")),
            "is_vantage": None if ident is None else ident["is_vantage"], "environment": env.get("environment"),
            "server": env.get("server"), "symbol": (facts.get("symbol") or {}).get("name")}


async def run(provider: Any, *, clock: Clock, schedule: TradingSchedule, detect_environment: bool = True) -> dict[str, Any]:
    """Run the checklist on the provider's MT5 thread and return the record to store."""
    started = clock.now()
    facts, frames = await provider.run_sync(provider.verification_facts)
    term = facts.get("terminal") or {}
    env = (environment(term.get("data_path"), enabled=detect_environment) if not facts.get("init_error")
           else {"environment": "UNKNOWN", "server": None, "source": None, "evidence": "terminal not attached"})
    activity = server_activity(term.get("data_path")) if detect_environment and not facts.get("init_error") else None
    result = evaluate(facts, frames, now=clock.now(), schedule=schedule, env=env, activity=activity)
    return {"id": "mv_" + secrets.token_hex(8), "started_at": iso(started), "finished_at": iso(clock.now()), **result}
