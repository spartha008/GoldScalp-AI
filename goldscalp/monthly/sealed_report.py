"""Phase 6 H6.4: verify the sealed data, freeze the registration and code, and report (read-only on the stores).

    python -m goldscalp.monthly.sealed_report --verify               # data checks only (no strategy, no result)
    python -m goldscalp.monthly.sealed_report --freeze "<note>"      # records the checks and the hashes
    python -m goldscalp.monthly.sealed_report --check
    python -m goldscalp.monthly.sealed_report [out_dir]              # H6.4 report of every run so far

The verification reads timestamps and compares candles between timeframes and against the application's own MT5
history; it never runs a strategy and never computes a result."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from goldscalp.core.config import Settings
from goldscalp.core.timeframes import Timeframe
from goldscalp.monthly import discovery as dv
from goldscalp.monthly import runner
from goldscalp.monthly import sealed as sl
from goldscalp.simulation.instrument import InstrumentSpec

ROOT = dv.ROOT
STORE = ROOT / "var" / "monthly" / "monthly.sqlite3"
APP_DB = ROOT / "var" / "goldscalp.sqlite3"
REPORTS = STORE.parent / "reports"
MANIFEST = REPORTS / "h64-protocol-frozen.json"
FILES = ("config/discovery/phase6-h64.json", "PHASE_6_H64_REGISTRATION.md", "goldscalp/monthly/sealed.py",
         "goldscalp/monthly/sealed_report.py", "goldscalp/monthly/discovery.py", "config/discovery/phase6.json")
SEALED_KEY = "csv:vantage-mt5-xauusd-sealed-2023-2025"


def hashes() -> dict[str, str | None]:
    return {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() if (ROOT / f).exists() else None for f in FILES}


def _manifests() -> list[Path]:
    return sorted(REPORTS.glob(MANIFEST.stem + "*.json"), key=lambda q: (len(q.name), q.name))


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _frame(con: sqlite3.Connection, series_key: str, tf: str, start: datetime, end: datetime) -> pd.DataFrame:
    row = con.execute("SELECT id FROM series WHERE key = ?", (series_key,)).fetchone()
    if row is None:
        return pd.DataFrame(columns=["o", "h", "l", "c"])
    rows = con.execute("SELECT ts, o, h, l, c FROM candles WHERE series_id = ? AND tf = ? AND ts >= ? AND ts < ? ORDER BY ts",
                       (row["id"], tf, int(start.timestamp()), int(end.timestamp()))).fetchall()
    f = pd.DataFrame([dict(r) for r in rows], columns=["ts", "o", "h", "l", "c"])
    f.index = pd.to_datetime(f["ts"], unit="s", utc=True)
    return f.rename(columns={"ts": "t"})                          # t: raw stored seconds, for the alignment check


PRICES = ["o", "h", "l", "c"]


def _pair(a: pd.DataFrame, b: pd.DataFrame, suffix: str) -> pd.DataFrame:
    return a[PRICES].merge(b[PRICES], left_index=True, right_index=True, how="inner", suffixes=("", suffix))


def compare(fine: pd.DataFrame, coarse: pd.DataFrame, fine_tf: Timeframe, coarse_tf: Timeframe) -> dict[str, int]:
    """Aggregate complete groups of fine bars and compare with the stored coarse bars (integer prices: exact)."""
    if fine.empty or coarse.empty:
        return {"compared": 0, "mismatched": 0}
    need = coarse_tf.seconds // fine_tf.seconds
    key = fine.index.floor(f"{coarse_tf.seconds}s")
    g = fine.groupby(key)
    agg = pd.DataFrame({"o": g["o"].first(), "h": g["h"].max(), "l": g["l"].min(), "c": g["c"].last(), "n": g["o"].count()})
    agg = agg[agg["n"] == need].drop(columns=["n"])
    both = _pair(agg, coarse, "_s")
    bad = ((both["o"] != both["o_s"]) | (both["h"] != both["h_s"]) | (both["l"] != both["l_s"]) | (both["c"] != both["c_s"]))
    return {"compared": int(len(both)), "mismatched": int(bad.sum())}


def verify(store: Path = STORE, app_db: Path = APP_DB) -> dict[str, Any]:
    spec = sl.load_spec()
    schedule = InstrumentSpec.load(Settings().instrument_spec).schedule
    start, end = dv.SEALED[0] - timedelta(days=20), dv.SEALED_EVAL[1]
    con = _ro(store)
    try:
        frames = {tf: _frame(con, SEALED_KEY, tf, start, end) for tf in ("M5", "M15", "H1")}
        sid = con.execute("SELECT id FROM series WHERE key = ?", (SEALED_KEY,)).fetchone()
        dupes = {tf: (con.execute("SELECT COUNT(*) - COUNT(DISTINCT ts) FROM candles WHERE series_id = ? AND tf = ?",
                                  (sid["id"], tf)).fetchone()[0] if sid else None) for tf in frames}
        imports = [dict(r) for r in con.execute(
            "SELECT tf, file_name, rows_stored, rows_rejected, duplicates_dropped, first_ts, last_ts FROM data_imports "
            "WHERE series_id = ? ORDER BY tf, first_ts", (sid["id"],))] if sid else []
    finally:
        con.close()
    holidays = sl.holiday_windows(spec)
    months: dict[str, dict[str, Any]] = {}
    for w in sl.sealed_windows():
        a, b = runner.parse_iso(w["start"]), runner.parse_iso(w["end"])
        months[w["month"]] = {}
        for tf in ("M5", "M15", "H1"):
            t = Timeframe.parse(tf)
            f = frames[tf]
            present = f.index[(f.index >= pd.Timestamp(a)) & (f.index < pd.Timestamp(b))]
            cov = sl.adjusted_share(schedule.expected_bars(a, b, t.seconds), present, holidays)
            warm_needed = schedule.expected_bars(a - timedelta(days=5), a, t.seconds)
            before = f.index[f.index < pd.Timestamp(a)]
            warm = len(before) > 0 and len(warm_needed) > 0 and before[0] <= warm_needed[0]
            months[w["month"]][tf] = {**cov, "warmup": bool(warm), "sufficient": cov["share"] >= 0.95 and bool(warm)}
    aligned = {tf: int((f["t"].astype("int64") % Timeframe.parse(tf).seconds != 0).sum()) if len(f) else 0
               for tf, f in frames.items()}
    consistency = {"M5_vs_M15": compare(frames["M5"], frames["M15"], Timeframe.M5, Timeframe.M15),
                   "M15_vs_H1": compare(frames["M15"], frames["H1"], Timeframe.M15, Timeframe.H1)}
    app = _ro(app_db)
    try:
        app_h1 = _frame(app, "live:mt5:XAUUSD", "H1", start, end)
    finally:
        app.close()
    both = _pair(frames["H1"], app_h1, "_app")
    tz_mismatch = int(((both["o"] != both["o_app"]) | (both["c"] != both["c_app"])).sum()) if len(both) else None
    sufficient = {tf: sum(1 for m in months.values() if m[tf]["sufficient"]) for tf in ("M5", "M15", "H1")}
    return {"series": SEALED_KEY, "months": months, "sufficient_months": sufficient, "duplicate_timestamps": dupes,
            "misaligned_bars": aligned, "cross_timeframe": consistency,
            "timezone_check_vs_app_mt5_h1": {"compared": int(len(both)), "mismatched": tz_mismatch},
            "imports": imports, "verified_at": datetime.now(UTC).isoformat()}


def results_count(store: Path = STORE) -> int:
    con = _ro(store)
    try:
        has = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sealed_results'").fetchone()
        return 0 if not has else con.execute(
            "SELECT COUNT(*) FROM sealed_results r JOIN sealed_runs s ON s.id = r.run_id WHERE s.purpose = 'sealed'"
        ).fetchone()[0]
    finally:
        con.close()


def freeze(note: str) -> Path:
    existing = _manifests()
    target = MANIFEST if not existing else MANIFEST.with_name(f"{MANIFEST.stem}-v{len(existing) + 1}.json")
    if target.exists():
        raise SystemExit(f"already frozen: {target}")
    if results_count() > 0:
        raise SystemExit("sealed results already exist: H6.4 cannot be (re)frozen after its sealed run")
    spec = sl.load_spec()
    data = verify()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "version": len(existing) + 1, "definition": sl.DEFINITION, "frozen_at": datetime.now(UTC).isoformat(), "note": note,
        "files": hashes(), "sealed_results_at_freeze": 0, "code_version": runner.app_version(),
        "candidate": {"key": spec["candidate"]["key"], "fingerprint": sl.builder.fingerprint(spec["candidate"]["config"])},
        "control": {"key": spec["control"]["key"], "fingerprint": sl.builder.fingerprint(spec["control"]["config"])},
        "windows": sl.sealed_windows(), "criteria": spec["criteria"], "data_verification": data}, indent=1), encoding="utf-8")
    return target


def check() -> dict[str, Any]:
    ms = _manifests()
    if not ms:
        return {"frozen": False, "unchanged": False, "note": "no H6.4 protocol manifest"}
    m = json.loads(ms[-1].read_text(encoding="utf-8"))
    now = hashes()
    changed = [f for f, h in m["files"].items() if now.get(f) != h]
    return {"frozen": True, "unchanged": not changed, "changed_files": changed, "manifest": ms[-1].name,
            "frozen_at": m["frozen_at"], "version": m["version"], "sufficient_months": m["data_verification"]["sufficient_months"]}


def _r(x: Any) -> str:
    return "–" if x is None else f"{x:+.3f}"


def _n(x: Any, d: int = 2) -> str:
    return "–" if x is None else f"{x:.{d}f}"


def write(out_dir: Path = REPORTS, store: Path = STORE) -> dict[str, Path]:
    spec = sl.load_spec()
    con = _ro(store)
    runs = [dict(r) for r in con.execute("SELECT * FROM sealed_runs ORDER BY created_at")]
    con.close()
    fw = check()
    m = json.loads(_manifests()[-1].read_text(encoding="utf-8")) if _manifests() else {}
    h = spec["hypothesis"]
    L = ["# GoldScalp AI: Phase 6 H6.4 report", "",
         "> Historical simulation only: separate hypothetical $1,000 accounts per configuration, timeframe and month; no "
         "orders; Phase 4 untouched. Classes describe historical research candidates, not trading readiness.", "",
         "## 1. Registration", "", f"**{h['id']} {h['title']}.** {h['hypothesis']}", "", f"**Origin (disclosed).** {h['origin']}",
         "", f"**Candidate** `{spec['candidate']['key']}`: {h['entry']} **Control** `{spec['control']['key']}`: "
         f"{spec['control']['role']}.", "", f"**Prediction.** {spec['prediction']['text']}", "",
         "**Protocol:** " + (f"{fw['manifest']} frozen {fw['frozen_at']} ({'unchanged' if fw['unchanged'] else 'CHANGED'}), "
                             f"0 sealed results at freeze." if fw.get("frozen") else "not frozen."), ""]
    if m:
        dvf = m["data_verification"]
        L += ["## 2. Sealed data verification (before any result)", "",
              f"- Months with sufficient data (calendar-aware 95 % rule plus warm-up), of 24: M5 {dvf['sufficient_months']['M5']}, "
              f"M15 {dvf['sufficient_months']['M15']}, H1 {dvf['sufficient_months']['H1']}.",
              f"- Duplicate timestamps: {dvf['duplicate_timestamps']}; misaligned bars: {dvf['misaligned_bars']}.",
              f"- M5 aggregated to M15 vs the M15 export: {dvf['cross_timeframe']['M5_vs_M15']}; M15 to H1 vs the H1 export: "
              f"{dvf['cross_timeframe']['M15_vs_H1']}.",
              f"- Timezone: sealed H1 vs the application's own MT5 H1 history: {dvf['timezone_check_vs_app_mt5_h1']}.",
              f"- December 2024: {spec['coverage_rule']['resolution_december_2024']}", "",
              "| Month | M5 share (plain) | M15 share (plain) | H1 share (plain) | Holiday bars excused (M5/M15/H1) |",
              "|---|---:|---:|---:|---:|"]
        for mo, v in dvf["months"].items():
            L.append(f"| {mo} | {v['M5']['share']:.1%} ({v['M5']['plain_share']:.1%}) | {v['M15']['share']:.1%} "
                     f"({v['M15']['plain_share']:.1%}) | {v['H1']['share']:.1%} ({v['H1']['plain_share']:.1%}) | "
                     f"{v['M5']['holiday_missing']}/{v['M15']['holiday_missing']}/{v['H1']['holiday_missing']} |")
        L.append("")
    for r in runs:
        p = json.loads(r["params"])
        a = json.loads(r["analysis"]) if r["analysis"] else None
        L += [f"## {'3' if r['purpose'] == 'development' else '4'}. {r['purpose'].title()} run `{r['id']}` "
              f"(attempt {p['attempt']}, {r['status']})", ""]
        if r["purpose"] == "development":
            L += ["Debugging run on January–13 November 2026. Disclosed; it did not decide whether the sealed test ran.", ""]
        if not a:
            L += [f"No analysis{': ' + r['error'] if r['error'] else ''}.", ""]
            continue
        L += ["| Configuration | TF | Months valid | Trades | R/trade | PF | Sum R | Max DD R | ×2 | ×3 | Half 1 R | Half 2 R |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for k, per_tf in a["configs"].items():
            for tf, s in per_tf.items():
                q = s["pooled"]
                L.append(f"| `{k}` | {tf} | {len(s['valid'])} | {q['trades']} | {_r(q['expectancy_r'])} | {_n(q['profit_factor'])} | "
                         f"{_n(q['sum_r'])} | {_n(q['max_dd_r'])} | {_r(q['expectancy_r_costs_x2'])} | "
                         f"{_r(q['expectancy_r_costs_x3'])} | {_n(s['halves'].get('1', s['halves'].get(1, {})).get('sum_r'))} | "
                         f"{_n(s['halves'].get('2', s['halves'].get(2, {})).get('sum_r'))} |")
        first_tf = next(iter(next(iter(a["configs"].values())).values()))
        L += ["", "Month by month (M5, sum of R and trades):", "",
              "| Configuration | " + " | ".join(first_tf["months"]) + " |", "|---|" + "---:|" * len(first_tf["months"])]
        for k, per_tf in a["configs"].items():
            mm = per_tf["M5"]["months"]
            L.append(f"| `{k}` | " + " | ".join(f"{_n(v['sum_r'], 1)} ({v['trades']})" if v.get("status") == "COMPLETED"
                                                  else v.get("status", "-") for v in mm.values()) + " |")
        L += ["", "By M15 volatility regime (M5; trades, R/trade):", ""]
        for k, per_tf in a["configs"].items():
            L.append(f"- `{k}`: " + "; ".join(f"{g} {s['trades']}, {_r(s['expectancy_r'])}" for g, s in per_tf["M5"]["by_vol_m15"].items()))
        if a.get("assessment"):
            ass = a["assessment"]
            L += ["", f"### Verdict: H6.4 is **{ass['class']}**" + (f" (fails: {', '.join(ass['failed'])})" if ass["failed"] else ""),
                  "", "| Criterion | Result | Detail |", "|---|---|---|"]
            for c, v in ass["criteria"].items():
                L.append(f"| {c} | {'pass' if v['pass'] else '**fail**'} | {v['detail']} |")
            L += ["", f"Control (comparison only, never selected): {a['control_assessment']['class']}. {a['control_note']}"]
        L.append("")
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"markdown": out_dir / "h64-report.md", "json": out_dir / "h64-report.json"}
    paths["markdown"].write_text("\n".join(L) + "\n", encoding="utf-8")
    paths["json"].write_text(json.dumps({"protocol": fw, "manifest": m, "runs": [
        {**{k: r[k] for k in ("id", "purpose", "status", "created_at", "finished_at", "error")},
         "params": json.loads(r["params"]), "analysis": json.loads(r["analysis"]) if r["analysis"] else None} for r in runs]},
        indent=1, default=str), encoding="utf-8")
    return paths


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--verify":
        v = verify()
        print(json.dumps({k: v[k] for k in ("sufficient_months", "duplicate_timestamps", "misaligned_bars", "cross_timeframe",
                                             "timezone_check_vs_app_mt5_h1")}, indent=1))
        print(json.dumps({mo: {tf: [x["share"], x["plain_share"], x["holiday_missing"], x["warmup"]] for tf, x in v.items()}
                          for mo, v in v["months"].items()}))
    elif arg == "--freeze":
        print(freeze(" ".join(sys.argv[2:]) or "H6.4 frozen before any sealed result"))
    elif arg == "--check":
        print(json.dumps(check(), indent=1))
    else:
        print({k: str(v) for k, v in write(Path(arg) if arg else REPORTS).items()})
