"""OPTIONAL Claude mode of the research assistant (PHASE4_ARCHITECTURE.md section 9.3).

Off by default. It needs GOLDSCALP_ASSISTANT_LLM_ENABLED=true, the official ``anthropic`` package
(requirements-ai.txt, imported lazily here and nowhere else) and a key (GOLDSCALP_ASSISTANT_API_KEY, or the
SDK's own credential resolution). When on, excerpts of STORED GoldScalp data returned by the read-only tools
are sent to Anthropic; the dashboard says so.

Request: Messages API, model claude-opus-5-5, adaptive thinking, effort medium, server-side refusal fallback,
strict JSON-schema tools with tool_choice auto, a manual tool loop (max 8 rounds) that appends the full
assistant content each round and returns every tool result in one user message. The final answer is checked:
numbers that appear in no tool result are listed as unverified."""
from __future__ import annotations

import importlib
import importlib.util
import json
import re
from typing import Any

from goldscalp.assistant import tools

MAX_ROUNDS = 8
FALLBACK_BETA = "server-side-fallback-2026-07-01"
SYSTEM = (
    "You are GoldScalp's research assistant for XAUUSD strategy evaluation. Answer ONLY from the results of the "
    "provided tools, which read stored GoldScalp records (signals, SIMULATED paper trades, evaluations, research "
    "runs, proposals, terminal verification records). Never invent prices, trades, metrics or market observations; if "
    "the tools return no data, say what is missing. Every trade is SIMULATED (paper trading) - say so. Cite record ids "
    "(for example strategy_signals:ls_..., live_trades:12) for the facts you use. Point out small samples: ratio metrics "
    "below the minimum sample are withheld and one trade is not evidence. You cannot change strategies or place orders; "
    "improvements are adopted only when the user approves a validated proposal.")
_NULL_INT = {"type": ["integer", "null"]}
_NULL_NUM = {"type": ["number", "null"]}
_NULL_STR = {"type": ["string", "null"]}


def _tool(name: str, description: str, props: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": props, "required": list(props),
                             "additionalProperties": False}}


TOOL_SCHEMAS = [
    _tool("connection_status", "Market-data provider state, latest quote, broker company and the latest MT5 terminal "
                               "verification record.", {}),
    _tool("latest_signal", "The newest stored strategy signal with its SIMULATED trade, price outcome and evaluation.",
          {"strategy_id": _NULL_INT}),
    _tool("trade_detail", "One SIMULATED live-paper trade with its 7-question evaluation. trade_id null = the most "
                          "recent closed trade (losing_only true = most recent losing trade).",
          {"trade_id": _NULL_INT, "losing_only": {"type": "boolean"}}),
    _tool("strategy_performance", "Live-paper metrics per monitored strategy version over the last N days (null = all).",
          {"days": _NULL_NUM}),
    _tool("false_signals_by_session", "Measured signal outcomes grouped by market session, with false-signal counts.",
          {"days": _NULL_NUM}),
    _tool("backtest_vs_live", "Validating backtest metrics vs live-paper metrics per monitored version, and per-trade "
                              "comparisons with the backtest engine's replica of the same decision.",
          {"strategy_id": _NULL_INT, "version": _NULL_INT}),
    _tool("proposals_summary", "Improvement proposals with their holdout validation and status.",
          {"status": {"type": ["string", "null"],
                      "enum": ["validated", "inconclusive", "not_improved", "approved", "rejected", None]}}),
    _tool("evaluated_versions", "Strategy versions being monitored (mode, status, evaluation counts, validation account).",
          {}),
    _tool("journal_search", "Journal entries filtered by date (ISO), strategy, session, timeframe, direction, outcome.",
          {"start": _NULL_STR, "end": _NULL_STR, "strategy_id": _NULL_INT, "session": _NULL_STR,
           "timeframe": _NULL_STR, "direction": _NULL_STR, "outcome": _NULL_STR}),
]
_SCHEMA_BY_NAME = {t["name"]: t["input_schema"] for t in TOOL_SCHEMAS}


class AssistantUnavailable(RuntimeError):
    pass


def status(settings: Any) -> dict[str, Any]:
    if not settings.assistant_llm_enabled:
        return {"enabled": False, "reason": "Claude mode is off (GOLDSCALP_ASSISTANT_LLM_ENABLED=false): answers are "
                                            "computed locally from stored data", "model": None}
    try:
        found = importlib.util.find_spec("anthropic") is not None
    except (ImportError, ValueError):
        found = False
    if not found:
        return {"enabled": False, "reason": "the 'anthropic' package is not installed (pip install -r "
                                            "requirements-ai.txt)", "model": settings.assistant_model}
    return {"enabled": True, "reason": "Claude mode is on: excerpts of stored GoldScalp data are sent to Anthropic",
            "model": settings.assistant_model}


def make_client(settings: Any) -> Any:
    try:
        anthropic = importlib.import_module("anthropic")
    except ImportError as exc:
        raise AssistantUnavailable("the 'anthropic' package is not installed") from exc
    key = settings.assistant_api_key.get_secret_value() if settings.assistant_api_key else None
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def _check_input(name: str, data: Any) -> dict[str, Any]:
    schema = _SCHEMA_BY_NAME.get(name)
    if schema is None:
        raise ValueError(f"unknown tool {name!r}")
    if not isinstance(data, dict) or set(data) - set(schema["properties"]):
        raise ValueError("invalid tool input")
    out = {}
    for key, spec in schema["properties"].items():
        value = data.get(key)
        types = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
        ok = (value is None and "null" in types) or (isinstance(value, bool) and "boolean" in types) or \
             (isinstance(value, int) and not isinstance(value, bool) and ("integer" in types or "number" in types)) or \
             (isinstance(value, float) and "number" in types) or (isinstance(value, str) and "string" in types)
        if not ok:
            raise ValueError(f"invalid value for {key}")
        out[key] = value
    return out


def run_tool(gs: Any, name: str, data: Any) -> dict[str, Any]:
    args = _check_input(name, data)
    if name == "trade_detail" and args.get("losing_only") is None:
        args["losing_only"] = True
    return tools.TOOLS[name](gs, **args)


def _numbers(text: str) -> set[str]:
    out = set()
    for m in re.findall(r"-?\d+(?:\.\d+)?", text):
        try:
            v = float(m)
        except ValueError:
            continue
        out.add(f"{abs(v):.4f}".rstrip("0").rstrip("."))
        for n in (0, 1, 2, 3):
            out.add(f"{abs(round(v, n)):.{n}f}")
    return out


def unverified_numbers(answer: str, tool_texts: list[str]) -> list[str]:
    known = set()
    for t in tool_texts:
        known |= _numbers(t)
    bad = []
    for m in re.findall(r"-?\d+(?:\.\d+)?", answer):
        v = abs(float(m))
        if v <= 10 and float(v).is_integer():
            continue            # counts, list numbers, question numbers
        if f"{v:.4f}".rstrip("0").rstrip(".") not in known and m.lstrip("-") not in known:
            bad.append(m)
    return sorted(set(bad))


def ask(gs: Any, question: str, *, client: Any = None) -> dict[str, Any]:
    if client is None:
        st = status(gs.settings)
        if not st["enabled"]:
            raise AssistantUnavailable(st["reason"])
        client = make_client(gs.settings)
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    calls: list[dict[str, Any]] = []
    sources: list[str] = []
    texts: list[str] = []
    response = None
    for _ in range(MAX_ROUNDS):
        try:
            response = client.beta.messages.create(
                model=gs.settings.assistant_model, max_tokens=16000, betas=[FALLBACK_BETA], fallbacks="default",
                thinking={"type": "adaptive"}, output_config={"effort": "medium"}, system=SYSTEM,
                tools=TOOL_SCHEMAS, tool_choice={"type": "auto"}, messages=messages)
        except Exception as exc:  # noqa: BLE001 - any API failure falls back to the local answer
            raise AssistantUnavailable(f"the Claude request failed ({type(exc).__name__})") from exc
        if response.stop_reason == "refusal":
            raise AssistantUnavailable("Claude declined this request")
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            try:
                out = run_tool(gs, block.name, block.input)
                payload = json.dumps(out, default=str)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": payload})
                texts.append(payload)
                sources += out["sources"]
                calls.append({"tool": block.name, "input": block.input})
            except Exception as exc:  # noqa: BLE001 - a bad tool call is reported to the model, not raised
                results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                "content": f"tool error: {type(exc).__name__}: {exc}"})
        messages.append({"role": "user", "content": results})
    else:
        raise AssistantUnavailable("Claude did not finish within the tool-round limit")
    answer = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text").strip()
    if response.stop_reason == "max_tokens":
        answer += "\n[answer cut off at the length limit]"
    unverified = unverified_numbers(answer, texts)
    if unverified:
        answer += ("\n\nUnverified numbers (not found in any tool result - treat with caution): " + ", ".join(unverified))
    return {"intent": "claude", "answer": answer or "(no answer)", "tools": calls, "sources": sorted(set(sources)),
            "data": None, "model": gs.settings.assistant_model, "unverified_numbers": unverified}
