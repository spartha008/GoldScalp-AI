"""Phase 4 research assistant: every listed question answered from STORED records (with sources), honest
"no data" answers on an empty database, the audit log, and the optional Claude mode with a FAKE client
(tool loop, request shape, refusal fallback, unverified-number guard). No network is used."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from goldscalp.assistant import answer, llm
from goldscalp.core.config import Settings
from tests.test_phase4_live import CUT, lab, mark_validated, monitor, ny_strategy, quote, run_cycle  # noqa: F401


def populate(c):
    """One monitored version, one signal, one SIMULATED trade stopped out and evaluated."""
    rec = ny_strategy(c)
    mark_validated(c.gs, rec)
    monitor(c, rec)
    quote(c, "3878.30", "3878.51", at=CUT + timedelta(seconds=29))
    run_cycle(c, CUT + timedelta(seconds=30))
    c.clock.set(CUT + timedelta(minutes=1, seconds=1))
    quote(c, "3873.00", "3873.21")
    c.gs.engine.on_quote()
    run_cycle(c, CUT + timedelta(minutes=1, seconds=2))
    return rec


def ask(c, q, mode="local"):
    r = c.post("/api/assistant/ask", json={"question": q, "mode": mode})
    assert r.status_code == 200, r.text
    return r.json()


def test_empty_database_answers_say_there_is_no_data(lab):  # noqa: F811
    for q in answer.QUESTIONS[:7]:
        out = ask(lab, q)
        assert out["mode"] == "deterministic" and out["intent"] is not None, q
        assert "No stored data" in out["answer"] or "no " in out["answer"].lower(), (q, out["answer"])
        assert out["sources"] == []
    unknown = ask(lab, "Tell me a joke")
    assert unknown["intent"] is None and "Try one of" in unknown["answer"]


def test_every_listed_question_is_answered_from_stored_records(lab):  # noqa: F811
    rec = populate(lab)
    out = ask(lab, "What happened to the latest signal?")
    assert out["intent"] == "latest_signal" and "NY session long v1 LONG" in out["answer"]
    assert "closed" in out["answer"] and "stop_loss" in out["answer"] and "LOSS" in out["answer"]
    assert any(s.startswith("strategy_signals:ls_") for s in out["sources"])
    assert any(s.startswith("trade_evaluations:") for s in out["sources"])
    fail = ask(lab, "Why did this simulated trade fail?")
    assert fail["intent"] == "trade_failed" and "stopped out" in fail["answer"] and "hypothesis" in fail["answer"].lower()
    best = ask(lab, "Which strategy performed best this week?")
    assert best["intent"] == "best_strategy" and "NY session long v1" in best["answer"]
    assert "not evidence" in best["answer"]                                     # 1 trade: no ranking claim
    fs = ask(lab, "Which session produced the most false signals?")
    assert fs["intent"] == "false_signals_session" and "no signal outcome has been measured yet" in fs["answer"]
    bvl = ask(lab, "Why did backtest performance differ from live-paper performance?")
    assert bvl["intent"] == "backtest_vs_live" and "rr_fixture" in bvl["answer"] and "Structural reasons" in bvl["answer"]
    props = ask(lab, "What improvements are being proposed?")
    assert props["intent"] == "proposals" and "No stored data" in props["answer"]
    ver = ask(lab, "Which strategy version is currently being evaluated?")
    assert ver["intent"] == "evaluated_version" and "NY session long v1 (paper, active, M5" in ver["answer"]
    conn = ask(lab, "Is the Vantage terminal connected?")
    assert conn["intent"] == "connection" and "No terminal verification has been run yet" in conn["answer"]
    log = lab.get("/api/assistant/log").json()["log"]
    assert len(log) == 8 and log[0]["question"] == "Is the Vantage terminal connected?" and log[-1]["sources"]
    assert rec["id"]


# ------------------------------------------------------------------------------------------------ Claude mode (fake)
def block(**kw):
    return SimpleNamespace(**kw)


class FakeMessages:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})   # snapshot: the list keeps growing
        return self.script.pop(0)


def fake_client(script):
    msgs = FakeMessages(script)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def test_claude_mode_is_off_by_default():
    st = llm.status(Settings())
    assert st["enabled"] is False and "off" in st["reason"]


def test_claude_mode_tool_loop_request_shape_and_number_guard(lab):  # noqa: F811
    populate(lab)
    client, msgs = fake_client([
        block(stop_reason="tool_use", content=[block(type="thinking", thinking=""),
                                               block(type="tool_use", id="tu_1", name="latest_signal",
                                                     input={"strategy_id": None})]),
        block(stop_reason="end_turn", content=[block(type="text", text="The latest SIMULATED trade entered at 3878.56 "
                                                                       "and lost 9999.99 USD.")]),
    ])
    lab.app.state.assistant_client = client
    out = ask(lab, "What happened to the latest signal?", mode="auto")
    assert out["mode"] == "claude" and out["unverified_numbers"] == ["9999.99"]
    assert "Unverified numbers" in out["answer"] and any(s.startswith("strategy_signals:") for s in out["sources"])
    first, second = msgs.calls
    assert first["model"] == "claude-opus-5-5" and first["thinking"] == {"type": "adaptive"}
    assert first["fallbacks"] == "default" and first["betas"] == [llm.FALLBACK_BETA]
    assert first["tool_choice"] == {"type": "auto"} and all(t["strict"] for t in first["tools"])
    assert all(t["input_schema"]["additionalProperties"] is False for t in first["tools"])
    results = second["messages"][-1]
    assert results["role"] == "user" and results["content"][0]["tool_use_id"] == "tu_1"
    assert second["messages"][1]["role"] == "assistant"                                  # full content appended


def test_claude_refusal_or_bad_tool_input_falls_back_safely(lab):  # noqa: F811
    client, _ = fake_client([block(stop_reason="refusal", content=[])])
    lab.app.state.assistant_client = client
    out = ask(lab, "Which strategy version is currently being evaluated?", mode="auto")
    assert out["mode"] == "deterministic" and "declined" in out["note"]
    client, msgs = fake_client([
        block(stop_reason="tool_use", content=[block(type="tool_use", id="tu_9", name="journal_search",
                                                     input={"strategy_id": "drop table"})]),
        block(stop_reason="end_turn", content=[block(type="text", text="No data.")]),
    ])
    lab.app.state.assistant_client = client
    out = ask(lab, "Show the journal", mode="auto")
    err = msgs.calls[1]["messages"][-1]["content"][0]
    assert err["is_error"] is True and "invalid value" in err["content"] and out["answer"] == "No data."
    assert ask(lab, "Show the journal", mode="local")["mode"] == "deterministic"            # local mode never calls out
