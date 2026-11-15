"""Exports: CSV trade reports, CSV performance summaries, JSON strategy configurations, PDF research
reports and the CSV strategy journal. Every report states its dataset, date range, assumptions and
limitations (PHASE3_ARCHITECTURE.md section 11)."""
from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from goldscalp.core.clock import iso
from goldscalp.reports.pdf import PDF, Layout
from goldscalp.strategies.builder import SCHEMA

TRADE_COLUMNS = ["strategy", "fold", "id", "side", "setup_cutoff", "entry_time", "entry_price", "lots", "ounces", "stop",
                 "stop_source", "target", "exit_time", "exit_price", "exit_reason", "ambiguous_bar", "gross_pnl",
                 "commission", "spread_cost", "slippage_cost", "net_pnl", "r_multiple", "mfe", "mae", "mfe_r", "mae_r",
                 "holding_minutes", "session", "volatility", "timeframe"]
SUMMARY_KEYS = ["trades", "wins", "losses", "breakeven", "win_rate", "win_rate_ci95", "gross_profit", "gross_loss", "net",
                "profit_factor", "average_outcome", "expectancy_r", "average_r", "max_drawdown", "max_drawdown_pct",
                "max_drawdown_marked", "mfe_avg", "mae_avg", "mfe_r_avg", "mae_r_avg", "mfe_max", "mae_max",
                "average_holding_minutes", "total_commission", "total_spread_cost", "total_slippage_cost",
                "ambiguous_bar_trades", "sufficient", "reason"]
COLOURS = [(0.72, 0.55, 0.1), (0.2, 0.5, 0.8), (0.55, 0.35, 0.75), (0.2, 0.6, 0.45)]


def _metadata(run: dict[str, Any]) -> list[tuple[str, str]]:
    r = run["result"]
    p = run["params"]
    rows = [("report", f"GoldScalp AI {run['kind'].replace('_', ' ')} {run['id']}" + (f" ({run['name']})" if run.get("name") else "")),
            ("generated", iso(datetime.now().astimezone())),
            ("dataset", f"{r.get('series', run['series_key'])} (base {r.get('base_tf')}, candle hash "
                        f"{str(r.get('dataset_hash', ''))[:16]})"),
            ("date range (UTC)", f"{r['range']['start']} to {r['range']['end']}"),
            ("analysis config", str(r.get("analysis_config") or run.get("config_fingerprint"))),
            ("assumptions", "; ".join(f"{k}={v}" for k, v in (r.get("assumptions") or p.get("assumptions") or {}).items())),
            ("strategies", "; ".join(f"{s['name']} v{s.get('version', '?')} [{str(s.get('fingerprint', ''))[:12]}]"
                                     for s in _strategies(run)))]
    for i, lim in enumerate(r.get("limitations", []), 1):
        rows.append((f"limitation {i}", lim))
    rows.append(("disclaimer", r.get("disclaimer", "")))
    return rows


def _strategies(run: dict[str, Any]) -> list[dict[str, Any]]:
    r = run["result"]
    if run["kind"] == "walk_forward":
        return r.get("strategies_meta", [{"name": n} for n in r.get("candidates", [])])
    return [s["strategy"] for s in r.get("strategies", [])]


def _comment_block(run: dict[str, Any]) -> str:
    return "".join(f"# {k}: {v}\n" for k, v in _metadata(run))


def trades_csv(run: dict[str, Any]) -> str:
    buf = io.StringIO()
    buf.write(_comment_block(run))
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(TRADE_COLUMNS)
    r = run["result"]
    rows: list[tuple[str, dict[str, Any]]] = []
    if run["kind"] == "walk_forward":
        rows = [(t.get("strategy", ""), t) for t in r.get("out_of_sample_trades", [])]
    else:
        for s in r.get("strategies", []):
            rows += [(s["strategy"]["name"], t) for t in s.get("trades", [])]
    for name, t in rows:
        w.writerow([name if c == "strategy" else t.get(c, "") for c in TRADE_COLUMNS])
    return buf.getvalue()


def summary_csv(run: dict[str, Any]) -> str:
    buf = io.StringIO()
    buf.write(_comment_block(run))
    w = csv.writer(buf, lineterminator="\n")
    r = run["result"]
    if run["kind"] == "walk_forward":
        w.writerow(["section", "fold", "dev_start", "dev_end", "eval_start", "eval_end", "selected", "trades", "net",
                    "win_rate", "profit_factor", "expectancy_r", "sufficient", "note"])
        for f in r["folds"]:
            e = f.get("evaluation") or {}
            w.writerow(["fold", f["fold"], f["dev_start"], f["dev_end"], f["eval_start"], f["eval_end"], f.get("selected"),
                        e.get("trades"), e.get("net"), e.get("win_rate"), e.get("profit_factor"), e.get("expectancy_r"),
                        e.get("sufficient"), f.get("note", "")])
        w.writerow([])
        w.writerow(["metric", "out_of_sample"])
        for k in SUMMARY_KEYS:
            w.writerow([k, r["out_of_sample"].get(k)])
        return buf.getvalue()
    strategies = r.get("strategies", [])
    w.writerow(["metric"] + [s["strategy"]["name"] for s in strategies])
    for k in SUMMARY_KEYS:
        w.writerow([k] + [s["metrics"]["summary"].get(k) for s in strategies])
    for section in ("by_session", "by_volatility", "by_timeframe", "by_side"):
        w.writerow([])
        w.writerow([section, "group", "trades", "wins", "losses", "net", "win_rate", "profit_factor", "expectancy_r",
                    "sufficient"])
        for s in strategies:
            for g in s["metrics"][section]:
                w.writerow([s["strategy"]["name"], g["group"], g["trades"], g["wins"], g["losses"], g["net"],
                            g["win_rate"], g["profit_factor"], g["expectancy_r"], g["sufficient"]])
    return buf.getvalue()


def strategy_json(record: dict[str, Any]) -> dict[str, Any]:
    return {"schema": SCHEMA, "exported_at": iso(datetime.now().astimezone()), "strategy_id": record["id"],
            "version": record["version"], "fingerprint": record["fingerprint"], "note": record.get("note"),
            "config": record["config"],
            "disclaimer": "A research configuration of conditions on Phase 2 observations. Not a claim of "
                          "profitability and not a trading instruction."}


def journal_csv(entries: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    buf.write("# GoldScalp AI strategy journal: SIMULATED paper positions linked to strategy setups (no broker orders)\n")
    buf.write(f"# generated: {iso(datetime.now().astimezone())}\n")
    w = csv.writer(buf, lineterminator="\n")
    cols = ["id", "strategy_name", "strategy_version", "setup_id", "signal_id", "signal_time", "position_id", "side",
            "lots", "entry_time", "entry_price", "planned_stop", "planned_target", "planned_max_holding_minutes",
            "status", "exit_time", "exit_price", "close_reason", "invalidation_reason", "gross_pnl", "net_pnl"]
    w.writerow(cols)
    for e in entries:
        p = e["planned"]
        w.writerow([e["id"], e["strategy_name"], e["strategy_version"], e["setup_id"], e["signal_id"], e["signal_time"],
                    e["position_id"], e["side"], e["lots"], e["entry_time"], e["entry_price"], p.get("stop"),
                    p.get("target"), p.get("max_holding_minutes"), e["status"], e["exit_time"], e["exit_price"],
                    e["close_reason"], e["invalidation_reason"], e["gross_pnl"], e["net_pnl"]])
    return buf.getvalue()


def _fmt(v: Any, digits: int = 2) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.{digits}f}"
    return str(v)


def report_pdf(run: dict[str, Any]) -> bytes:
    r = run["result"]
    pdf = PDF(title=f"GoldScalp AI research report {run['id']}")
    lay = Layout(pdf)
    lay.heading(f"Historical research report: {run['kind'].replace('_', ' ')}", 16)
    lay.paragraph(r.get("disclaimer", ""))
    lay.heading("Dataset, range and assumptions", 12)
    lay.kv([(k, v) for k, v in _metadata(run) if not k.startswith("limitation") and k != "disclaimer"])
    if run["kind"] == "walk_forward":
        o = r["out_of_sample"]
        lay.heading("Walk-forward: out-of-sample results", 12)
        lay.kv([("mode / objective", f"{r['mode']} / {r['objective']}"), ("folds", r["variation"]["folds"]),
                ("positive folds", f"{r['variation']['positive_folds']} of {r['variation']['traded_folds']} traded"),
                ("out-of-sample trades", o["trades"]), ("out-of-sample net (USD)", _fmt(o["net"])),
                ("win rate", _fmt(o.get("win_rate"), 3)), ("profit factor", _fmt(o.get("profit_factor"), 3)),
                ("expectancy (R)", _fmt(o.get("expectancy_r"), 3)), ("max drawdown (USD)", _fmt(o["max_drawdown"])),
                ("sample", o.get("reason") or "sufficient")])
        lay.table(["fold", "evaluation window", "selected", "trades", "net", "PF"],
                  [[f["fold"], f"{f['eval_start'][:16]} - {f['eval_end'][:16]}", f.get("selected") or "none",
                    (f.get("evaluation") or {}).get("trades"), _fmt((f.get("evaluation") or {}).get("net")),
                    _fmt((f.get("evaluation") or {}).get("profit_factor"), 2)] for f in r["folds"]],
                  [30, 190, 150, 40, 50, 40])
        lay.chart("Out-of-sample equity (USD)", [("out of sample", [p["equity"] for p in r["out_of_sample_equity"]],
                                                   COLOURS[0])])
        lay.chart("Out-of-sample drawdown (USD)", [("drawdown", [p["drawdown"] for p in r["out_of_sample_drawdown"]],
                                                     (0.8, 0.2, 0.2))], height=90)
    else:
        strategies = r.get("strategies", [])
        lay.heading("Metrics", 12)
        names = [s["strategy"]["name"][:22] for s in strategies]
        width = min(110.0, (499.0 - 130) / max(1, len(names)))
        lay.table(["metric"] + names,
                  [[k] + [_fmt(s["metrics"]["summary"].get(k), 3) for s in strategies] for k in SUMMARY_KEYS
                   if k not in ("win_rate_ci95",)], [130] + [width] * len(names))
        lay.chart("Equity (marked to market at each step, USD)",
                  [(s["strategy"]["name"], [p["equity"] for p in s["marked_equity"]], COLOURS[i % 4])
                   for i, s in enumerate(strategies)])
        lay.chart("Drawdown (USD)", [(s["strategy"]["name"], [p["drawdown"] for p in s["metrics"]["drawdown"]],
                                      COLOURS[i % 4]) for i, s in enumerate(strategies)], height=100)
        for s in strategies:
            lay.heading(f"{s['strategy']['name']}: conditions and breakdowns", 11)
            for line in s.get("conditions_text", []):
                lay.paragraph(f"- {line}", 8)
            for section in ("by_session", "by_volatility"):
                lay.table([section.replace("_", " "), "trades", "wins", "net", "win rate", "PF"],
                          [[g["group"], g["trades"], g["wins"], _fmt(g["net"]), _fmt(g["win_rate"], 3),
                            _fmt(g["profit_factor"], 2)] for g in s["metrics"][section]], [170, 50, 50, 70, 60, 50])
    lay.heading("Limitations", 12)
    for lim in r.get("limitations", []):
        lay.paragraph(f"- {lim}")
    lay.paragraph("Hypothetical results do not predict future performance. No orders were placed; GoldScalp AI has "
                  "no broker execution.")
    return pdf.output()
