"""The research assistant (PHASE4_ARCHITECTURE.md section 9).

Deterministic mode (default): an intent router maps a question to read-only tools and fills a template with
the returned data only. With no stored data it says so. Claude mode (optional, explicit settings): the same
read-only tools via the Messages API (llm.py); answers keep their sources and a numbers check.
Every question is logged (assistant_log) with the tools used and the records cited."""
from __future__ import annotations

import re
from typing import Any

from goldscalp.assistant import llm, tools
from goldscalp.storage import live_repo

QUESTIONS = [
    "What happened to the latest signal?",
    "Why did this simulated trade fail?",
    "Which strategy performed best this week?",
    "Which session produced the most false signals?",
    "Why did backtest performance differ from live-paper performance?",
    "What improvements are being proposed?",
    "Which strategy version is currently being evaluated?",
    "Is the Vantage MT5 terminal connected and verified?",
]
INTENTS = [
    ("trade_failed", re.compile(r"why .*(trade|position).*(fail|lose|lost|loss|stopped)|why .*(los(s|ing)|fail)", re.I)),
    ("latest_signal", re.compile(r"(latest|last|most recent|newest) signal|happened to .*signal", re.I)),
    ("backtest_vs_live", re.compile(r"backtest.*live|live.*backtest|histor.*live", re.I)),
    ("best_strategy", re.compile(r"(strategy|version).*(best|top|most profitable)|best.*(strategy|version)|perform", re.I)),
    ("false_signals_session", re.compile(r"false signal|session.*(false|worst|wrong)", re.I)),
    ("proposals", re.compile(r"improv|propos|recommend", re.I)),
    ("evaluated_version", re.compile(r"version|being evaluated|monitor|currently (evaluat|test|run)", re.I)),
    ("connection", re.compile(r"connect|vantage|terminal|mt5|verif|demo", re.I)),
]
NO_DATA = "No stored data answers this yet"


def route(question: str) -> str | None:
    for name, rx in INTENTS:
        if rx.search(question):
            return name
    return None


def _num(x: Any, n: int = 2) -> str:
    if x is None:
        return "n/a"
    try:
        return f"{float(x):+.{n}f}"
    except (TypeError, ValueError):
        return str(x)


def _trade_id(question: str) -> int | None:
    m = re.search(r"(?:trade|#)\s*#?(\d{1,9})", question, re.I)
    return int(m.group(1)) if m else None


def deterministic(gs: Any, question: str) -> dict[str, Any]:
    intent = route(question)
    if intent is None:
        return {"intent": None, "answer": "I answer questions from stored GoldScalp records. Try one of: "
                                          + " | ".join(QUESTIONS), "data": None, "sources": [], "tools": []}
    if intent == "latest_signal":
        r = tools.latest_signal(gs)
        d = r["data"]
        if not d:
            text = f"{NO_DATA}: no strategy signal is stored. Signals appear once a monitored strategy version fires at a candle close while market data is LIVE."
        else:
            s, t, ev = d["signal"], d["trade"], d["evaluation"]
            text = (f"Latest signal {s['id']}: {s['strategy']['name']} v{s['strategy']['version']} {s['direction'].upper()} on "
                    f"{s['timeframe']} at {s['cutoff']} (bid {s['price']['bid']} / ask {s['price']['ask']}, spread "
                    f"{s['price']['spread']}, source {s['source'].get('provider_name')}). Status: {s['status']}"
                    + (f" - {s['status_reason']}" if s.get("status_reason") else "") + ".")
            if t:
                if t["status"] == "open":
                    text += (f" SIMULATED trade {t['id']} is open since {t['open_time']} at {t['open_price']} "
                             f"(unrealized {t.get('unrealized_pnl')} USD; stop {t['stop_loss']}, target {t['take_profit']}).")
                else:
                    text += (f" SIMULATED trade {t['id']} closed at {t['close_time']} by {t['close_reason']}: net "
                             f"{t['realized_net']} USD (MFE {t['mfe']}, MAE {t['mae']}).")
            oc = s.get("outcome")
            if oc and oc.get("status") == "complete":
                text += (f" Price afterwards: {oc['verdict']}; MFE {oc['mfe']} / MAE {oc['mae']} over "
                         f"{oc['horizon_minutes']} minutes.")
            elif not t:
                text += " The follow-up price outcome is not measured yet (it needs the holding horizon to pass)."
            if ev:
                text += f" Evaluation: {ev['summary']}."
        return {"intent": intent, "answer": text, **_rest(r, ["latest_signal"])}
    if intent == "trade_failed":
        r = tools.trade_detail(gs, _trade_id(question), losing_only=True)
        d = r["data"]
        if not d:
            text = f"{NO_DATA}: no closed losing SIMULATED trade is stored."
        elif not d["evaluation"]:
            t = d["trade"]
            text = (f"Trade {t['id']} ({t['strategy_name']} v{t['version']}) closed by {t['close_reason']} with net "
                    f"{t['realized_net']} USD, but no evaluation is stored for it yet.")
        else:
            t, ev = d["trade"], d["evaluation"]
            lines = [f"Trade {t['id']} ({t['strategy_name']} v{t['version']}, {t['side']} {t['lots']} lots): {ev['summary']}."]
            problem = {2: ("no", "partly"), 3: ("no", "partly"), 4: ("yes",), 5: ("yes", "partly"), 6: ("no", "partly")}
            for a in ev["answers"]:
                if a["verdict"] in problem.get(a["n"], ()):
                    lines.append(f"- {a['question']} {a['answer']}")
            if ev["went_wrong"]:
                lines.append("What went wrong: " + "; ".join(ev["went_wrong"]) + ".")
            hint = next((a for a in ev["answers"] if a["n"] == 7), None)
            if hint:
                lines.append("Improvement hypothesis: " + hint["answer"])
            text = "\n".join(lines)
        return {"intent": intent, "answer": text, **_rest(r, ["trade_detail"])}
    if intent == "best_strategy":
        days = 1 if re.search(r"today", question, re.I) else 30 if re.search(r"month", question, re.I) else 7
        r = tools.strategy_performance(gs, days)
        d = r["data"]
        if not d["strategies"]:
            text = f"{NO_DATA}: no strategy version is being monitored."
        elif not d["best_by_net"]:
            text = f"No SIMULATED trade closed in the last {days} day(s), so no strategy can be ranked yet."
        else:
            b = d["best_by_net"]
            text = (f"Over the last {days} day(s), {b['name']} v{b['version']} has the highest live-paper net: "
                    f"{_num(b['net'])} USD from {b['trades']} SIMULATED trade(s)")
            text += (f", win rate {b['win_rate']}, profit factor {b['profit_factor']}." if b["sufficient"] else
                     f". {b['reason']}: with so few trades this ranking is not evidence of a better strategy.")
            others = [s for s in d["strategies"] if s is not b]
            if others:
                text += " Others: " + "; ".join(f"{s['name']} v{s['version']} {_num(s['net'])} USD ({s['trades']} trades)"
                                                for s in others) + "."
        return {"intent": intent, "answer": text, **_rest(r, ["strategy_performance"])}
    if intent == "false_signals_session":
        r = tools.false_signals_by_session(gs)
        d = r["data"]
        rows = [g for g in d["sessions"] if g["measured_signals"]]
        if not rows:
            text = (f"{NO_DATA}: no signal outcome has been measured yet ({d['pending_outcome']} signal(s) waiting for "
                    "their horizon to pass).")
        else:
            top = rows[0]
            text = (f"{top['session']} produced the most false signals: {top['false_signals']} of {top['measured_signals']} "
                    f"measured signals (rate {top['rate']}). " + "; ".join(
                        f"{g['session']}: {g['false_signals']}/{g['measured_signals']}" for g in rows[1:]) +
                    f". Definition: {d['definition']}.")
        return {"intent": intent, "answer": text, **_rest(r, ["false_signals_by_session"])}
    if intent == "backtest_vs_live":
        r = tools.backtest_vs_live(gs)
        d = r["data"]
        if not d["strategies"]:
            text = f"{NO_DATA}: no strategy version is being monitored, so there is no live-paper result to compare."
        else:
            lines = []
            for s in d["strategies"]:
                b, lv = s["backtest"] or {}, s["live"]
                lines.append(f"{s['name']} v{s['version']}: backtest ({b.get('kind', 'none')} {b.get('id', '')}) "
                             f"{b.get('trades', 'n/a')} trades, net {_num(b.get('net'))}, expectancy {b.get('expectancy_r')} R; "
                             f"live-paper {lv['trades']} trades, net {_num(lv['net'])}"
                             + ("" if lv["sufficient"] else f" ({lv['reason']})") + ".")
                if s["replica_comparisons"]:
                    lines.append(f"  Same decisions replayed by the backtest engine: same exit in "
                                 f"{s['same_exit_rate']:.0%} of {len(s['replica_comparisons'])} trades; mean live-minus-"
                                 f"backtest entry difference {s['mean_entry_diff']}.")
            lines.append("Structural reasons results differ: " + " ".join(d["structural_differences"]))
            text = "\n".join(lines)
        return {"intent": intent, "answer": text, **_rest(r, ["backtest_vs_live"])}
    if intent == "proposals":
        r = tools.proposals_summary(gs)
        d = r["data"]
        if not d:
            text = f"{NO_DATA}: no improvement study has produced a proposal. Start a study in the Improvement tab."
        else:
            lines = [f"{len(d)} proposal(s); none is adopted without your approval:"]
            for p in d[:8]:
                lines.append(f"- [{p['status']}] {p['strategy_name']} v{p['base_version']}: {p['title']} - {p['diff']}. "
                             f"Holdout: {p['holdout']['why']}")
            text = "\n".join(lines)
        return {"intent": intent, "answer": text, **_rest(r, ["proposals_summary"])}
    if intent == "evaluated_version":
        r = tools.evaluated_versions(gs)
        d = [x for x in r["data"] if x["status"] != "stopped"]
        if not d:
            text = f"{NO_DATA}: no strategy version is being monitored."
        else:
            text = "Currently evaluated: " + "; ".join(
                f"{x['name']} v{x['version']} ({x['mode']}, {x['status']}, {x['step_timeframe']} cut-offs since "
                f"{x['started_at']}; {sum(x['evaluations'].values())} cut-offs evaluated, "
                f"{x['evaluations'].get('setup', 0)} signal(s))" for x in d) + "."
        return {"intent": intent, "answer": text, **_rest(r, ["evaluated_versions"])}
    r = tools.connection_status(gs)
    d = r["data"]
    v = d["latest_verification"]
    text = (f"Market data: {d['provider']} is {d['state']} ({d['reason']}). Broker reported by the terminal: "
            f"{d['broker'] or 'unknown'}. ")
    text += ("No terminal verification has been run yet." if v is None else
             f"Latest verification {v['id']} at {v['started_at']}: {v['overall']} (broker {v['broker']}, environment "
             f"{v['environment']}, symbol {v['symbol']}, adapter {v['adapter']}"
             + ("" if v["actual_terminal"] else " - NOT an actual-terminal run") + ").")
    return {"intent": "connection", "answer": text, **_rest(r, ["connection_status"])}


def _rest(r: dict[str, Any], names: list[str]) -> dict[str, Any]:
    return {"data": r["data"], "sources": r["sources"], "as_of": r["as_of"], "tools": names}


def ask(gs: Any, question: str, *, mode: str = "auto", client: Any = None) -> dict[str, Any]:
    question = question.strip()[:1000]
    status = llm.status(gs.settings)
    out: dict[str, Any]
    if mode != "local" and (status["enabled"] or client is not None):
        try:
            out = {"mode": "claude", **llm.ask(gs, question, client=client)}
        except llm.AssistantUnavailable as exc:
            out = {"mode": "deterministic", **deterministic(gs, question), "note": f"Claude mode unavailable: {exc}"}
    else:
        out = {"mode": "deterministic", **deterministic(gs, question)}
    out["question"] = question
    with gs.db.transaction() as con:
        out["log_id"] = live_repo.insert_assistant_log(con, now=gs.clock.now(), question=question, mode=out["mode"],
                                                       intent=out.get("intent"), answer=out["answer"],
                                                       tools=out.get("tools") or [], sources=out.get("sources") or [])
    return out
