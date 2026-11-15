"""Phase 5.3: frozen pre-June historical validation (definition validation@1). Simulation only.

PROTOCOL (frozen before any January-May 2026 data was imported):
  candidates   exactly four variants already selected in Phase 5.2 (study rb_7ca33b7b5a81), rules copied byte for byte
               and fingerprint-checked: R2|v-d-Xb, R1|v-b-Xd, R5|v-d-Xd-tr, R7|v-c-Xe-tr
  benchmarks   their originals R2, R1, R5, R7, unchanged (fingerprint-checked against the same study)
  period       1 January - 31 May 2026: five calendar months, each a SEPARATE hypothetical $1,000 account
  data         Vantage MT5 XAUUSD bid candles exported by the user (M5, M15, H1) into its own research-store series
               (SERIES below); analysis base M5; decisions and execution on M5 and on M15 (M1 is not part of this round)
  execution    identical to Phase 5.2: the same engine code, costs (candle spread else $0.30; $3.50 per lot per side;
               $0.05 adverse slippage), 0.02 lots as configured, next-open fills, stop-first, month-end close
  selection    NONE. January-May is never used to choose, tune or replace anything. Results are reported separately and
               never change a Phase 5.2 classification.
CRITERIA (Phase 5.2 numerical thresholds; R after costs per trade; M5 unless stated; months = the five months that
have sufficient data):
  expectancy     pooled expectancy > 0
  trade_count    at least 30 pooled trades - a minimum SCREEN, not proof of statistical reliability
  profit_factor  pooled PF >= 1.1 AND PF > 1 in at least ceil(0.75 x months) months (4 of 5)
  drawdown       recovery factor (pooled sum R / max drawdown in R, all trades in time order) >= 1
  consistency    at least ceil(0.75 x months) months with a positive sum of R (4 of 5) AND the pooled sum stays positive
                 without the best month. A month with sufficient data but no trade counts as NOT positive.
  timeframes     pooled M15 expectancy > 0 as well as M5
  execution      pooled expectancy still > 0 with spread + slippage doubled (tripled is reported, not a criterion)
  data           at least 4 of the 5 months must have sufficient data (95 % coverage and 5 days of history); a month
                 without sufficient data is INCOMPLETE and excluded from every count above
CLASSES (historical research candidates, not trading readiness; overfit does not apply - nothing is developed here):
  promising     every criterion passes
  inconclusive  pooled expectancy > 0 but at least one criterion fails (including too few trades or too few months)
  rejected      pooled expectancy <= 0, or fewer than 30 trades and not positive"""
from __future__ import annotations

import json
import math
import secrets
import threading
from datetime import UTC, datetime
from typing import Any

from goldscalp.backtesting import features
from goldscalp.core.clock import iso
from goldscalp.core.timeframes import Timeframe
from goldscalp.monthly import robustness as rb
from goldscalp.monthly import runner
from goldscalp.monthly import verdicts as vd
from goldscalp.strategies import builder

DEFINITION = "validation@1"
SOURCE_STUDY = "rb_7ca33b7b5a81"
CANDIDATES = {"R2": "R2|v-d-Xb", "R1": "R1|v-b-Xd", "R5": "R5|v-d-Xd-tr", "R7": "R7|v-c-Xe-tr"}
FINGERPRINTS = {"R2|v-d-Xb": "655888c68fa6b289", "R1|v-b-Xd": "4a48dc6838b22687", "R5|v-d-Xd-tr": "a071703036513072",
                "R7|v-c-Xe-tr": "566c147b5370418e", "R2|original": "eb622d47fb2ffb99", "R1|original": "b549a86b00d99229",
                "R5|original": "952765a67b99e71a", "R7|original": "40d023f434fe1049"}   # first 16 hex of SHA-256
PERIOD = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC))
SERIES = ("research", "csv:vantage-mt5-xauusd-2026-jan-may")
TIMEFRAMES = ("M5", "M15")
MIN_MONTHS = 4
SHARE = 0.75


class ValidationError(ValueError):
    pass


def windows() -> list[dict[str, Any]]:
    out, cur, n = [], PERIOD[0], 1
    while cur < PERIOD[1]:
        nxt = datetime(cur.year + (cur.month == 12), cur.month % 12 + 1, 1, tzinfo=UTC)
        out.append({"key": f"V{n}", "start": iso(cur), "end": iso(min(nxt, PERIOD[1])), "month": cur.strftime("%Y-%m"),
                    "role": "validation", "partial": False})
        cur, n = nxt, n + 1
    return out


def locked_configs(con: Any) -> dict[str, dict[str, Any]]:
    """The exact rules of the 4 candidates and their 4 originals, copied from the Phase 5.2 study and verified."""
    keys = list(CANDIDATES.values()) + [f"{k}|original" for k in CANDIDATES]
    out = {}
    for k in keys:
        row = con.execute("SELECT config, fingerprint FROM robustness_configs WHERE study_id = ? AND key = ?",
                          (SOURCE_STUDY, k)).fetchone()
        if row is None:
            raise ValidationError(f"{k} is not stored in {SOURCE_STUDY}")
        cfg = json.loads(row[0])
        fp = builder.fingerprint(cfg)
        if fp != row[1] or not fp.startswith(FINGERPRINTS[k]):
            raise ValidationError(f"{k}: fingerprint {fp[:16]} does not match the locked {FINGERPRINTS[k]}")
        out[k] = {"config": cfg, "fingerprint": fp, "role": "benchmark" if k.endswith("|original") else "candidate",
                  "strategy": k.split("|")[0]}
    return out


def create(ms: Any) -> dict[str, Any]:
    with ms.store.read() as con:
        locked = locked_configs(con)
    vid = "vl_" + secrets.token_hex(6)
    wins = windows()
    params = {"definition": DEFINITION, "source_study": SOURCE_STUDY, "candidates": CANDIDATES, "windows": wins,
              "series": list(SERIES), "timeframes": list(TIMEFRAMES), "costs": rb.COSTS, "lots": "as configured (0.02)",
              "starting_balance": runner.STARTING_BALANCE, "leverage": runner.LEVERAGE, "lookback_days": rb.LOOKBACK_DAYS,
              "configs": {k: {kk: v[kk] for kk in ("fingerprint", "role", "strategy")} for k, v in locked.items()},
              "selection": "none", "criteria": __doc__}
    now = ms.clock.now()
    with ms.store.transaction() as con:
        con.execute("INSERT INTO validation_studies (id, status, params, configs, app_version, created_at, updated_at, "
                    "progress, total) VALUES (?, 'READY', ?, ?, ?, ?, ?, 0, ?)",
                    (vid, json.dumps(params), json.dumps({k: v["config"] for k, v in locked.items()}, sort_keys=True),
                     runner.app_version(), iso(now), iso(now), len(locked) * len(wins) * len(TIMEFRAMES)))
    return {"id": vid, "windows": wins, "configs": len(locked)}


def _update(ms: Any, vid: str, **fields: Any) -> None:
    sets, args = ["updated_at = ?"], [iso(ms.clock.now())]
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        args.append(json.dumps(v, default=str) if k == "analysis" else v)
    with ms.store.transaction() as con:
        con.execute(f"UPDATE validation_studies SET {', '.join(sets)} WHERE id = ?", (*args, vid))


def run(ms: Any, vid: str, cancel: threading.Event) -> None:
    """Every locked configuration on every month and timeframe; one timeline per (timeframe, month)."""
    with ms.store.read() as con:
        row = con.execute("SELECT params, configs FROM validation_studies WHERE id = ?", (vid,)).fetchone()
    params, configs = json.loads(row["params"]), json.loads(row["configs"])
    for k, c in configs.items():                 # re-verified at run time: nothing can have drifted
        if builder.fingerprint(c) != params["configs"][k]["fingerprint"]:
            _update(ms, vid, status="FAILED", error=f"{k}: rules changed after the study was created")
            return
    spec, _contract = runner.contract_spec(ms.app_spec, ms.sources.verified_spec())
    assumptions = runner.make_assumptions(rb.COSTS)
    detail = set().union(*(builder.detail_timeframes(c) for c in configs.values()))
    store, series = params["series"]
    srow = ms.sources.series_row(store, series)
    _update(ms, vid, status="RUNNING", error=None)
    done = 0
    try:
        for tf_name in params["timeframes"]:
            tf = Timeframe.parse(tf_name)
            for w in params["windows"]:
                start, end = runner.parse_iso(w["start"]), runner.parse_iso(w["end"])
                if srow is None:
                    cov = {"sufficient": False, "problems": [f"series {series} not found in the {store} store"]}
                else:
                    cov = rb._coverage(ms.sources, store, srow, Timeframe.M5, start, end, ms.app_spec.schedule)
                    if cov["sufficient"] and tf is not Timeframe.M5:
                        ex = rb._coverage(ms.sources, store, srow, tf, start, end, ms.app_spec.schedule)
                        cov = ex if not ex["sufficient"] else cov
                if not cov["sufficient"]:
                    with ms.store.transaction() as con:
                        con.executemany("INSERT OR REPLACE INTO validation_results (study_id, config_key, timeframe, "
                                        "window, status, reason) VALUES (?, ?, ?, ?, 'INCOMPLETE', ?)",
                                        [(vid, k, tf_name, w["key"], "; ".join(cov["problems"])) for k in configs])
                    done += len(configs)
                    _update(ms, vid, progress=done, phase=f"{tf_name} {w['month']} incomplete")
                    continue

                def tick(n: int, tf_name: str = tf_name, w: dict[str, Any] = w) -> None:
                    if n % 500 == 0:
                        _update(ms, vid, phase=f"{tf_name} {w['month']} timeline: {n} steps")
                prepared = runner.prepare(ms.sources, store, srow, tf, start, end, lookback_days=rb.LOOKBACK_DAYS,
                                          detail=detail, cfg=ms.analysis_cfg, progress=tick, cancelled=cancel.is_set)
                snaps = {s["cutoff"]: s for s in prepared["snapshots"]}
                rows = []
                for k, cfg in configs.items():
                    if cancel.is_set():
                        raise features.Cancelled("cancelled")
                    res = runner.run_one(cfg, prepared, tf, start, end, assumptions, spec)
                    trades = rb.compact(res["trades"], snaps, cfg["exit"]["stop"]["timeframe"])
                    rows.append((vid, k, tf_name, w["key"], "COMPLETED", None, json.dumps(rb.summarize(trades)),
                                 json.dumps(trades), json.dumps(prepared["dataset"], default=str), res["result_hash"]))
                done += len(configs)
                with ms.store.transaction() as con:
                    con.executemany("INSERT OR REPLACE INTO validation_results (study_id, config_key, timeframe, window, "
                                    "status, reason, metrics, trades, dataset, result_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    rows)
                _update(ms, vid, progress=done, phase=f"{tf_name} {w['month']} done")
                del prepared, snaps
        _update(ms, vid, phase="analysis")
        _update(ms, vid, status="COMPLETED", phase=None, analysis=evaluate(load_results(ms.store, vid), params),
                finished_at=iso(ms.clock.now()))
    except Exception as exc:  # noqa: BLE001 - recorded; the service keeps running
        msg = "cancelled" if isinstance(exc, features.Cancelled) else f"{type(exc).__name__}: {exc}"
        _update(ms, vid, status="FAILED", error=msg, phase=None, finished_at=iso(ms.clock.now()))


def load_results(store: Any, vid: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    out = {}
    with store.read() as con:
        for r in con.execute("SELECT config_key, timeframe, window, status, reason, trades FROM validation_results WHERE "
                             "study_id = ?", (vid,)):
            out[(r["config_key"], r["timeframe"], r["window"])] = {
                "status": r["status"], "reason": r["reason"], "metrics": None,
                "trades": json.loads(r["trades"]) if r["trades"] else []}
    return out


# ------------------------------------------------------------------ evaluation (pure)
def _f(x: Any, n: int = 3) -> str:
    return "-" if x is None else f"{x:+.{n}f}"


def _stressed(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """summarize() plus expectancy and sum of R with spread + slippage x2 and x3 (reported; x2 is a criterion)."""
    m = rb.summarize(trades)
    for k in (2.0, 3.0):
        rs = [x for x in (vd.stressed_r(t, k) for t in trades) if x is not None]
        m[f"expectancy_r_costs_x{k:g}"] = round(sum(rs) / len(rs), 4) if rs else None
        m[f"sum_r_costs_x{k:g}"] = round(sum(rs), 3) if rs else None
    return m


def assess(results: dict, key: str, months: list[str]) -> dict[str, Any]:
    """The protocol's criteria for one configuration (see the module docstring)."""
    valid = [m for m in months if (results.get((key, "M5", m)) or {}).get("status") == "COMPLETED"]
    per = {m: results[(key, "M5", m)]["trades"] for m in valid}
    pooled = [t for m in valid for t in per[m]]
    p = _stressed(pooled)
    by = {m: _stressed(per[m]) for m in valid}
    need = math.ceil(SHARE * len(valid)) if valid else MIN_MONTHS
    positive = sum(1 for m in valid if by[m]["trades"] and by[m]["sum_r"] > 0)
    pf_ok = sum(1 for m in valid if (by[m]["profit_factor"] or 0) > 1)
    best = max(valid, key=lambda m: by[m]["sum_r"]) if valid else None
    without_best = round(sum(by[m]["sum_r"] for m in valid if m != best), 3) if valid else None
    m15_valid = [m for m in months if (results.get((key, "M15", m)) or {}).get("status") == "COMPLETED"]
    m15 = _stressed([t for m in m15_valid for t in results[(key, "M15", m)]["trades"]])
    e = p["expectancy_r"]
    rec = (p["sum_r"] / p["max_dd_r"]) if p["max_dd_r"] else (float("inf") if p["trades"] and p["sum_r"] > 0 else None)
    crit = {
        "data": {"pass": len(valid) >= MIN_MONTHS, "detail": f"{len(valid)} of {len(months)} months with sufficient data "
                                                             f"(at least {MIN_MONTHS} needed)"},
        "expectancy": {"pass": e is not None and e > 0, "detail": f"pooled {_f(e)} R/trade"},
        "trade_count": {"pass": p["trades"] >= vd.MIN_TRADES,
                        "detail": f"{p['trades']} trades (minimum screen {vd.MIN_TRADES}; not proof of reliability)"},
        "profit_factor": {"pass": (p["profit_factor"] or 0) >= 1.1 and pf_ok >= need,
                          "detail": f"pooled PF {_f(p['profit_factor'], 2)}; PF > 1 in {pf_ok} of {len(valid)} months "
                                    f"(need {need})"},
        "drawdown": {"pass": rec is not None and rec >= 1,
                     "detail": f"sum {_f(p['sum_r'], 2)} R vs max drawdown {_f(p['max_dd_r'], 2)} R"},
        "consistency": {"pass": positive >= need and without_best is not None and without_best > 0,
                        "detail": f"{positive} of {len(valid)} months positive (need {need}); without the best month "
                                  f"({best}) {_f(without_best, 2)} R"},
        "timeframes": {"pass": e is not None and e > 0 and (m15["expectancy_r"] or 0) > 0,
                       "detail": f"M5 {_f(e)} R/trade; M15 {_f(m15['expectancy_r'])} R/trade ({m15['trades']} trades)"},
        "execution": {"pass": (p["expectancy_r_costs_x2"] or 0) > 0,
                      "detail": f"x2 {_f(p['expectancy_r_costs_x2'])} R/trade; x3 {_f(p['expectancy_r_costs_x3'])} R/trade"},
    }
    if e is None or e <= 0:
        cls, why = "rejected", f"pooled expectancy {_f(e)} R/trade after costs ({p['trades']} trades)"
    elif all(c["pass"] for c in crit.values()):
        cls, why = "promising", "passes every criterion of the frozen protocol on January-May 2026"
    else:
        cls, why = "inconclusive", "positive, but fails: " + ", ".join(k for k, c in crit.items() if not c["pass"])
    return {"class": cls, "why": why, "criteria": crit, "pooled": p, "m15": m15,
            "months": {m: ({"status": "COMPLETED", **{k: by[m][k] for k in ("trades", "sum_r", "expectancy_r",
                                                                              "profit_factor", "net_usd", "sum_r_costs_x2",
                                                                              "sum_r_costs_x3")}}
                           if m in by else {"status": (results.get((key, "M5", m)) or {}).get("status", "not run"),
                                            "reason": (results.get((key, "M5", m)) or {}).get("reason")})
                       for m in months},
            "m15_months": {m: {k: v for k, v in rb.summarize(results[(key, "M15", m)]["trades"]).items()
                               if k in ("trades", "sum_r", "expectancy_r")} for m in m15_valid}}


def evaluate(results: dict, params: dict[str, Any]) -> dict[str, Any]:
    months = [w["key"] for w in params["windows"]]
    out = {"definition": DEFINITION, "months": {w["key"]: w["month"] for w in params["windows"]}, "strategies": {}}
    for strategy, cand in params["candidates"].items():
        out["strategies"][strategy] = {"candidate": {"key": cand, **assess(results, cand, months)},
                                       "original": {"key": f"{strategy}|original",
                                                    **assess(results, f"{strategy}|original", months)}}
    out["summary"] = {s: {"candidate": v["candidate"]["class"], "original": v["original"]["class"]}
                      for s, v in out["strategies"].items()}
    return out
