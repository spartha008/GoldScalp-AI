"""Research library (read-only): every Phase 5 and Phase 6 study normalised into comparable results, plus the written
research reports, so the Monthly Backtest page can compare any of them side by side.

Read-only by construction. The research store is opened through read() only; nothing here writes, runs, changes or
re-scores a study, and the application database is never touched. Results are taken as stored: the frozen report files
(Phase 5.2, 5.3) and the stored analyses (Phase 5, 5.1, 6, H6.4). Classes and verdicts are copied, never recomputed.

Honesty rules:
  - a run that has not COMPLETED contributes no results, so a running sealed test is never shown early;
  - every other study is still listed with its status and error, so incomplete or failed work is not hidden;
  - hypothetical risk scenarios and development-only results carry that label on every result.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from goldscalp.core.config import PROJECT_ROOT

router = APIRouter(prefix="/api/monthly/library")
DEFINITION = "library@1"
REPORTS = PROJECT_ROOT / "var" / "monthly" / "reports"
PRE7 = PROJECT_ROOT / "research" / "pre_phase7"
DOCS_ROOT = PROJECT_ROOT
ROOT_DOCS = ("PRE_PHASE_7_STAGE_2_SCREENING_REPORT.md", "PRE_PHASE_7_EXTERNAL_STRATEGY_RESEARCH_REPORT.md",
             "PHASE_5_IMPLEMENTATION_REPORT.md", "PHASE_5_2_RESEARCH_REPORT.md", "PHASE_5_3_VALIDATION_REPORT.md",
             "PHASE_6_PROTOCOL.md", "PHASE_6_H64_REGISTRATION.md")
METRICS = ("trades", "win_rate", "sum_r", "expectancy_r", "profit_factor", "max_dd_r", "net_usd",
           "expectancy_r_costs_x2", "expectancy_r_costs_x3", "max_dd_pct", "return_pct")
MONTH_KEYS = ("status", "trades", "win_rate", "sum_r", "expectancy_r", "profit_factor", "net_usd", "partial")
BREAKDOWNS = {"by_vol_m15": "M15 volatility", "by_vol_m5": "M5 volatility", "by_session": "Session", "by_h1": "H1 structure"}
MAX_DOC_BYTES = 2_000_000


# ------------------------------------------------------------------ helpers
def _j(text: str | None) -> Any:
    return json.loads(text) if text else None


def _num(v: Any) -> float | int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if isinstance(v, int) else f


def metrics(d: dict[str, Any] | None) -> dict[str, Any]:
    """The comparable metrics of one result (missing ones are None, never invented)."""
    d = d or {}
    out = {k: _num(d.get(k)) for k in METRICS}
    t = out["trades"]
    for k in (2, 3):                                     # monthly cost-stress sums -> per-trade figures
        if out[f"expectancy_r_costs_x{k}"] is None and d.get(f"sum_r_costs_x{k}") is not None and t:
            out[f"expectancy_r_costs_x{k}"] = round(float(d[f"sum_r_costs_x{k}"]) / t, 4)
    return out


def month_row(d: dict[str, Any] | None, partial: bool = False) -> dict[str, Any]:
    d = d or {}
    out = {k: (d.get(k) if k == "status" else _num(d.get(k))) for k in MONTH_KEYS if k != "partial"}
    out["status"] = out["status"] or "COMPLETED"
    if out["sum_r"] is None and out["expectancy_r"] is not None and out["trades"] is not None:
        out["sum_r"] = round(out["expectancy_r"] * out["trades"], 3)
    out["partial"] = bool(partial or d.get("partial"))
    return out


def breakdowns(d: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, title in BREAKDOWNS.items():
        cells = d.get(key)
        if isinstance(cells, dict) and cells:
            out[title] = {b: {"trades": _num(c.get("trades")), "expectancy_r": _num(c.get("expectancy_r")),
                              "sum_r": _num(c.get("sum_r"))} for b, c in cells.items() if isinstance(c, dict)}
    return out


def result(study: dict[str, Any], key: str, label: str, *, strategy: str, timeframe: str | None,
           pooled: dict[str, Any] | None, months: dict[str, dict[str, Any]] | None = None, role: str | None = None,
           cls: str | None = None, why: str | None = None, scope: str | None = None, status: str = "COMPLETED",
           name: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": f"{study['id']}:{key}", "study": study["id"], "phase": study["phase"], "label": label,
            "strategy": strategy, "name": name, "role": role, "timeframe": timeframe, "status": status, "class": cls,
            "why": why, "scope": scope, "flags": list(study.get("flags", [])), "pooled": metrics(pooled),
            "months": dict(sorted((months or {}).items())), **(extra or {})}


def study(sid: str, phase: str, title: str, *, status: str, period: str | None = None, kind: str, summary: str | None = None,
          flags: list[str] | None = None, created_at: str | None = None, finished_at: str | None = None,
          error: str | None = None, source: str | None = None) -> dict[str, Any]:
    return {"id": sid, "phase": phase, "title": title, "status": status, "period": period, "kind": kind,
            "summary": summary, "flags": flags or [], "created_at": created_at, "finished_at": finished_at,
            "error": error, "source": source, "results": []}


def _rows(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.OperationalError:                     # a table an older store does not have yet
        return []


def _span(months: list[str]) -> str | None:
    ms = sorted(m for m in months if m)
    return None if not ms else ms[0] if len(ms) == 1 else f"{ms[0]} – {ms[-1]}"


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ Phase 5 and 5.1: monthly evaluations
def phase5(con: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for ev in _rows(con, "SELECT id, month, status, params, created_at, finished_at, error FROM evaluations ORDER BY created_at"):
        p = _j(ev["params"]) or {}
        hypothetical = bool(p.get("risk_study"))
        s = study(ev["id"], "5.1" if hypothetical else "5",
                  (p.get("label") or "Risk scenario") if hypothetical else f"Fixed-lot evaluation, {ev['month']}",
                  status=ev["status"], period=ev["month"], kind="risk-scenario" if hypothetical else "monthly",
                  flags=["HYPOTHETICAL risk scenario"] if hypothetical else [], created_at=ev["created_at"],
                  finished_at=ev["finished_at"], error=ev["error"], source="research store: evaluations",
                  summary=("Compounded percentage-risk stress test on the frozen versions; not a real-account setting."
                           if hypothetical else "Exact frozen versions A, B, C and candidates R1–R9, one month, fixed lots."))
        if ev["status"] == "COMPLETED":
            latest: dict[tuple[str, str], sqlite3.Row] = {}
            for r in _rows(con, "SELECT strategy_key, kind, timeframe, status, reason, json_extract(config, '$.name') AS name, "
                                "json_extract(result, '$.summary') AS summary FROM runs WHERE evaluation_id = ? "
                                "ORDER BY finished_at", (ev["id"],)):
                latest[(r["strategy_key"], r["timeframe"])] = r
            for (key, tf), r in sorted(latest.items()):
                sm = _j(r["summary"]) or {}
                pooled = {"trades": sm.get("trades"), "win_rate": sm.get("win_rate"), "expectancy_r": sm.get("average_r"),
                          "profit_factor": sm.get("profit_factor"), "net_usd": sm.get("net"),
                          "max_dd_pct": sm.get("max_drawdown_pct"), "return_pct": sm.get("return_pct")}
                if sm.get("average_r") is not None and sm.get("trades") is not None:
                    pooled["sum_r"] = round(sm["average_r"] * sm["trades"], 3)
                done = r["status"] == "COMPLETED"
                s["results"].append(result(
                    s, f"{key}|{tf}", f"{key} · {tf}", strategy=key, timeframe=tf, name=r["name"],
                    role="existing version" if r["kind"] == "existing" else "research candidate",
                    pooled=pooled if done else None, status=r["status"], why=r["reason"],
                    months={ev["month"]: month_row(pooled)} if done else {},
                    scope=f"{ev['month']}, one $1,000 account, {tf} execution"))
        out.append(s)
    return out


# ------------------------------------------------------------------ Phase 5.2: robustness (final verdicts)
def phase52(con: sqlite3.Connection, reports: Path) -> list[dict[str, Any]]:
    out, seen = [], set()
    for f in sorted(reports.glob("rb_*-final-verdicts.json")):
        j = _load(f)
        if not isinstance(j, dict) or "final" not in j:
            continue
        sid, fin = j["study"], j["final"]
        seen.add(sid)
        row = next(iter(_rows(con, "SELECT params, status, created_at, finished_at FROM robustness_studies WHERE id = ?",
                              (sid,))), None)
        windows = (_j(row["params"]) or {}).get("windows", []) if row else []
        wmonth = {w["key"]: w.get("month", w["key"]) for w in windows}
        partial = {w["key"] for w in windows if w.get("start", "")[8:10] not in ("", "01") or w.get("end", "")[8:10] not in ("", "01")}
        oos = ", ".join(wmonth.get(w, w) for w in fin.get("oos_months", []))
        s = study(sid, "5.2", "Robustness research: originals vs redesigned versions", status=row["status"] if row else "COMPLETED",
                  period=_span(list(wmonth.values())), kind="robustness", created_at=row["created_at"] if row else None,
                  finished_at=row["finished_at"] if row else None, source=f"var/monthly/reports/{f.name}",
                  summary=f"Final verdicts ({fin.get('definition')}). Pooled figures are the out-of-sample months ({oos}), "
                          "M5; the monthly series covers every stored month.")
        for key, st in fin.get("strategies", {}).items():
            for role in ("original", "redesign"):
                v = st.get(role)
                if not isinstance(v, dict):
                    continue
                months = {wmonth.get(w, w): month_row(m, w in partial) for w, m in (v.get("all_months_m5") or {}).items()}
                label = f"{key} original" if role == "original" else st.get("version_name") or f"{key} redesign"
                s["results"].append(result(
                    s, f"{key}|{role}", label, strategy=key, timeframe="M5", pooled=v.get("oos"), months=months,
                    role="original (benchmark)" if role == "original" else "redesigned research version",
                    cls=v.get("class"), why=v.get("why"), name=v.get("development_choice"),
                    scope=f"out-of-sample {oos}, M5", extra={"criteria": v.get("criteria"), "family": st.get("family")}))
        out.append(s)
    for row in _rows(con, "SELECT id, status, params, created_at, finished_at, error FROM robustness_studies ORDER BY created_at"):
        if row["id"] in seen:
            continue
        windows = (_j(row["params"]) or {}).get("windows", [])
        out.append(study(row["id"], "5.2", "Robustness study (no final-verdicts report)", status=row["status"],
                         period=_span([w.get("month") for w in windows]), kind="robustness", created_at=row["created_at"],
                         finished_at=row["finished_at"], error=row["error"], source="research store: robustness_studies",
                         summary="Kept as a record. Only the study with a final-verdicts report carries comparable results."))
    return out


# ------------------------------------------------------------------ Phase 5.3: frozen validation
def phase53(reports: Path) -> list[dict[str, Any]]:
    out = []
    for f in sorted(reports.glob("vl_*-validation.json")):
        j = _load(f)
        if not isinstance(j, dict) or "analysis" not in j:
            continue
        a = j["analysis"]
        vmonth = a.get("months", {})
        s = study(j["study"], "5.3", "Frozen pre-June validation", status="COMPLETED", period=_span(list(vmonth.values())),
                  kind="validation", source=f"var/monthly/reports/{f.name}",
                  summary="Four locked candidates and their originals on January–May 2026, run once with frozen criteria.")
        for key, pair in a.get("strategies", {}).items():
            for role in ("candidate", "original"):
                v = pair.get(role)
                if not isinstance(v, dict):
                    continue
                label = f"{key} v2 ({v['key'].split('|')[1]})" if role == "candidate" else f"{key} original"
                common = {"strategy": key, "role": "locked candidate" if role == "candidate" else "original (benchmark)",
                          "cls": v.get("class"), "why": v.get("why"), "name": v.get("key"),
                          "extra": {"criteria": v.get("criteria")}}
                s["results"].append(result(s, f"{v['key']}|M5", f"{label} · M5", timeframe="M5", pooled=v.get("pooled"),
                                           months={vmonth.get(k, k): month_row(m) for k, m in (v.get("months") or {}).items()},
                                           scope="January–May 2026, M5 execution", **common))
                s["results"].append(result(s, f"{v['key']}|M15", f"{label} · M15", timeframe="M15", pooled=v.get("m15"),
                                           months={vmonth.get(k, k): month_row(m) for k, m in (v.get("m15_months") or {}).items()},
                                           scope="January–May 2026, M15 execution (the class covers both timeframes)", **common))
        out.append(s)
    return out


# ------------------------------------------------------------------ Phase 6: discovery trials (development data)
def _titles() -> dict[str, str]:
    spec = _load(PROJECT_ROOT / "config" / "discovery" / "phase6.json") or {}
    hyp = spec.get("hypotheses", [])
    return {h["id"]: h.get("title", h["id"]) for h in hyp if isinstance(h, dict) and "id" in h}


def phase6(con: sqlite3.Connection) -> list[dict[str, Any]]:
    out, titles = [], _titles()
    for t in _rows(con, "SELECT id, hypothesis, kind, status, params, analysis, created_at, finished_at, error "
                        "FROM discovery_trials ORDER BY created_at"):
        a = _j(t["analysis"]) or {}
        pred = a.get("prediction") or {}
        s = study(t["id"], "6", f"{t['hypothesis']} {t['kind']}: {titles.get(t['hypothesis'], '')}".strip(": "),
                  status=t["status"], kind="discovery", flags=["development data"], created_at=t["created_at"],
                  finished_at=t["finished_at"], error=t["error"], source="research store: discovery_trials",
                  summary=" ".join(x for x in (pred.get("detail"), a.get("gate")) if x) or None)
        if t["status"] == "COMPLETED":
            months_all: list[str] = []
            for ck, c in (a.get("configs") or {}).items():
                months = {m: month_row(v) for m, v in (c.get("months") or {}).items()}
                months_all += list(months)
                s["results"].append(result(s, ck, ck, strategy=ck, timeframe="M5", pooled=c.get("pooled"), months=months,
                                           role="baseline", scope="development months, January – 13 November 2026, M5",
                                           extra={"breakdowns": breakdowns(c), "valid_months": c.get("valid_months")}))
            s["period"] = _span(months_all)
        out.append(s)
    return out


# ------------------------------------------------------------------ Phase 6 H6.4: development and sealed runs
def h64(con: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for r in _rows(con, "SELECT id, hypothesis, purpose, status, params, analysis, created_at, finished_at, error "
                        "FROM sealed_runs ORDER BY created_at"):
        sealed = r["purpose"] == "sealed"
        a = _j(r["analysis"]) if r["status"] == "COMPLETED" else None
        p = _j(r["params"]) or {}
        windows = p.get("windows", [])
        s = study(r["id"], "6", f"{r['hypothesis']} {'sealed validation' if sealed else 'development run'}",
                  status=r["status"], period=_span([w.get("month") for w in windows]),
                  kind="sealed" if sealed else "discovery", flags=[] if sealed else ["development data"],
                  created_at=r["created_at"], finished_at=r["finished_at"], error=r["error"],
                  source="research store: sealed_runs",
                  summary=("The single sealed test on November 2023 – October 2025 under the frozen criteria."
                           if sealed else "Disclosed debugging run on development data; it decides nothing."))
        if a is None:
            if r["status"] in ("READY", "RUNNING"):
                s["summary"] = (s["summary"] or "") + " Results appear only after the run completes."
            out.append(s)
            continue
        assessment = a.get("assessment") or {}
        for ck, tfs in (a.get("configs") or {}).items():
            is_control = ck != r["hypothesis"]
            for tf, c in tfs.items():
                months = {m: month_row(v) for m, v in (c.get("months") or {}).items()}
                cls = None if is_control or not sealed else assessment.get("class")
                role = ("control (comparison only, never selected)" if is_control else "registered candidate")
                if not sealed:
                    role += " · development"
                s["results"].append(result(
                    s, f"{ck}|{tf}", f"{ck} · {tf}" + ("" if sealed else " (dev)"), strategy=ck, timeframe=tf,
                    pooled=c.get("pooled"), months=months, role=role, cls=cls,
                    why=None if cls is None else assessment.get("why"),
                    scope=("November 2023 – October 2025" if sealed else "development months") + f", {tf} execution",
                    extra={"breakdowns": breakdowns(c), "halves": {str(k): metrics(v) for k, v in (c.get("halves") or {}).items()},
                           "criteria": assessment.get("criteria") if (sealed and not is_control) else None}))
        out.append(s)
    return out


# ------------------------------------------------------------------ documents
def _doc_files(root: Path | None = None, reports: Path | None = None) -> list[tuple[dict[str, Any], Path]]:
    root, reports = root or DOCS_ROOT, reports or REPORTS
    out = []
    for f in [root / n for n in ROOT_DOCS] + sorted(reports.glob("*.md")):
        if not f.is_file():
            continue
        try:
            head = f.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        m = re.search(r"^#\s+(.+)$", head, re.M)
        st = f.stat()
        rel = str(f.relative_to(root)).replace("\\", "/") if f.is_relative_to(root) else f"reports/{f.name}"
        out.append(({"id": f.name, "title": m.group(1).strip() if m else f.stem, "file": rel, "bytes": st.st_size,
                     "modified": datetime.fromtimestamp(st.st_mtime, UTC).isoformat().replace("+00:00", "Z")}, f))
    return out


def documents(root: Path | None = None, reports: Path | None = None) -> list[dict[str, Any]]:
    return [meta for meta, _ in _doc_files(root, reports)]


# ------------------------------------------------------------------ Pre-Phase 7: external strategy screens
def _months_from_trades(trades: list[dict[str, Any]], partial: set[str]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        groups.setdefault(t["entry"][:7], []).append(t)
    out = {}
    for m, ts in groups.items():
        rs = [t["r"] for t in ts if t.get("r") is not None]
        out[m] = {"status": "COMPLETED", "trades": len(ts), "win_rate": round(sum(1 for t in ts if t["net"] > 0) / len(ts), 4),
                  "sum_r": round(sum(rs), 3), "expectancy_r": round(sum(rs) / len(rs), 4) if rs else None,
                  "profit_factor": None, "net_usd": round(sum(t["net"] for t in ts), 2), "partial": m in partial}
    return out


def _partial_months(start: str, end: str) -> set[str]:
    """Months a window covers only in part (so the charts can mark them)."""
    out = set()
    if start[8:10] != "01":
        out.add(start[:7])
    if end[8:10] != "01":
        out.add(end[:7])
    return out


def prephase7(pre7: Path) -> list[dict[str, Any]]:
    """The pre-Phase 7 screens: stage 1 (strategy builder, bt@1) and stage 2 (rule simulator), screen and context."""
    out = []
    flags = ["development data", "screening only"]
    s1 = _load(pre7 / "screening-results.json")
    if isinstance(s1, dict) and "results" in s1:
        w = s1["window"]
        st = study("p7s1-screen", "P7", "Stage 1: 10-day screen (strategy builder)", status="COMPLETED", period=w["start"][:7],
                   kind="screen", flags=flags, finished_at=s1.get("ran_at"), source="research/pre_phase7/screening-results.json",
                   summary=f"Published concepts the strategy builder could express, {w['start'][:10]} to {w['end'][:10]}. "
                           "A screen is not validation.")
        partial = _partial_months(w["start"], w["end"])
        for key, tfs in s1["results"].items():
            cls = (s1.get("classifications") or {}).get(key) or {}
            for tf, e in tfs.items():
                st["results"].append(result(st, f"{key}|{tf}", f"{key} · {tf} · 10-day screen", strategy=key, timeframe=tf,
                                            pooled=e["stats"], months=_months_from_trades(e["trades"], partial), role="screened rule set",
                                            cls=cls.get("classification") if tf == "M5" else None,
                                            why="; ".join(cls.get("why", [])) or None, scope="10-day screen, bt@1"))
        out.append(st)
    s2 = _load(pre7 / "stage2-results.json")
    ctx = _load(pre7 / "stage2-builder-context.json")
    if isinstance(s2, dict) and "results" in s2:
        titles = {"screen": "Stage 2: 10-day screen (rule simulator)", "context": "Stage 2: 11-week context run"}
        for wname in ("screen", "context"):
            w = s2["windows"][wname]
            st = study(f"p7s2-{wname}", "P7", titles[wname], status="COMPLETED", period=_span([w["start"][:7], w["end"][:7]]),
                       kind="screen", flags=flags, finished_at=s2.get("ran_at"), source="research/pre_phase7/stage2-results.json",
                       summary=w.get("note"))
            partial = _partial_months(w["start"], w["end"])
            for key, both in s2["results"].items():
                e = both.get(wname)
                if not e:
                    continue
                cls = (s2.get("classifications") or {}).get(key) or {}
                st["results"].append(result(
                    st, key, f"{key} · {'10-day screen' if wname == 'screen' else '11 weeks'}", strategy=key,
                    timeframe=e.get("frame", "M5"), pooled=e["stats"],
                    months=_months_from_trades(e["trades"], partial),
                    role="baseline (market drift)" if key.startswith("BASE-") else "screened rule set",
                    cls=cls.get("classification") if wname == "screen" else None,
                    why="; ".join(cls.get("why", [])) or None,
                    scope=f"{w['start'][:10]} to {w['end'][:10]}, rule simulator",
                    extra={"flags": flags + (["nominal R (no stop)"] if e.get("nominal_r") else [])}))
            if wname == "context" and isinstance(ctx, dict):
                for key, e in ctx.get("results", {}).items():
                    st["results"].append(result(st, f"{key}|bt1", f"{key} · 11 weeks (bt@1)", strategy=key, timeframe="M5", pooled=e["stats"],
                                                months=_months_from_trades(e["trades"], partial),
                                                role="stage 1 rule set or archived benchmark", scope="context window, bt@1 engine"))
            out.append(st)
    return out


def build(con: sqlite3.Connection, reports: Path | None = None, pre7: Path | None = None) -> dict[str, Any]:
    reports = reports or REPORTS
    studies = phase5(con) + phase52(con, reports) + phase53(reports) + phase6(con) + h64(con) + prephase7(pre7 or PRE7)
    return {"definition": DEFINITION, "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "studies": studies, "results": sum(len(s["results"]) for s in studies),
            "documents": documents(reports=reports)}


# ------------------------------------------------------------------ API (GET only)
@router.get("")
def library(request: Request) -> dict[str, Any]:
    with request.app.state.monthly.store.read() as con:
        return build(con)


@router.get("/documents/{doc_id}")
def document(doc_id: str) -> dict[str, Any]:
    found = next(((meta, path) for meta, path in _doc_files() if meta["id"] == doc_id), None)   # whitelist only
    if found is None:
        raise HTTPException(404, f"unknown document {doc_id}")
    meta, path = found
    if meta["bytes"] > MAX_DOC_BYTES:
        raise HTTPException(413, f"{doc_id} is too large to show here")
    return {**meta, "text": path.read_text(encoding="utf-8", errors="replace")}
