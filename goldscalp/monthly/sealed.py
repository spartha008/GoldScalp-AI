"""Phase 6 H6.4: the one sealed validation (definition sealed@1). Historical simulation only; research store only.

H6.4 is registered in ``config/discovery/phase6-h64.json``: one candidate (VOL-C plus exactly one condition, no entries
while M15 volatility is contracting) and the unfiltered VOL-C as a comparison baseline that is never selected instead.
The registration, this code and the data verification are frozen by hash (``sealed_report --freeze``) before the sealed
series is ever run. Rules enforced here:

* development runs use only the Phase 6 development windows (``discovery.guard``); their result is disclosed and
  does not decide whether the sealed test runs;
* the sealed run reads only the sealed series, scores only November 2023 to October 2025, runs once (a run that failed
  before producing any result may be restarted, and every attempt is kept), and refuses unless the H6.4 protocol is
  frozen and unchanged;
* coverage is measured against the market calendar fixed in the registration (missing bars inside listed holiday
  closures are not expected); the 95 % threshold is unchanged;
* evaluation applies the Phase 6 sealed criteria exactly as pre-registered."""
from __future__ import annotations

import json
import math
import secrets
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from goldscalp.backtesting import features
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe
from goldscalp.monthly import discovery as dv
from goldscalp.monthly import robustness as rb
from goldscalp.monthly import runner
from goldscalp.strategies import builder

DEFINITION = "sealed@1"
SPEC_FILE = dv.ROOT / "config" / "discovery" / "phase6-h64.json"
MIN_VALID_MONTHS = 18
MONTH_SHARE = 0.6
MIN_TRADES = 100
MIN_PF = 1.1


class SealedError(ValueError):
    pass


def load_spec(path: Path | None = None) -> dict[str, Any]:
    spec = json.loads((path or SPEC_FILE).read_text(encoding="utf-8"))
    if spec.get("definition") != DEFINITION:
        raise SealedError("unexpected registration definition")
    cand = builder.validate(spec["candidate"]["config"])
    ctrl = builder.validate(spec["control"]["config"])
    phase6 = dv.load_spec()
    if builder.fingerprint(ctrl) != builder.fingerprint(phase6["configs"]["VOL-C"]):
        raise SealedError("the control is not the frozen Phase 6 VOL-C baseline")
    extra = [c for c in cand["conditions"] if c not in ctrl["conditions"]]
    if (cand["conditions"][:-1] != ctrl["conditions"] or len(extra) != 1 or extra[0]["type"] != "volatility_regime"
            or "contracting" in extra[0]["allowed"] or any(cand[k] != ctrl[k] for k in ("entry", "exit", "risk", "direction"))):
        raise SealedError("the candidate must be VOL-C plus exactly one condition excluding contracting volatility")
    spec["candidate"]["config"], spec["control"]["config"] = cand, ctrl
    return spec


def configs(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {spec["candidate"]["key"]: spec["candidate"]["config"], spec["control"]["key"]: spec["control"]["config"]}


def sealed_windows() -> list[dict[str, Any]]:
    out, cur, n = [], dv.SEALED_EVAL[0], 1
    store, series = "research", "csv:vantage-mt5-xauusd-sealed-2023-2025"
    while cur < dv.SEALED_EVAL[1]:
        nxt = datetime(cur.year + (cur.month == 12), cur.month % 12 + 1, 1, tzinfo=UTC)
        out.append({"key": f"S{n}", "start": iso(cur), "end": iso(nxt), "month": cur.strftime("%Y-%m"), "store": store,
                    "series": series, "half": 1 if n <= 12 else 2, "partial": False})
        cur, n = nxt, n + 1
    return out


# ------------------------------------------------------------------ calendar-aware coverage (fixed in the registration)
def holiday_windows(spec: dict[str, Any]) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    rule = spec["coverage_rule"]
    out = []
    for d in rule["full_closures"]:
        day = pd.Timestamp(d, tz="UTC")
        out.append((day - pd.Timedelta(hours=2), day + pd.Timedelta(days=1)))          # [D-1 22:00, D+1 00:00)
    for d in rule["early_closes"]:
        day = pd.Timestamp(d, tz="UTC")
        out.append((day + pd.Timedelta(hours=17), day + pd.Timedelta(days=1)))         # [D 17:00, D+1 00:00)
    return out


def adjusted_share(expected: pd.DatetimeIndex, present: pd.DatetimeIndex,
                   holidays: list[tuple[pd.Timestamp, pd.Timestamp]]) -> dict[str, Any]:
    missing = expected.difference(present)
    in_holiday = pd.Series(False, index=missing)
    for a, b in holidays:
        in_holiday |= (missing >= a) & (missing < b)
    excused = int(in_holiday.sum())
    denom = len(expected) - excused
    have = len(present.intersection(expected))
    return {"expected": int(len(expected)), "present": have, "missing": int(len(missing)), "holiday_missing": excused,
            "plain_share": round(have / len(expected), 4) if len(expected) else 0.0,
            "share": round(min(1.0, have / denom), 4) if denom > 0 else 0.0}


def coverage(src: Any, store: str, row: Any, tf: Timeframe, start: datetime, end: datetime, schedule: Any,
             spec: dict[str, Any]) -> dict[str, Any]:
    rule = spec["coverage_rule"]
    frame = src.load(store, row, tf, start - timedelta(days=rule["warmup_days"] + 7), end, limit=2_000_000)
    expected = schedule.expected_bars(start, end, tf.seconds)
    present = frame.index[(frame.index >= pd.Timestamp(start)) & (frame.index < pd.Timestamp(end))]
    out = adjusted_share(expected, present, holiday_windows(spec))
    scheduled = schedule.expected_bars(start - timedelta(days=rule["warmup_days"]), start, tf.seconds)
    needed = scheduled[0] if len(scheduled) else pd.Timestamp(start - timedelta(days=rule["warmup_days"]))
    before = frame.index[frame.index < pd.Timestamp(start)]
    warm = len(before) > 0 and before[0] <= needed
    problems = []
    if out["share"] < rule["threshold"]:
        problems.append(f"{tf.value} coverage {out['share']:.1%} after market holidays (need {rule['threshold']:.0%})")
    if not warm:
        problems.append(f"{tf.value} warm-up history missing before {iso(start)}")
    return {**out, "warmup": warm, "sufficient": not problems, "problems": problems}


# ------------------------------------------------------------------ runs
def _runs(con: Any) -> list[Any]:
    return con.execute("SELECT * FROM sealed_runs ORDER BY created_at").fetchall()


def create_run(ms: Any, purpose: str, *, protocol: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = load_spec()
    cfgs = configs(spec)
    with ms.store.read() as con:
        runs = _runs(con)
        result_rows = {r["id"]: con.execute("SELECT COUNT(*) FROM sealed_results WHERE run_id = ? AND status = 'COMPLETED'",
                                            (r["id"],)).fetchone()[0] for r in runs}
    if purpose == "development":
        windows = dv.dev_windows()
        for w in windows:
            dv.guard(runner.parse_iso(w["start"]), runner.parse_iso(w["end"]), rb.LOOKBACK_DAYS)
        timeframes = ["M5"]
    elif purpose == "sealed":
        if not protocol or not protocol.get("frozen") or not protocol.get("unchanged"):
            raise SealedError(f"the H6.4 protocol must be frozen and unchanged before the sealed run: {protocol}")
        for r in runs:
            if r["purpose"] == "sealed" and (r["status"] != "FAILED" or result_rows[r["id"]] > 0):
                raise SealedError(f"the sealed test has already run ({r['id']}, {r['status']}); it runs once and H6.4 "
                                  "is never retuned on its result")
        windows = sealed_windows()
        for w in windows:
            a, b = runner.parse_iso(w["start"]), runner.parse_iso(w["end"])
            if a < dv.SEALED_EVAL[0] or b > dv.SEALED_EVAL[1]:
                raise SealedError(f"{w['key']} lies outside the registered sealed window")
        timeframes = ["M5", "M15"]
    else:
        raise SealedError("purpose is 'development' or 'sealed'")
    rid = "sr_" + secrets.token_hex(6)
    now = ms.clock.now()
    attempt = 1 + sum(1 for r in runs if r["purpose"] == purpose)
    params = {"definition": DEFINITION, "hypothesis": spec["hypothesis"]["id"], "purpose": purpose, "attempt": attempt,
              "windows": windows, "timeframes": timeframes, "costs": rb.COSTS, "protocol": protocol,
              "fingerprints": {k: builder.fingerprint(c) for k, c in cfgs.items()},
              "roles": {spec["candidate"]["key"]: "candidate", spec["control"]["key"]: "control (comparison only)"}}
    with ms.store.transaction() as con:
        con.execute("INSERT INTO sealed_runs (id, hypothesis, purpose, status, params, configs, app_version, created_at, "
                    "updated_at, progress, total) VALUES (?, ?, ?, 'READY', ?, ?, ?, ?, ?, 0, ?)",
                    (rid, spec["hypothesis"]["id"], purpose, json.dumps(params), json.dumps(cfgs, sort_keys=True),
                     runner.app_version(), iso(now), iso(now), len(cfgs) * len(windows) * len(timeframes)))
    return {"id": rid, "purpose": purpose, "attempt": attempt, "windows": len(windows), "timeframes": timeframes}


def _update(ms: Any, rid: str, **fields: Any) -> None:
    sets, args = ["updated_at = ?"], [iso(ms.clock.now())]
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        args.append(json.dumps(v, default=str) if k == "analysis" else v)
    with ms.store.transaction() as con:
        con.execute(f"UPDATE sealed_runs SET {', '.join(sets)} WHERE id = ?", (*args, rid))


def run(ms: Any, rid: str, cancel: threading.Event) -> None:
    with ms.store.read() as con:
        row = con.execute("SELECT params, configs FROM sealed_runs WHERE id = ?", (rid,)).fetchone()
    params, cfgs = json.loads(row["params"]), json.loads(row["configs"])
    for k, c in cfgs.items():
        if builder.fingerprint(c) != params["fingerprints"][k]:
            _update(ms, rid, status="FAILED", error=f"{k}: rules changed after the run was created")
            return
    spec = load_spec()
    cspec, _contract = runner.contract_spec(ms.app_spec, ms.sources.verified_spec())
    assumptions = runner.make_assumptions(rb.COSTS)
    detail = set().union(*(builder.detail_timeframes(c) for c in cfgs.values()))
    _update(ms, rid, status="RUNNING", error=None)
    done = 0
    try:
        for tf_name in params["timeframes"]:
            tf = Timeframe.parse(tf_name)
            for w in params["windows"]:
                start, end = runner.parse_iso(w["start"]), runner.parse_iso(w["end"])
                if params["purpose"] == "development":
                    dv.guard(start, end, rb.LOOKBACK_DAYS)
                srow = ms.sources.series_row(w["store"], w["series"])
                if srow is None:
                    cov = {"sufficient": False, "problems": [f"series {w['series']} not found in the {w['store']} store"]}
                else:
                    cov = coverage(ms.sources, w["store"], srow, Timeframe.M5, start, end, ms.app_spec.schedule, spec)
                    if cov["sufficient"] and tf is not Timeframe.M5:
                        ex = coverage(ms.sources, w["store"], srow, tf, start, end, ms.app_spec.schedule, spec)
                        cov = ex if not ex["sufficient"] else cov
                if not cov["sufficient"]:
                    with ms.store.transaction() as con:
                        con.executemany("INSERT OR REPLACE INTO sealed_results (run_id, config_key, timeframe, window, "
                                        "status, reason) VALUES (?, ?, ?, ?, 'INCOMPLETE', ?)",
                                        [(rid, k, tf_name, w["key"], "; ".join(cov["problems"])) for k in cfgs])
                    done += len(cfgs)
                    _update(ms, rid, progress=done, phase=f"{tf_name} {w['month']} incomplete")
                    continue

                def tick(n: int, tf_name: str = tf_name, w: dict[str, Any] = w) -> None:
                    if n % 500 == 0:
                        _update(ms, rid, phase=f"{tf_name} {w['month']} timeline: {n} steps")
                prepared = runner.prepare(ms.sources, w["store"], srow, tf, start, end, lookback_days=rb.LOOKBACK_DAYS,
                                          detail=detail, cfg=ms.analysis_cfg, progress=tick, cancelled=cancel.is_set)
                snaps = {s["cutoff"]: s for s in prepared["snapshots"]}
                rows = []
                for k, cfg in cfgs.items():
                    if cancel.is_set():
                        raise features.Cancelled("cancelled")
                    res = runner.run_one(cfg, prepared, tf, start, end, assumptions, cspec)
                    trades = rb.compact(res["trades"], snaps, cfg["exit"]["stop"]["timeframe"])
                    rows.append((rid, k, tf_name, w["key"], "COMPLETED", None, json.dumps(rb.summarize(trades)),
                                 json.dumps(trades), json.dumps(prepared["dataset"], default=str), res["result_hash"]))
                done += len(cfgs)
                with ms.store.transaction() as con:
                    con.executemany("INSERT OR REPLACE INTO sealed_results (run_id, config_key, timeframe, window, status, "
                                    "reason, metrics, trades, dataset, result_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    rows)
                _update(ms, rid, progress=done, phase=f"{tf_name} {w['month']} done")
                del prepared, snaps
        _update(ms, rid, phase="analysis")
        _update(ms, rid, status="COMPLETED", phase=None, analysis=evaluate(load_results(ms.store, rid), params, spec),
                finished_at=iso(ms.clock.now()))
    except Exception as exc:  # noqa: BLE001 - recorded; the service keeps running
        msg = "cancelled" if isinstance(exc, features.Cancelled) else f"{type(exc).__name__}: {exc}"
        _update(ms, rid, status="FAILED", error=msg, phase=None, finished_at=iso(ms.clock.now()))


def load_results(store: Any, rid: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    out = {}
    with store.read() as con:
        for r in con.execute("SELECT config_key, timeframe, window, status, reason, trades FROM sealed_results "
                             "WHERE run_id = ?", (rid,)):
            out[(r["config_key"], r["timeframe"], r["window"])] = {
                "status": r["status"], "reason": r["reason"], "trades": json.loads(r["trades"]) if r["trades"] else []}
    return out


# ------------------------------------------------------------------ evaluation (pure)
def _series(results: dict, key: str, tf: str, windows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [w for w in windows if (results.get((key, tf, w["key"])) or {}).get("status") == "COMPLETED"]
    per = {w["key"]: results[(key, tf, w["key"])]["trades"] for w in valid}
    pooled = [t for w in valid for t in per[w["key"]]]
    months = {w["month"]: ({"status": "COMPLETED", **dv.stats(per[w["key"]])} if w in valid else
                           {"status": (results.get((key, tf, w["key"])) or {}).get("status", "not run"),
                            "reason": (results.get((key, tf, w["key"])) or {}).get("reason")}) for w in windows}
    halves = {}
    for h in (1, 2):
        ts = [t for w in valid if w.get("half") == h for t in per[w["key"]]]
        halves[h] = dv.stats(ts)
    return {"valid": [w["month"] for w in valid], "pooled": dv.stats(pooled), "months": months, "halves": halves,
            "by_vol_m15": dv.breakdown(pooled, "vol_m15"), "by_session": dv.breakdown(pooled, "session")}


def _f(x: Any, n: int = 3) -> str:
    return "-" if x is None else f"{x:+.{n}f}"


def assess(cand: dict[str, Any], cand_m15: dict[str, Any], ctrl: dict[str, Any], scored_months: int,
           sealed: bool = True) -> dict[str, Any]:
    p = cand["pooled"]
    e = p["expectancy_r"] if p["trades"] else None
    valid = [m for m in cand["valid"]]
    monthly = {m: cand["months"][m] for m in valid}
    positive = sum(1 for s in monthly.values() if s["trades"] and s["sum_r"] > 0)
    need = math.ceil(MONTH_SHARE * len(valid)) if valid else 1
    best = max(valid, key=lambda m: monthly[m]["sum_r"]) if valid else None
    without_best = round(sum(monthly[m]["sum_r"] for m in valid if m != best), 3) if valid else None
    halves_ok = all(cand["halves"][h]["trades"] and cand["halves"][h]["sum_r"] > 0 for h in (1, 2))
    cc = ctrl["by_vol_m15"]
    contracting = cc.get("contracting")
    outside = [t for t in ("normal", "expanding") if t in cc]
    outside_trades = sum(cc[t]["trades"] for t in outside)
    outside_e = (sum(cc[t]["sum_r"] for t in outside) / outside_trades) if outside_trades else None
    contr_e = contracting["expectancy_r"] if contracting and contracting["trades"] else None
    pred_ok = e is not None and e > 0 and contr_e is not None and outside_e is not None and contr_e < outside_e
    m15e = cand_m15["pooled"]["expectancy_r"] if cand_m15["pooled"]["trades"] else None
    crit = {
        "data": {"pass": len(valid) >= MIN_VALID_MONTHS if sealed else len(valid) > 0,
                 "detail": f"{len(valid)} of {scored_months} months with sufficient data (at least {MIN_VALID_MONTHS} needed)"},
        "expectancy": {"pass": e is not None and e > 0, "detail": f"pooled {_f(e)} R/trade after costs"},
        "trade_count": {"pass": p["trades"] >= MIN_TRADES, "detail": f"{p['trades']} trades (screen {MIN_TRADES})"},
        "profit_factor": {"pass": (p["profit_factor"] or 0) >= MIN_PF, "detail": f"pooled PF {_f(p['profit_factor'], 2)}"},
        "consistency": {"pass": positive >= need and halves_ok and without_best is not None and without_best > 0,
                        "detail": f"{positive} of {len(valid)} months positive (need {need}); halves "
                                  f"{_f(cand['halves'][1]['sum_r'], 2)} / {_f(cand['halves'][2]['sum_r'], 2)} R; without the "
                                  f"best month ({best}) {_f(without_best, 2)} R"},
        "prediction": {"pass": bool(pred_ok),
                       "detail": f"candidate {_f(e)} R/trade; control in contracting volatility {_f(contr_e)} "
                                 f"({(contracting or {}).get('trades', 0)} trades) vs outside {_f(outside_e)} "
                                 f"({outside_trades} trades) R/trade"},
        "execution": {"pass": (p["expectancy_r_costs_x2"] or 0) > 0,
                      "detail": f"x2 {_f(p['expectancy_r_costs_x2'])}; x3 {_f(p['expectancy_r_costs_x3'])} R/trade"},
        "timeframes": {"pass": m15e is not None and m15e > 0,
                       "detail": f"M5 {_f(e)}; M15 {_f(m15e)} R/trade ({cand_m15['pooled']['trades']} trades)"},
    }
    if e is None or e <= 0:
        cls = "rejected"
    elif all(c["pass"] for c in crit.values()):
        cls = "promising"
    else:
        cls = "inconclusive"
    return {"class": cls, "criteria": crit, "failed": [k for k, c in crit.items() if not c["pass"]]}


def evaluate(results: dict, params: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    windows = params["windows"]
    ck, ok = spec["candidate"]["key"], spec["control"]["key"]
    out = {"definition": DEFINITION, "purpose": params["purpose"], "configs": {}}
    for k in (ck, ok):
        out["configs"][k] = {tf: _series(results, k, tf, windows) for tf in params["timeframes"]}
    if params["purpose"] == "sealed":
        a = assess(out["configs"][ck]["M5"], out["configs"][ck]["M15"], out["configs"][ok]["M5"], len(windows))
        out["assessment"] = a
        out["control_assessment"] = assess(out["configs"][ok]["M5"], out["configs"][ok]["M15"], out["configs"][ok]["M5"],
                                           len(windows))
        out["control_note"] = "The control is reported for comparison only and is never selected instead of H6.4."
    return out


def runs_overview(store: Any) -> list[dict[str, Any]]:
    with store.read() as con:
        return [dict(r) for r in con.execute("SELECT id, hypothesis, purpose, status, phase, progress, total, created_at, "
                                             "finished_at, error FROM sealed_runs ORDER BY created_at")]
