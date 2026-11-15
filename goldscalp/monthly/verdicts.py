"""Phase 5.2 final classification (definition verdicts@2). Pre-declared on 14 Nov 2026 BEFORE any October / November
result of the full study existed. Pure, read-only functions over a study's stored results; nothing is re-run and no
strategy, configuration or stored analysis is changed. These classes describe HISTORICAL RESEARCH CANDIDATES, not
real-world trading readiness.

verdicts@2 (amended at the user's request, still before any hold-out result existed): OCTOBER IS SECONDARY. Its
historical results (of the old strategies) influenced the redesign hypotheses, so October never decides a class; it is
reported in full as a secondary evaluation (including M1, which exists only for October). November 1-13 is the cleanest
hold-out, but a short sample. August and September are walk-forward months: each is tested with a variant chosen on
earlier months only, but the diagnostic study that shaped the redesign used June-September, so they test the SELECTION,
not the DESIGN. Only November is untouched by both.

Out-of-sample (OOS) months for the verdict: every walk-forward test month except the secondary month(s), each chosen on
DEVELOPMENT months only (a hold-out month never influences any choice). The originals are measured on the same months.

Criteria (all in R after costs per trade; sizing-independent):
  expectancy      pooled OOS expectancy > 0 AND primary hold-out (November) expectancy, development choice, > 0
  trade_count     at least 30 OOS trades - a minimum SCREEN, not proof of statistical reliability
  profit_factor   pooled OOS PF >= 1.1 AND PF > 1 in at least 75 % of OOS months (2 of 3)
  drawdown        OOS recovery factor (sum R / max drawdown in R) >= 1
  consistency     at least 75 % of OOS months positive (2 of 3) AND the OOS sum stays positive without the best month
  timeframes      OOS expectancy > 0 on M15 as well as M5 (M1 exists only for October: secondary, reported)
  execution       OOS expectancy still > 0 with spread + slippage doubled (tripled is reported)
Classes:
  overfit       development >= +0.05 R/trade on >= 30 trades but OOS <= 0
  rejected      OOS <= 0, or fewer than 30 OOS trades and not positive
  promising     every criterion passes
  inconclusive  positive, but at least one criterion fails (including too few trades)"""
from __future__ import annotations

import statistics
from typing import Any

from goldscalp.monthly import robustness as rb

DEFINITION = "verdicts@2"
MIN_TRADES = 30
SECONDARY_MONTHS = ("2026-10",)          # reported, never part of a verdict (verdicts@2)
OVERFIT_DEV_R = 0.05
COMMISSION_PER_LOT_ROUND_TRIP = 7.0      # $3.50 per lot per side (the study's cost assumption)


def stressed_r(t: dict[str, Any], k: float) -> float | None:
    """R of a stored trade if spread + slippage had been k times larger (commission unchanged). Exact for the cost
    component; second-order effects on stop / target triggers are not modelled."""
    if t.get("r") is None or not t.get("risk_usd"):
        return None
    fill_costs = max(0.0, t["cost_usd"] - COMMISSION_PER_LOT_ROUND_TRIP * float(t["lots"]))
    return t["r"] - (k - 1) * fill_costs / t["risk_usd"]


def _metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    m = rb.summarize(trades)
    for k in (2.0, 3.0):
        rs = [x for x in (stressed_r(t, k) for t in trades) if x is not None]
        m[f"expectancy_r_costs_x{k:g}"] = round(sum(rs) / len(rs), 4) if rs else None
    return m


def _trades(results: dict, key: str | None, tf: str, wins: list[str]) -> list[dict[str, Any]]:
    if not key:
        return []
    return [t for w in wins for t in ((results.get((key, tf, w)) or {}).get("trades") or [])
            if (results.get((key, tf, w)) or {}).get("status") == "COMPLETED"]


def _status(results: dict, key: str | None, tf: str, w: str) -> str:
    return (results.get((key, tf, w)) or {}).get("status", "not run") if key else "no choice"


def assess(per_month: dict[str, list[dict[str, Any]]], *, dev: dict[str, Any], hold: dict[str, Any],
           m15_oos: dict[str, Any], m1_hold: dict[str, Any] | None) -> dict[str, Any]:
    """Criteria and class from per-OOS-month trades (M5) plus development, hold-out, M15 and M1 summaries."""
    months = list(per_month)
    pooled = [t for m in months for t in per_month[m]]
    oos = _metrics(pooled)
    by_month = {m: rb.summarize(per_month[m]) for m in months}
    month_sums = {m: by_month[m]["sum_r"] for m in months}
    positive = sum(1 for m in months if month_sums[m] > 0)
    pf_ok_months = sum(1 for m in months if (by_month[m]["profit_factor"] or 0) > 1)
    best = max(month_sums, key=month_sums.get) if months else None
    without_best = round(sum(v for m, v in month_sums.items() if m != best), 3) if months else None
    need = max(1, round(0.75 * len(months)))
    e = oos["expectancy_r"]
    rec = (oos["sum_r"] / oos["max_dd_r"]) if oos["max_dd_r"] else (None if not oos["trades"] else float("inf"))
    crit = {
        "expectancy": {"pass": e is not None and e > 0 and (hold.get("expectancy_r") or 0) > 0,
                       "detail": f"OOS {_f(e)} R/trade; hold-out {_f(hold.get('expectancy_r'))} R/trade"},
        "trade_count": {"pass": oos["trades"] >= MIN_TRADES,
                        "detail": f"{oos['trades']} OOS trades (minimum screen {MIN_TRADES}; not proof of reliability)"},
        "profit_factor": {"pass": (oos["profit_factor"] or 0) >= 1.1 and pf_ok_months >= need,
                          "detail": f"pooled PF {_f(oos['profit_factor'], 2)}; PF > 1 in {pf_ok_months} of {len(months)} months"},
        "drawdown": {"pass": rec is not None and rec >= 1,
                     "detail": f"sum {_f(oos['sum_r'], 2)} R vs max drawdown {_f(oos['max_dd_r'], 2)} R "
                               f"(recovery {('inf' if rec == float('inf') else _f(rec, 2)) if rec is not None else '-'})"},
        "consistency": {"pass": positive >= need and without_best is not None and without_best > 0,
                        "detail": f"{positive} of {len(months)} months positive; without the best month ({best}) "
                                  f"{_f(without_best, 2)} R"},
        "timeframes": {"pass": (m15_oos.get("expectancy_r") or 0) > 0 and e is not None and e > 0,
                       "detail": f"M5 {_f(e)} R/trade, M15 {_f(m15_oos.get('expectancy_r'))} R/trade "
                                 f"({m15_oos.get('trades', 0)} trades)"
                                 + (f"; M1 October {_f(m1_hold.get('expectancy_r'))} R/trade ({m1_hold.get('trades')} trades)"
                                    if m1_hold and m1_hold.get("trades") else "; M1: no data")},
        "execution": {"pass": (oos["expectancy_r_costs_x2"] or 0) > 0,
                      "detail": f"spread + slippage x2: {_f(oos['expectancy_r_costs_x2'])} R/trade; "
                                f"x3: {_f(oos['expectancy_r_costs_x3'])} R/trade"},
    }
    flags = []
    if m1_hold and m1_hold.get("trades") and hold.get("expectancy_r") is not None and \
            (m1_hold.get("expectancy_r") or 0) * (hold.get("expectancy_r") or 0) < 0:
        flags.append("M1 (October) has the opposite sign to M5")
    dev_e, dev_n = dev.get("expectancy_r"), dev.get("trades", 0)
    if dev_e is not None and dev_n >= MIN_TRADES and dev_e >= OVERFIT_DEV_R and (e is None or e <= 0):
        cls, why = "overfit", f"development {dev_e:+.3f} R/trade on {dev_n} trades, but OOS {_f(e)} R/trade"
    elif e is None or e <= 0:
        cls, why = "rejected", f"OOS expectancy {_f(e)} R/trade after costs ({oos['trades']} trades)"
    elif all(c["pass"] for c in crit.values()):
        cls, why = "promising", "passes every pre-declared criterion on unseen months"
    else:
        failed = [k for k, c in crit.items() if not c["pass"]]
        cls, why = "inconclusive", f"positive out of sample, but fails: {', '.join(failed)}"
    return {"class": cls, "why": why, "criteria": crit, "flags": flags, "oos": oos,
            "months": {m: {"trades": by_month[m]["trades"], "sum_r": by_month[m]["sum_r"],
                           "expectancy_r": by_month[m]["expectancy_r"], "profit_factor": by_month[m]["profit_factor"]}
                       for m in months}}


def _f(x: Any, n: int = 3) -> str:
    return "-" if x is None else f"{x:+.{n}f}"


def variant_table(results: dict, params: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """EVERY redesign variant of one strategy (the full distribution, failures included): development and hold-out on
    M5 (each variant is a fixed rule set, never chosen on the hold-out), cost-stressed hold-out, and every month."""
    dev = [w["key"] for w in params["windows"] if w["role"] == "development"]
    secondary = [w["key"] for w in params["windows"] if w["month"] in SECONDARY_MONTHS]
    hold = [w["key"] for w in params["windows"] if w["role"] == "hold-out" and w["key"] not in secondary]
    tf = rb.SELECTION_TF
    rows = []
    for c in [c for c in params["configs"] if c["strategy"] == key and c["role"] == "redesign"]:
        d, h = _metrics(_trades(results, c["key"], tf, dev)), _metrics(_trades(results, c["key"], tf, hold))
        sec = _metrics(_trades(results, c["key"], tf, secondary))
        rows.append({"key": c["key"], "entry": c.get("entry") or "-", "exit": c.get("exit") or "-",
                     "overlay": c.get("overlay") or "none", "label": c.get("label", c["key"]),
                     "development": {k: d[k] for k in ("trades", "expectancy_r", "profit_factor", "win_rate", "max_dd_r")},
                     "hold_out": {k: h[k] for k in ("trades", "expectancy_r", "profit_factor", "win_rate", "max_dd_r",
                                                    "expectancy_r_costs_x2", "expectancy_r_costs_x3")},
                     "secondary_october": {k: sec[k] for k in ("trades", "expectancy_r", "profit_factor")},
                     "months": {w["key"]: rb.summarize(_trades(results, c["key"], tf, [w["key"]]))["sum_r"]
                                for w in params["windows"]}})
    return rows


def group_effect(rows: list[dict[str, Any]], by: str) -> list[dict[str, Any]]:
    """Median development and hold-out expectancy of the variants grouped by entry, exit or overlay."""
    out = []
    for g in sorted({r[by] for r in rows}):
        mine = [r for r in rows if r[by] == g]
        dv = [r["development"]["expectancy_r"] for r in mine if r["development"]["expectancy_r"] is not None
              and r["development"]["trades"] >= 10]
        hv = [r["hold_out"]["expectancy_r"] for r in mine if r["hold_out"]["expectancy_r"] is not None
              and r["hold_out"]["trades"] >= 10]
        out.append({by: g, "variants": len(mine), "label": mine[0]["label"],
                    "development_median_r": round(statistics.median(dv), 4) if dv else None,
                    "development_positive": sum(1 for x in dv if x > 0), "development_n": len(dv),
                    "hold_out_median_r": round(statistics.median(hv), 4) if hv else None,
                    "hold_out_positive": sum(1 for x in hv if x > 0), "hold_out_n": len(hv)})
    return out


def paired_overlays(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each entry x exit of one strategy: plain vs + break-even vs + trailing, same entries and exits otherwise."""
    base = {(r["entry"], r["exit"]): {} for r in rows}
    for r in rows:
        base[(r["entry"], r["exit"])][r["overlay"]] = r
    out = []
    for (ev, xk), v in sorted(base.items()):
        out.append({"entry": ev, "exit": xk, **{ov: {"development_r": (v[ov]["development"]["expectancy_r"] if ov in v else None),
                                                      "hold_out_r": (v[ov]["hold_out"]["expectancy_r"] if ov in v else None),
                                                      "hold_out_trades": (v[ov]["hold_out"]["trades"] if ov in v else None)}
                                                 for ov in ("none", "be", "tr")}})
    return out


def final(results: dict, params: dict[str, Any]) -> dict[str, Any]:
    """The classification of every original and its redesigned version, with strict (development-only) choices."""
    strict = rb.strict_params(params)
    dev = [w["key"] for w in strict["windows"] if w["role"] == "development"]
    secondary = [w["key"] for w in strict["windows"] if w["month"] in SECONDARY_MONTHS]
    hold = [w["key"] for w in strict["windows"] if w["role"] == "hold-out" and w["key"] not in secondary]
    tf = rb.SELECTION_TF
    out: dict[str, Any] = {"definition": DEFINITION, "criteria_doc": __doc__, "development": dev, "hold_out": hold,
                           "secondary": secondary, "folds": strict["folds"], "strategies": {},
                           "oos_months": [fd["test"] for fd in strict["folds"] if fd["test"] not in secondary]}
    for key in dict.fromkeys(c["strategy"] for c in strict["configs"]):
        variants = [c["key"] for c in strict["configs"] if c["strategy"] == key and c["role"] == "redesign"]
        orig = f"{key}|original"
        dev_choice = rb.select(results, variants, dev, allowed=dev, min_trades=rb.MIN_DEV_TRADES)
        fold_choices = {fd["test"]: rb.select(results, variants, fd["select_on"], allowed=fd["select_on"],
                                              min_trades=rb.MIN_TRADES_PER_WINDOW * len(fd["select_on"]))
                        for fd in strict["folds"]}
        oos_months = [fd["test"] for fd in strict["folds"] if fd["test"] not in secondary]
        entries = {}
        picks = {"original": {m: orig for m in oos_months},
                 "redesign": {m: fold_choices[m]["chosen"] for m in oos_months}}
        for label, pick in picks.items():
            per_month = {m: _trades(results, pick[m], tf, [m]) for m in oos_months}
            chosen_dev = orig if label == "original" else dev_choice["chosen"]
            m15 = rb.summarize([t for m in oos_months for t in _trades(results, pick[m], "M15", [m])])
            hold_m = rb.summarize(_trades(results, chosen_dev, tf, hold))
            dev_m = rb.summarize(_trades(results, chosen_dev, tf, dev))
            a = assess(per_month, dev=dev_m, hold=hold_m, m15_oos=m15, m1_hold=None)   # October / M1: secondary only
            a["secondary_october"] = {
                t: {k: v for k, v in _metrics(_trades(results, chosen_dev, t, secondary)).items()
                    if k in ("trades", "expectancy_r", "profit_factor", "net_usd", "win_rate", "expectancy_r_costs_x2")}
                for t in ("M5", "M15", "M1")} if chosen_dev else {}
            a["chosen_per_month"] = dict(pick)
            if label == "redesign" and not dev_choice["chosen"] and not any(pick.values()):
                a["class"], a["why"] = "rejected", (f"no redesign variant reached the minimum development sample "
                                                     f"({dev_choice['flag']}); nothing to evaluate out of sample")
            a["development_choice"] = chosen_dev
            a["development"] = {k: dev_m[k] for k in ("trades", "expectancy_r", "profit_factor", "win_rate", "max_dd_r")}
            a["hold_out"] = {k: hold_m[k] for k in ("trades", "expectancy_r", "profit_factor", "win_rate", "net_usd")}
            a["all_months_m5"] = {w["key"]: {"status": _status(results, chosen_dev, tf, w["key"]),
                                             **{k: rb.summarize(_trades(results, chosen_dev, tf, [w["key"]]))[k]
                                                for k in ("trades", "sum_r", "expectancy_r", "net_usd")}}
                                  for w in strict["windows"]}
            entries[label] = a
        hv = [rb.summarize(_trades(results, k, tf, hold)) for k in variants]
        hv = [m["expectancy_r"] for m in hv if m["expectancy_r"] is not None and m["trades"] >= 10]
        dv = [rb.summarize(_trades(results, k, tf, dev)) for k in variants]
        dv = [m["expectancy_r"] for m in dv if m["expectancy_r"] is not None and m["trades"] >= 30]
        table = variant_table(results, strict, key)
        out["strategies"][key] = {
            "variants": table, "by_entry": group_effect(table, "entry"), "by_exit": group_effect(table, "exit"),
            "by_overlay": group_effect(table, "overlay"), "paired_overlays": paired_overlays(table),
            "family": rb.FAMILY.get(key), "version_name": rb.VERSION_NAMES.get(key, f"{key} v2"),
            "variants_tried": len(variants), "selection_rule": dev_choice["rule"], "selection_flag": dev_choice["flag"],
            "original": entries["original"], "redesign": entries["redesign"],
            "variant_distribution": {
                "development_with_30_trades": len(dv), "development_positive": sum(1 for x in dv if x > 0),
                "development_median_r": round(statistics.median(dv), 4) if dv else None,
                "hold_out_with_10_trades": len(hv), "hold_out_positive": sum(1 for x in hv if x > 0),
                "hold_out_median_r": round(statistics.median(hv), 4) if hv else None,
                "chosen_hold_out_rank": (sorted(hv, reverse=True).index(entries["redesign"]["hold_out"]["expectancy_r"]) + 1
                                         if entries["redesign"]["hold_out"]["expectancy_r"] in hv else None)}}
    out["summary"] = {k: {"original": s["original"]["class"], "redesign": s["redesign"]["class"]}
                      for k, s in out["strategies"].items()}
    return out
