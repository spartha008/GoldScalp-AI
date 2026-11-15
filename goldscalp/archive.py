"""Research archive for generation 1 (Phases 1-6, closed 15 November 2026).

Archive, never delete. This module only READS the live databases (read-only SQLite connections and the online backup
API) and writes nothing outside ``archive/generation-1`` and the staging folder ``var/archive``. It never stops a monitor,
changes an account or touches MT5: stopping the Phase 4 monitors is done separately through the application's own API.

  python -m goldscalp.archive --record pre-stop     # Phase 4 final state before the monitors are stopped
  python -m goldscalp.archive --record post-stop    # the same record after they are stopped
  python -m goldscalp.archive --build               # one-shot: copy, hash and mark read-only
  python -m goldscalp.archive --check               # re-hash every file against the manifest

The archive holds a consistent snapshot of the application database and the research store, every report, the
documentation, the strategy configurations, a zip of the code and the Phase 4 final-state records. Every file is listed
with its SHA-256 in MANIFEST.json and marked read-only. ``build`` refuses if the archive already exists.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from goldscalp.core.config import PROJECT_ROOT

GENERATION = "generation-1"
ARCHIVE = PROJECT_ROOT / "archive" / GENERATION
STAGING = PROJECT_ROOT / "var" / "archive"
APP_DB = PROJECT_ROOT / "var" / "goldscalp.sqlite3"
STORE = PROJECT_ROOT / "var" / "monthly" / "monthly.sqlite3"
REPORTS = PROJECT_ROOT / "var" / "monthly" / "reports"
CODE = ("goldscalp", "web", "config", "tests", "docs", "pyproject.toml", "constraints.txt", "requirements*.txt",
        "run.py", "research.py")
CODE_EXCLUDE = ("__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache")
PHASE4_TABLES = ("monitored_strategies", "monitor_evaluations", "strategy_signals", "live_trades", "trade_evaluations",
                 "strategy_journal", "demo_links", "demo_trades")
VERDICTS = {"A": ("rejected", "Phase 5.2 out of sample -0.005 R/trade over 157 trades"),
            "B": ("inconclusive", "Phase 5.2 out of sample +0.029 R/trade over only 24 trades: too small a sample"),
            "C": ("rejected", "Phase 5.2 out of sample -0.005 R/trade over 249 trades")}
CONCLUSION = ("Phase 4 did not establish a reliable edge. A and C were rejected; B was inconclusive because of its small "
              "sample. This is not a claim that every strategy was proven ineffective.")


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _rows(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, args)]
    except sqlite3.OperationalError:
        return []


def _json(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _loop_status() -> dict[str, Any] | None:
    """The running monitor loop as the application reports it (GET only, local)."""
    try:
        from goldscalp.core.config import load_settings
        s = load_settings()
        with urllib.request.urlopen(f"http://{s.host}:{s.port}/api/live/monitor", timeout=15) as r:  # noqa: S310
            return json.loads(r.read()).get("monitor")
    except Exception as exc:  # noqa: BLE001 - recorded, never fatal
        return {"unavailable": f"{type(exc).__name__}: {exc}"}


# ------------------------------------------------------------------ Phase 4 final state
def phase4_state(app_db: Path = APP_DB, store: Path = STORE, reports: Path = REPORTS, loop: bool = True) -> dict[str, Any]:
    con = _ro(app_db)
    try:
        monitors = _rows(con, "SELECT * FROM monitored_strategies ORDER BY id")
        for m in monitors:
            m["state"] = _json(m.get("state"))
            m["eligibility"] = _json(m.get("eligibility"))
            v = _rows(con, "SELECT s.name, v.config, v.fingerprint, v.created_at, v.note FROM strategy_versions v "
                           "JOIN strategies s ON s.id = v.strategy_id WHERE v.strategy_id = ? AND v.version = ?",
                      (m["strategy_id"], m["version"]))
            m["strategy"] = {**v[0], "config": _json(v[0]["config"])} if v else None
        positions = _rows(con, "SELECT * FROM paper_positions ORDER BY id")
        accounts = _rows(con, "SELECT * FROM paper_accounts ORDER BY id")
        monitored_accounts = {m["account_id"] for m in monitors if m.get("account_id")}
        for a in accounts:
            led = _rows(con, "SELECT ts, kind, amount, balance_after, position_id, note FROM account_ledger "
                             "WHERE account_id = ? ORDER BY id", (a["id"],))
            snap = _rows(con, "SELECT ts, balance, equity, open_positions FROM equity_snapshots WHERE account_id = ? "
                              "ORDER BY ts DESC LIMIT 1", (a["id"],))
            a["phase4"] = a["id"] in monitored_accounts
            a["ledger"] = led
            a["balance"] = led[-1]["balance_after"] if led else None
            a["latest_snapshot"] = snap[0] if snap else None
            a["open_positions"] = [p for p in positions if p["account_id"] == a["id"] and p["status"] == "open"]
        counts = {t: (con.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0] if _rows(con, f"PRAGMA table_info({t})")
                      else None) for t in PHASE4_TABLES}
        records = {t: _rows(con, f"SELECT * FROM [{t}]") for t in PHASE4_TABLES if counts.get(t)}
        strategies = _rows(con, "SELECT id, name, created_at, updated_at, current_version, archived FROM strategies")
        versions = _rows(con, "SELECT strategy_id, version, fingerprint, created_at, note FROM strategy_versions "
                              "ORDER BY strategy_id, version")
        verifications = _rows(con, "SELECT id, started_at, finished_at, overall, environment, symbol, summary "
                                   "FROM mt5_verifications ORDER BY started_at")
        for v in verifications:
            v["summary"] = _json(v["summary"])
        feed = _rows(con, "SELECT ts, provider, from_state, to_state, reason FROM feed_events ORDER BY id DESC LIMIT 10")
    finally:
        con.close()
    cross = {}
    if store.exists():
        s = _ro(store)
        try:
            for r in _rows(s, "SELECT strategy_key, timeframe, fingerprint FROM runs WHERE evaluation_id = "
                              "'me_23932a71a56a' AND strategy_key IN ('A', 'B', 'C') AND timeframe = 'M5'"):
                cross[r["strategy_key"]] = r["fingerprint"]
        finally:
            s.close()
    keys = {1: "A", 2: "B", 3: "C"}
    research = []
    for m in monitors:
        k = keys.get(m["strategy_id"])
        cls, why = VERDICTS.get(k, (None, None))
        research.append({"monitor": m["id"], "strategy": k, "version": m["version"], "class": cls, "evidence": why,
                         "same_rules_as_phase5_original": cross.get(k) == m["fingerprint"] if k in cross else None})
    return {"generation": GENERATION, "recorded_at": now_iso(), "conclusion": CONCLUSION,
            "loop": _loop_status() if loop else None, "monitors": monitors, "research_verdicts": research,
            "accounts": accounts, "positions": positions, "phase4_counts": counts, "phase4_records": records,
            "strategies": strategies, "strategy_versions": versions, "mt5_verifications": verifications,
            "recent_feed_events": feed,
            "notes": ["The monitors were started on Saturday 14 November 2026 at 11:57 UTC, after the weekly close, so "
                      "no step candle closed in trading hours while they were active: they recorded no evaluation, "
                      "signal or trade. The research verdicts come from the Phase 5.2 backtests of the same rules.",
                      "Account 1 (manual Phase 1 paper trading) is outside Phase 4; it is recorded and left unchanged."]}


def state_markdown(st: dict[str, Any], label: str) -> str:
    L = [f"# GoldScalp AI: Phase 4 final state ({label})", "",
         f"Recorded {st['recorded_at']}. Read-only record; nothing was changed to produce it.", "",
         f"**Conclusion.** {st['conclusion']}", ""]
    lp = st.get("loop") or {}
    L += [f"**Monitor loop:** running={lp.get('running')}, cycles={lp.get('cycles')}, last cycle {lp.get('last_cycle')}, "
          f"last error {lp.get('last_error')}." if "unavailable" not in lp else f"**Monitor loop:** {lp['unavailable']}", "",
          "## Monitors", "", "| Id | Strategy | Version | Fingerprint | Mode | Status | Account | Started | Stopped |",
          "|---:|---|---:|---|---|---|---:|---|---|"]
    for m in st["monitors"]:
        name = (m.get("strategy") or {}).get("name", "?")
        L.append(f"| {m['id']} | {name} | {m['version']} | `{m['fingerprint'][:16]}…` | {m['mode']} | **{m['status']}** | "
                 f"{m['account_id']} | {m['started_at']} | {m.get('stopped_at') or '—'} |")
    L += ["", "## Research verdicts of the monitored rules", "", "| Strategy | Version | Class | Evidence | Same rules as the Phase 5 original |",
          "|---|---:|---|---|---|"]
    for r in st["research_verdicts"]:
        L.append(f"| {r['strategy']} | {r['version']} | {r['class']} | {r['evidence']} | {r['same_rules_as_phase5_original']} |")
    L += ["", "## Activity", "", "| Table | Rows |", "|---|---:|"]
    L += [f"| {t} | {n} |" for t, n in st["phase4_counts"].items()]
    L += ["", "## Accounts", "", "| Id | Name | Purpose | Phase 4 | Balance | Open positions |", "|---:|---|---|---|---:|---:|"]
    for a in st["accounts"]:
        L.append(f"| {a['id']} | {a['name']} | {a['purpose']} | {'yes' if a['phase4'] else 'no'} | {a['balance']} | "
                 f"{len(a['open_positions'])} |")
    openp = [p for p in st["positions"] if p["status"] == "open"]
    L += ["", "## Outstanding simulated positions", ""]
    L += ([f"- Position {p['id']} (account {p['account_id']}): {p['side']} {p['lots']} lots at {p['open_price']} since "
           f"{p['open_time']}." for p in openp] or ["- None."])
    L += ["", "## MT5 verification records", ""]
    L += [f"- {v['id']} ({v['started_at']}): {v['overall']}, {v['environment']}." for v in st["mt5_verifications"]] or ["- None."]
    L += ["", "## Notes", ""] + [f"- {n}" for n in st["notes"]]
    return "\n".join(L) + "\n"


def record(label: str, staging: Path = STAGING) -> dict[str, Path]:
    staging.mkdir(parents=True, exist_ok=True)
    st = phase4_state()
    j, m = staging / f"phase4-final-state-{label}.json", staging / f"phase4-final-state-{label}.md"
    j.write_text(json.dumps(st, indent=2, default=str), encoding="utf-8")
    m.write_text(state_markdown(st, label), encoding="utf-8")
    return {"json": j, "markdown": m}


# ------------------------------------------------------------------ archive
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_db(src: Path, dest: Path) -> dict[str, Any]:
    """A consistent online copy (SQLite backup API from a read-only connection), then an integrity check."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    d = sqlite3.connect(dest)
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode = DELETE")
        ok = d.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {n: d.execute(f"SELECT COUNT(*) FROM [{n}]").fetchone()[0]
                  for (n,) in d.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}
    finally:
        d.close()
        s.close()
    return {"source": str(src.relative_to(PROJECT_ROOT)).replace("\\", "/"), "integrity": ok, "tables": tables}


def _code_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for pattern in CODE:
        for p in sorted(root.glob(pattern)):
            items = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file())
            for f in items:
                parts = f.relative_to(root).parts
                if any(fnmatch.fnmatch(part, ex) for part in parts for ex in CODE_EXCLUDE):
                    continue
                out.append(f)
    return out


def _export_strategies(app_db: Path, store: Path, dest: Path) -> dict[str, int]:
    con = _ro(app_db)
    try:
        versions = [{**r, "config": _json(r["config"])} for r in _rows(
            con, "SELECT s.name, s.archived, v.* FROM strategy_versions v JOIN strategies s ON s.id = v.strategy_id "
                 "ORDER BY v.strategy_id, v.version")]
    finally:
        con.close()
    s = _ro(store)
    try:
        research = [{**r, "config": _json(r.get("config"))} for r in _rows(s, "SELECT * FROM research_versions")]
        candidates = _rows(s, "SELECT * FROM candidates")
    finally:
        s.close()
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "app-strategy-versions.json").write_text(json.dumps(versions, indent=2, default=str), encoding="utf-8")
    (dest / "research-versions.json").write_text(json.dumps(research, indent=2, default=str), encoding="utf-8")
    (dest / "phase5-candidates.json").write_text(json.dumps(candidates, indent=2, default=str), encoding="utf-8")
    return {"app_strategy_versions": len(versions), "research_versions": len(research), "candidates": len(candidates)}


def build(note: str = "", root: Path = PROJECT_ROOT, archive: Path = ARCHIVE, staging: Path = STAGING,
          app_db: Path = APP_DB, store: Path = STORE, reports: Path = REPORTS) -> dict[str, Any]:
    if archive.exists():
        raise SystemExit(f"{archive} already exists: an archive is written once and never replaced")
    states = sorted(staging.glob("phase4-final-state-*"))
    if not any(p.name.startswith("phase4-final-state-post-stop") for p in states):
        raise SystemExit("record the post-stop Phase 4 state first (--record post-stop)")
    tmp = archive.with_name(archive.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "phase4").mkdir(parents=True)
    for p in states:
        shutil.copy2(p, tmp / "phase4" / p.name)
    data = {"application-database": snapshot_db(app_db, tmp / "data" / "goldscalp.sqlite3"),
            "research-store": snapshot_db(store, tmp / "data" / "monthly.sqlite3")}
    shutil.copytree(reports, tmp / "reports")
    (tmp / "docs").mkdir()
    for f in sorted(root.glob("*.md")):
        shutil.copy2(f, tmp / "docs" / f.name)
    strategies = _export_strategies(app_db, store, tmp / "strategies")
    code = tmp / "code" / f"goldscalp-{GENERATION}.zip"
    code.parent.mkdir()
    files = _code_files(root)
    with zipfile.ZipFile(code, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(root).as_posix())
    (tmp / "README.md").write_text(_readme(note), encoding="utf-8")
    entries = {p.relative_to(tmp).as_posix(): {"sha256": sha256(p), "bytes": p.stat().st_size}
               for p in sorted(tmp.rglob("*")) if p.is_file()}
    manifest = {"generation": GENERATION, "phases": "1-6", "created_at": now_iso(), "note": note,
                "conclusion": CONCLUSION, "databases": data, "strategies": strategies, "code_files": len(files),
                "forward_data": "Candles from 2026-11-14T00:00:00Z onward are NOT research data of this generation; "
                                "they are reserved, untouched, for future confirmation.",
                "files": entries}
    (tmp / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (tmp / "MANIFEST.sha256").write_text(sha256(tmp / "MANIFEST.json") + "  MANIFEST.json\n", encoding="utf-8")
    tmp.rename(archive)
    for p in archive.rglob("*"):
        if p.is_file():
            os.chmod(p, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)          # read-only (Windows: read-only attribute)
    return {"archive": str(archive), "files": len(entries) + 2, "databases": data, "strategies": strategies}


def _readme(note: str) -> str:
    return f"""# GoldScalp AI research archive: {GENERATION} (Phases 1-6)

Closed on 15 November 2026. Archive, never delete: every file here is read-only and listed with its SHA-256 in
`MANIFEST.json` (whose own hash is in `MANIFEST.sha256`). Verify with `python -m goldscalp.archive --check`.

**Conclusion.** {CONCLUSION} No strategy from Phases 1-6 is promoted or monitored.

| Folder | Contents |
|---|---|
| `phase4/` | Phase 4 final state before and after the monitors were stopped (monitors, configurations, accounts, positions, activity) |
| `data/` | Consistent snapshots of the application database and the research store (all runs, studies, results and history) |
| `reports/` | Every research report, frozen manifest and export from Phases 5-6 |
| `docs/` | Architecture documents, implementation and research reports, roadmap and README |
| `strategies/` | Every strategy version and research configuration as JSON |
| `code/` | The code, tests, web pages and configuration of this generation (zip) |

Forward data from 14 November 2026 onward is not part of this generation's research and stays untouched.
{('Note: ' + note) if note else ''}
"""


def check(archive: Path = ARCHIVE) -> dict[str, Any]:
    mf = archive / "MANIFEST.json"
    if not mf.exists():
        return {"archived": False}
    m = json.loads(mf.read_text(encoding="utf-8"))
    side = (archive / "MANIFEST.sha256").read_text(encoding="utf-8").split()[0]
    changed, missing, writable = [], [], []
    for rel, e in m["files"].items():
        p = archive / rel
        if not p.exists():
            missing.append(rel)
            continue
        if sha256(p) != e["sha256"]:
            changed.append(rel)
        if os.access(p, os.W_OK):
            writable.append(rel)
    extra = [p.relative_to(archive).as_posix() for p in archive.rglob("*") if p.is_file()
             and p.relative_to(archive).as_posix() not in m["files"] and p.name not in ("MANIFEST.json", "MANIFEST.sha256")]
    manifest_ok = sha256(mf) == side
    return {"archived": True, "generation": m["generation"], "created_at": m["created_at"], "files": len(m["files"]),
            "manifest_unchanged": manifest_ok, "unchanged": manifest_ok and not changed and not missing and not extra,
            "changed": changed, "missing": missing, "extra": extra, "writable": writable}


def main(argv: list[str]) -> None:
    if argv[:1] == ["--record"] and len(argv) == 2:
        print(json.dumps({k: str(v) for k, v in record(argv[1]).items()}))
    elif argv[:1] == ["--build"]:
        print(json.dumps(build(" ".join(argv[1:])), indent=2, default=str))
    elif argv[:1] == ["--check"]:
        print(json.dumps(check(), indent=2))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
