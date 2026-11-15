"""Phase 6: new strategy discovery (definition discovery@1). Historical simulation only; research store only.

The hypotheses, baseline rules, predictions and validation criteria live in ``config/discovery/phase6.json`` and are
frozen by hash (``discovery_report --freeze``) BEFORE any Phase 6 trial runs. This module enforces the process:

* development uses January to 13 November 2026 only (already examined in Phases 5.2/5.3);
* the SEALED block (November 2023 to October 2025, plus one month of warm-up) and FORWARD data (from 14 November 2026)
  are never read by a development trial: every window, including its look-back and higher-timeframe history, is
  checked;
* each hypothesis gets at most four trials: its baseline and up to three refinements;
* a refinement is built by this module from a parent configuration, using one of the hypothesis's allowed kinds
  (a filter the hypothesis itself names, the stop timeframe, or the exit horizon x0.5 / x2), and needs a written
  rationale. There are no free-form edits and no parameter grids;
* every trial and every result is stored and counted."""
from __future__ import annotations

import copy
import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from goldscalp.backtesting import features
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe
from goldscalp.monthly import robustness as rb
from goldscalp.monthly import runner
from goldscalp.monthly import verdicts as vd
from goldscalp.strategies import builder

DEFINITION = "discovery@1"
ROOT = Path(__file__).resolve().parents[2]
SPEC_FILE = ROOT / "config" / "discovery" / "phase6.json"
SEALED = (datetime(2023, 10, 1, tzinfo=UTC), datetime(2025, 11, 1, tzinfo=UTC))   # evaluation Nov 2023-Oct 2025 + warm-up
SEALED_EVAL = (datetime(2023, 11, 1, tzinfo=UTC), datetime(2025, 11, 1, tzinfo=UTC))
FORWARD_FROM = datetime(2026, 11, 14, tzinfo=UTC)
HISTORY_MARGIN = timedelta(days=31)          # coarser-timeframe history read before a window (400 H1 bars ~ 3 weeks)
MAX_TRIALS = 4
RESEARCH_SERIES = ("research", "csv:vantage-mt5-xauusd-2026-jan-may")
APP_SERIES = ("app", "live:mt5:XAUUSD")
MIN_RATIONALE = 40
SESSION_BUCKETS = {"Asian / London": "London open", "London": "London open", "Asian": "Asian",
                   "London / New York": "New York", "New York": "New York"}


class DiscoveryError(ValueError):
    pass


def _d(y: int, m: int, d: int = 1) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def dev_windows() -> list[dict[str, Any]]:
    """The development months: January-May from the research-store export, 22 June-13 November from the app's MT5
    history (M5/M15 there start 15 June; 22 June leaves the five-day warm-up)."""
    spans = [(_d(2026, m), _d(2026, m + 1), *RESEARCH_SERIES) for m in range(1, 6)]
    spans += [(_d(2026, 6, 22), _d(2026, 7), *APP_SERIES)]
    spans += [(_d(2026, m), _d(2026, m + 1), *APP_SERIES) for m in range(7, 11)]
    spans += [(_d(2026, 11), FORWARD_FROM, *APP_SERIES)]
    return [{"key": f"D{i + 1}", "start": iso(a), "end": iso(b), "month": a.strftime("%Y-%m"), "store": st, "series": se,
             "partial": a.day != 1 or b.day != 1} for i, (a, b, st, se) in enumerate(spans)]


def guard(start: datetime, end: datetime, lookback_days: int, purpose: str = "development") -> None:
    """Refuse any development read that could touch sealed or forward data (look-back and coarser history
    included)."""
    if purpose != "development":
        raise DiscoveryError(f"unknown purpose {purpose!r}: sealed validation has its own, separately frozen path")
    earliest = start - timedelta(days=lookback_days) - HISTORY_MARGIN
    if earliest < SEALED[1] and end > SEALED[0]:
        raise DiscoveryError(f"{iso(start)}-{iso(end)} (reading from {iso(earliest)}) overlaps the sealed block "
                             f"{iso(SEALED[0])}-{iso(SEALED[1])}")
    if end > FORWARD_FROM:
        raise DiscoveryError(f"{iso(end)} is after {iso(FORWARD_FROM)}: forward data is reserved for confirmation")


def load_spec(path: Path | None = None) -> dict[str, Any]:
    spec = json.loads((path or SPEC_FILE).read_text(encoding="utf-8"))
    if spec.get("definition") != DEFINITION:
        raise DiscoveryError("unexpected specification definition")
    spec["configs"] = {k: builder.validate(c) for k, c in spec["configs"].items()}
    keys = set(spec["configs"])
    for h in spec["hypotheses"]:
        if not set(h["baselines"]) <= keys:
            raise DiscoveryError(f"{h['id']} names an unknown baseline")
    return spec


def hypothesis(spec: dict[str, Any], hid: str) -> dict[str, Any]:
    for h in spec["hypotheses"]:
        if h["id"] == hid:
            return h
    raise DiscoveryError(f"unknown hypothesis {hid}")


# ------------------------------------------------------------------ refinements (built here, never free-form)
REFINEMENT_KINDS = ("filter", "stop_timeframe", "exit_horizon")


def refine(parent: dict[str, Any], kind: str, value: Any, hyp: dict[str, Any]) -> dict[str, Any]:
    allowed = " ".join(hyp["allowed_refinements"])
    if kind not in REFINEMENT_KINDS or kind not in allowed:
        raise DiscoveryError(f"{hyp['id']} allows only: {hyp['allowed_refinements']}")
    cfg = copy.deepcopy(parent)
    if kind == "filter":
        if not isinstance(value, dict) or value.get("type") not in ("volatility_regime", "session"):
            raise DiscoveryError("a filter refinement adds one volatility_regime or session condition")
        if value["type"] not in allowed:
            raise DiscoveryError(f"{hyp['id']} does not name a {value['type']} filter")
        cfg["conditions"].append({"id": f"p6_{value['type']}", "enabled": True, **value})
    elif kind == "stop_timeframe":
        if value not in ("M5", "M15", "H1") or value == cfg["exit"]["stop"]["timeframe"]:
            raise DiscoveryError("stop_timeframe must change the stop to another of M5, M15, H1")
        cfg["exit"]["stop"]["timeframe"] = value
    else:
        if value not in (0.5, 2, 2.0):
            raise DiscoveryError("exit_horizon multiplies the maximum holding time by 0.5 or 2")
        cfg["exit"]["max_holding_minutes"] = int(cfg["exit"]["max_holding_minutes"] * float(value))
    cfg["name"] = f"{parent['name'].split(' (')[0]} ({kind} {value if kind != 'filter' else value['type']})"
    return builder.validate(cfg)


# ------------------------------------------------------------------ trials
def _trials(con: Any, hid: str) -> list[Any]:
    return con.execute("SELECT * FROM discovery_trials WHERE hypothesis = ? ORDER BY created_at", (hid,)).fetchall()


def create_trial(ms: Any, hid: str, kind: str = "baseline", *, rationale: str = "", parent: str | None = None,
                 refinement: dict[str, Any] | None = None, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = spec or load_spec()
    hyp = hypothesis(spec, hid)
    with ms.store.read() as con:
        done = _trials(con, hid)
        configs_so_far = {k: v for t in done for k, v in json.loads(t["configs"]).items()}
    if len(done) >= MAX_TRIALS:
        raise DiscoveryError(f"{hid} has used all {MAX_TRIALS} trials (baseline + 3 refinements); freeze or drop it")
    if kind == "baseline":
        if any(t["kind"] == "baseline" for t in done):
            raise DiscoveryError(f"{hid} already has its baseline trial")
        configs = {k: spec["configs"][k] for k in hyp["baselines"]}
    elif kind == "refinement":
        if not any(t["kind"] == "baseline" and t["status"] == "COMPLETED" for t in done):
            raise DiscoveryError(f"{hid}: run and complete the baseline first")
        if len(rationale.strip()) < MIN_RATIONALE:
            raise DiscoveryError(f"a refinement needs a written rationale citing the development evidence "
                                 f"(at least {MIN_RATIONALE} characters)")
        if parent not in configs_so_far:
            raise DiscoveryError(f"parent {parent!r} is not a configuration of an earlier {hid} trial")
        ref = refinement or {}
        cfg = refine(configs_so_far[parent], ref.get("kind", ""), ref.get("value"), hyp)
        configs = {f"{parent}+{ref['kind']}:{ref['value'] if ref['kind'] != 'filter' else ref['value']['type']}": cfg}
    else:
        raise DiscoveryError("kind is 'baseline' or 'refinement'")
    windows = dev_windows()
    for w in windows:
        guard(runner.parse_iso(w["start"]), runner.parse_iso(w["end"]), rb.LOOKBACK_DAYS)
    tid = "dt_" + secrets.token_hex(6)
    now = ms.clock.now()
    params = {"definition": DEFINITION, "hypothesis": hid, "kind": kind, "parent": parent, "refinement": refinement,
              "rationale": rationale.strip(), "windows": windows, "timeframes": ["M5"], "costs": rb.COSTS,
              "trial_number": len(done) + 1, "max_trials": MAX_TRIALS,
              "fingerprints": {k: builder.fingerprint(c) for k, c in configs.items()}}
    with ms.store.transaction() as con:
        con.execute("INSERT INTO discovery_trials (id, hypothesis, kind, status, params, configs, app_version, created_at, "
                    "updated_at, progress, total) VALUES (?, ?, ?, 'READY', ?, ?, ?, ?, ?, 0, ?)",
                    (tid, hid, kind, json.dumps(params), json.dumps(configs, sort_keys=True), runner.app_version(), iso(now),
                     iso(now), len(configs) * len(windows)))
    return {"id": tid, "hypothesis": hid, "kind": kind, "configs": list(configs), "trial_number": len(done) + 1}


def _update(ms: Any, tid: str, **fields: Any) -> None:
    sets, args = ["updated_at = ?"], [iso(ms.clock.now())]
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        args.append(json.dumps(v, default=str) if k == "analysis" else v)
    with ms.store.transaction() as con:
        con.execute(f"UPDATE discovery_trials SET {', '.join(sets)} WHERE id = ?", (*args, tid))


def run_trial(ms: Any, tid: str, cancel: threading.Event) -> None:
    run_trials(ms, [tid], cancel)


def run_trials(ms: Any, tids: list[str], cancel: threading.Event) -> None:
    """Run one or more trials (all over the same development windows). Each window's feature timeline is computed once
    and shared; every trial keeps its own results, analysis and count."""
    trials = {}
    with ms.store.read() as con:
        for tid in tids:
            row = con.execute("SELECT params, configs FROM discovery_trials WHERE id = ?", (tid,)).fetchone()
            trials[tid] = (json.loads(row["params"]), json.loads(row["configs"]))
    windows = trials[tids[0]][0]["windows"]
    for tid, (params, configs) in list(trials.items()):
        if params["windows"] != windows:
            _update(ms, tid, status="FAILED", error="trials in one job must share the development windows")
            trials.pop(tid)
            continue
        for k, c in configs.items():
            if builder.fingerprint(c) != params["fingerprints"][k]:
                _update(ms, tid, status="FAILED", error=f"{k}: rules changed after the trial was created")
                trials.pop(tid)
                break
    if not trials:
        return
    spec, _contract = runner.contract_spec(ms.app_spec, ms.sources.verified_spec())
    assumptions = runner.make_assumptions(rb.COSTS)
    detail = set().union(*(builder.detail_timeframes(c) for _p, cs in trials.values() for c in cs.values()))
    for tid in trials:
        _update(ms, tid, status="RUNNING", error=None)
    done = {tid: 0 for tid in trials}

    def mark(phase: str) -> None:
        for tid in trials:
            _update(ms, tid, progress=done[tid], phase=phase)
    try:
        for w in windows:
            start, end = runner.parse_iso(w["start"]), runner.parse_iso(w["end"])
            guard(start, end, rb.LOOKBACK_DAYS)                     # re-checked at run time
            srow = ms.sources.series_row(w["store"], w["series"])
            cov = ({"sufficient": False, "problems": [f"series {w['series']} not found in the {w['store']} store"]}
                   if srow is None else rb._coverage(ms.sources, w["store"], srow, Timeframe.M5, start, end,
                                                      ms.app_spec.schedule))
            if not cov["sufficient"]:
                with ms.store.transaction() as con:
                    con.executemany("INSERT OR REPLACE INTO discovery_results (trial_id, config_key, timeframe, window, "
                                    "status, reason) VALUES (?, ?, 'M5', ?, 'INCOMPLETE', ?)",
                                    [(tid, k, w["key"], "; ".join(cov["problems"])) for tid, (_p, cs) in trials.items()
                                     for k in cs])
                for tid, (_p, cs) in trials.items():
                    done[tid] += len(cs)
                mark(f"{w['month']} incomplete")
                continue

            def tick(n: int, w: dict[str, Any] = w) -> None:
                if n % 500 == 0:
                    mark(f"{w['month']} timeline: {n} steps")
            prepared = runner.prepare(ms.sources, w["store"], srow, Timeframe.M5, start, end, lookback_days=rb.LOOKBACK_DAYS,
                                      detail=detail, cfg=ms.analysis_cfg, progress=tick, cancelled=cancel.is_set)
            snaps = {s["cutoff"]: s for s in prepared["snapshots"]}
            rows = []
            for tid, (_p, cs) in trials.items():
                for k, cfg in cs.items():
                    if cancel.is_set():
                        raise features.Cancelled("cancelled")
                    res = runner.run_one(cfg, prepared, Timeframe.M5, start, end, assumptions, spec)
                    trades = rb.compact(res["trades"], snaps, cfg["exit"]["stop"]["timeframe"])
                    rows.append((tid, k, "M5", w["key"], "COMPLETED", None, json.dumps(rb.summarize(trades)),
                                 json.dumps(trades), json.dumps(prepared["dataset"], default=str), res["result_hash"]))
                done[tid] += len(cs)
            with ms.store.transaction() as con:
                con.executemany("INSERT OR REPLACE INTO discovery_results (trial_id, config_key, timeframe, window, status, "
                                "reason, metrics, trades, dataset, result_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            mark(f"{w['month']} done")
            del prepared, snaps
        spec_now = load_spec()
        for tid, (params, _cs) in trials.items():
            _update(ms, tid, phase="analysis")
            _update(ms, tid, status="COMPLETED", phase=None, analysis=evaluate(load_results(ms.store, tid), params, spec_now),
                    finished_at=iso(ms.clock.now()))
    except Exception as exc:  # noqa: BLE001 - recorded; the service keeps running
        msg = "cancelled" if isinstance(exc, features.Cancelled) else f"{type(exc).__name__}: {exc}"
        for tid in trials:
            _update(ms, tid, status="FAILED", error=msg, phase=None, finished_at=iso(ms.clock.now()))


def load_results(store: Any, tid: str) -> dict[tuple[str, str], dict[str, Any]]:
    out = {}
    with store.read() as con:
        for r in con.execute("SELECT config_key, window, status, reason, trades FROM discovery_results WHERE trial_id = ? "
                             "AND timeframe = 'M5'", (tid,)):
            out[(r["config_key"], r["window"])] = {"status": r["status"], "reason": r["reason"],
                                                   "trades": json.loads(r["trades"]) if r["trades"] else []}
    return out


# ------------------------------------------------------------------ development analysis (pure)
def stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    m = rb.summarize(trades)
    for k in (2.0, 3.0):
        rs = [x for x in (vd.stressed_r(t, k) for t in trades) if x is not None]
        m[f"expectancy_r_costs_x{k:g}"] = round(sum(rs) / len(rs), 4) if rs else None
    return {k: m[k] for k in ("trades", "win_rate", "sum_r", "expectancy_r", "profit_factor", "max_dd_r", "net_usd",
                              "expectancy_r_costs_x2", "expectancy_r_costs_x3")}


def breakdown(trades: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        key = SESSION_BUCKETS.get(t.get(field), t.get(field) or "unknown") if field == "session" else (t.get(field) or "unknown")
        groups.setdefault(key, []).append(t)
    return {k: stats(v) for k, v in sorted(groups.items())}


def _e(s: dict[str, Any] | None) -> float | None:
    return None if not s or not s["trades"] else s["expectancy_r"]


def check_prediction(pred: dict[str, Any], per_config: dict[str, dict[str, Any]]) -> dict[str, Any]:
    t = pred["type"]
    if t == "regime_interaction":
        c = per_config.get(pred["continuation"], {}).get("by_vol_m15", {})
        f = per_config.get(pred["reversal"], {}).get("by_vol_m15", {})
        n = pred["min_trades_per_cell"]
        cells = {"continuation_expanding": c.get("expanding"), "continuation_contracting": c.get("contracting"),
                 "reversal_contracting": f.get("contracting"), "reversal_expanding": f.get("expanding")}
        e = {k: _e(v) for k, v in cells.items()}
        sign_c = None not in (e["continuation_expanding"], e["continuation_contracting"]) and \
            e["continuation_expanding"] > e["continuation_contracting"]
        sign_f = None not in (e["reversal_contracting"], e["reversal_expanding"]) and \
            e["reversal_contracting"] > e["reversal_expanding"]
        favoured = [cells["continuation_expanding"], cells["reversal_contracting"]]
        fav_ok = all(s and s["trades"] >= n and s["expectancy_r"] > 0 for s in favoured)
        holds = bool(sign_c and sign_f and fav_ok)
        detail = (f"VOL-C expanding {_r(e['continuation_expanding'])} vs contracting {_r(e['continuation_contracting'])}; "
                  f"VOL-F contracting {_r(e['reversal_contracting'])} vs expanding {_r(e['reversal_expanding'])} R/trade; "
                  f"favoured cells {[(s or {}).get('trades', 0) for s in favoured]} trades")
        return {"holds": holds, "detail": detail, "parts": {"continuation_sign": bool(sign_c), "reversal_sign": bool(sign_f),
                                                            "favoured_positive": fav_ok}, "cells": cells}
    if t == "context_adds":
        w, wo = per_config.get(pred["with"], {}).get("pooled"), per_config.get(pred["without"], {}).get("pooled")
        better = None not in (_e(w), _e(wo)) and _e(w) > _e(wo)
        positive = bool(w and w["trades"] >= pred["min_trades"] and w["expectancy_r"] > 0)
        return {"holds": bool(better and positive),
                "detail": f"{pred['with']} {_r(_e(w))} ({(w or {}).get('trades', 0)} trades) vs {pred['without']} "
                          f"{_r(_e(wo))} ({(wo or {}).get('trades', 0)} trades) R/trade",
                "parts": {"better_than_control": bool(better), "positive_with_enough_trades": positive}}
    if t == "session_persistence":
        cfg = per_config.get(pred["config"], {})
        fav = cfg.get("by_session", {}).get("London open")
        cmp_ = cfg.get("by_session", {}).get("Asian")
        months = cfg.get("london_open_months", {})
        eligible = {m: s for m, s in months.items() if s["trades"] >= pred["min_trades_per_month"]}
        positive_months = sum(1 for s in eligible.values() if s["sum_r"] > 0)
        share = positive_months / len(eligible) if eligible else 0.0
        positive = bool(fav and fav["trades"] >= pred["min_trades"] and fav["expectancy_r"] > 0)
        better = None not in (_e(fav), _e(cmp_)) and _e(fav) > _e(cmp_)
        persistent = bool(eligible) and share >= pred["min_month_share"]
        return {"holds": bool(positive and better and persistent),
                "detail": f"London open {_r(_e(fav))} ({(fav or {}).get('trades', 0)} trades) vs Asian {_r(_e(cmp_))} "
                          f"({(cmp_ or {}).get('trades', 0)} trades) R/trade; positive in {positive_months} of "
                          f"{len(eligible)} months with >= {pred['min_trades_per_month']} trades",
                "parts": {"positive_with_enough_trades": positive, "better_than_asian": bool(better),
                          "persistent": persistent}}
    raise DiscoveryError(f"unknown prediction type {t}")


def _r(x: float | None) -> str:
    return "-" if x is None else f"{x:+.3f}"


def evaluate(results: dict[tuple[str, str], dict[str, Any]], params: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    windows = params["windows"]
    keys = sorted({k for k, _w in results})
    per_config: dict[str, dict[str, Any]] = {}
    for k in keys:
        valid = [w for w in windows if (results.get((k, w["key"])) or {}).get("status") == "COMPLETED"]
        trades = [t for w in valid for t in results[(k, w["key"])]["trades"]]
        london = {w["month"]: stats([t for t in results[(k, w["key"])]["trades"]
                                     if SESSION_BUCKETS.get(t.get("session")) == "London open"]) for w in valid}
        per_config[k] = {"pooled": stats(trades), "valid_months": len(valid), "months": {
            w["month"]: (stats(results[(k, w["key"])]["trades"]) if w in valid else
                         {"status": (results.get((k, w["key"])) or {}).get("status", "not run"),
                          "reason": (results.get((k, w["key"])) or {}).get("reason")}) for w in windows},
            "by_vol_m15": breakdown(trades, "vol_m15"), "by_vol_m5": breakdown(trades, "vol_m5"),
            "by_session": breakdown(trades, "session"), "by_h1": breakdown(trades, "h1"),
            "london_open_months": london}
    hyp = hypothesis(spec, params["hypothesis"])
    out = {"definition": DEFINITION, "hypothesis": hyp["id"], "kind": params["kind"], "configs": per_config}
    if params["kind"] == "baseline":
        pred = check_prediction(hyp["prediction"], per_config)
        out["prediction"] = pred
        out["gate"] = ("prediction holds on development data: the hypothesis may be refined (within its limits) and "
                       "frozen for sealed validation" if pred["holds"] else
                       "prediction does not hold on development data: refinements may still be tried within the limits; "
                       "if it never holds, record the hypothesis as not supported and keep the sealed data unspent")
    return out
