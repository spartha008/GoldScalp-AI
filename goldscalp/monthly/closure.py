"""Close the Phase 5 strategy-search cycle: freeze Phase 5.3, retire its candidates from promotion, and record which
data is spent. Read-only on both databases; writes one versioned manifest under var/monthly/reports/.

    python -m goldscalp.monthly.closure --close "<note>"
    python -m goldscalp.monthly.closure --check

After closing:
- Phase 5.3 (validation@1, study vl_747cce289048) is final. Its report, protocol, manifests, fingerprints, results
  and classifications are recorded here by hash and must not change. No further validation@1 run is accepted.
- R2 v2, R5 v2 and R7 v2 (rejected) and R1 v2 (inconclusive, not promising) are retired from promotion. None
  proceeds to paper-monitor observation. Nothing here touches Phase 4 or the application database.
- Every candle stored so far is spent. A new strategy hypothesis must be designed without, and eventually tested on,
  data outside the spent range."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from goldscalp.monthly import validation as vl
from goldscalp.monthly import validation_report as vr

ROOT = vr.ROOT
STORE = vr.STORE
APP_DB = ROOT / "var" / "goldscalp.sqlite3"
REPORTS = vr.REPORTS
MANIFEST = REPORTS / "phase5-research-cycle-closed.json"        # v1; amendments -v2 ... (never overwritten)
STUDY = "vl_747cce289048"
FILES = ("PHASE_5_3_VALIDATION_REPORT.md", f"var/monthly/reports/{STUDY}-validation.md",
         f"var/monthly/reports/{STUDY}-validation.json", f"var/monthly/reports/{STUDY}-validation.csv",
         "var/monthly/reports/validation-protocol-frozen.json", "goldscalp/monthly/validation.py",
         "goldscalp/monthly/validation_report.py", "goldscalp/monthly/migrations/0004_validation.sql")
RETIRED = {"R2|v-d-Xb": "rejected", "R5|v-d-Xd-tr": "rejected", "R7|v-c-Xe-tr": "rejected",
           "R1|v-b-Xd": "inconclusive (not promising)"}
RESEARCH_QUESTION = ("Why does the performance of these strategies change across market regimes (volatility, trend, "
                     "session, range expansion), and how can the research methodology detect regime dependence "
                     "before a candidate is promoted? Not: how to make these four strategies profitable.")


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _manifests() -> list[Path]:
    return sorted(REPORTS.glob(MANIFEST.stem + "*.json"), key=lambda q: (len(q.name), q.name))


def closed() -> bool:
    return bool(_manifests())


def phase53_fingerprint(store: Path | None = None) -> dict[str, Any]:
    con = _ro(store or STORE)
    try:
        h, n = hashlib.sha256(), 0
        for row in con.execute("SELECT config_key, timeframe, window, status, result_hash, metrics FROM validation_results "
                               "WHERE study_id = ? ORDER BY config_key, timeframe, window", (STUDY,)):
            h.update(json.dumps(list(row)).encode())
            n += 1
        s = con.execute("SELECT status, params, configs, analysis FROM validation_studies WHERE id = ?", (STUDY,)).fetchone()
    finally:
        con.close()
    summary = json.loads(s[3])["summary"] if s and s[3] else None
    return {"files": {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() if (ROOT / f).exists() else None
                      for f in FILES},
            "results": {"rows": n, "sha256": h.hexdigest()},
            "study": {"status": s[0] if s else None,
                      **{f"{k}_sha256": hashlib.sha256((v or "").encode()).hexdigest() if s else None
                         for k, v in zip(("params", "configs", "analysis"), (s[1:] if s else (None,) * 3), strict=True)}},
            "classes": summary}


def spent_data(store: Path | None = None, app_db: Path | None = None) -> dict[str, Any]:
    """The earliest and latest candle in either database: everything in between is spent (seen in design, selection,
    evaluation, warm-up or context)."""
    out = {}
    for name, path in (("research_store", store or STORE), ("app_db", app_db or APP_DB)):
        con = _ro(path)
        try:
            lo, hi = con.execute("SELECT MIN(ts), MAX(ts) FROM candles").fetchone()
        finally:
            con.close()
        out[name] = {"first": datetime.fromtimestamp(lo, UTC).isoformat() if lo is not None else None,
                     "last": datetime.fromtimestamp(hi, UTC).isoformat() if hi is not None else None}
    firsts = [v["first"] for v in out.values() if v["first"]]
    lasts = [v["last"] for v in out.values() if v["last"]]
    return {"by_database": out, "spent_from": min(firsts) if firsts else None, "spent_until": max(lasts) if lasts else None}


def phase4_record(app_db: Path | None = None) -> list[dict[str, Any]]:
    """What Phase 4 is monitoring at closure (recorded only; never changed from here)."""
    con = _ro(app_db or APP_DB)
    try:
        return [{"monitor": r[0], "strategy_id": r[1], "version": r[2], "fingerprint": r[3], "mode": r[4], "status": r[5]}
                for r in con.execute("SELECT id, strategy_id, version, fingerprint, mode, status FROM monitored_strategies "
                                     "ORDER BY id")]
    finally:
        con.close()


def close(note: str) -> Path:
    existing = _manifests()
    target = MANIFEST if not existing else MANIFEST.with_name(f"{MANIFEST.stem}-v{len(existing) + 1}.json")
    if target.exists():
        raise SystemExit(f"already closed: {target}")
    p53 = phase53_fingerprint()
    if p53["study"]["status"] != "COMPLETED" or p53["results"]["rows"] == 0:
        raise SystemExit(f"{STUDY} is not a completed study; nothing to close")
    proto = vr.check()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "version": len(existing) + 1, "closed_at": datetime.now(UTC).isoformat(), "note": note,
        "phase53": {"study": STUDY, "definition": vl.DEFINITION, **p53, "protocol_check": proto},
        "phase52": {"study": vl.SOURCE_STUDY, **vr.phase52_fingerprint()},
        "retired_from_promotion": {k: {"class": c, "fingerprint": vl.FINGERPRINTS[k],
                                       "paper_monitor_observation": False} for k, c in RETIRED.items()},
        "phase4_at_closure": phase4_record(),
        "spent_data": spent_data(),
        "next_research_question": RESEARCH_QUESTION,
        "fresh_data_rule": "A new hypothesis must be designed without, and eventually tested on, data outside the spent "
                           "range (forward data recorded after it, or history before it that was never imported or "
                           "inspected).",
    }, indent=1), encoding="utf-8")
    return target


def check() -> dict[str, Any]:
    ms = _manifests()
    if not ms:
        return {"closed": False}
    m = json.loads(ms[-1].read_text(encoding="utf-8"))
    now53, now52 = phase53_fingerprint(), vr.phase52_fingerprint()
    changed = [f for f, h in m["phase53"]["files"].items() if now53["files"].get(f) != h]
    changed += [f"phase53.{k}" for k in ("results", "study", "classes") if now53[k] != m["phase53"][k]]
    changed += [f for f, h in m["phase52"]["files"].items() if now52["files"].get(f) != h]
    changed += [f"phase52.{k}" for k in ("results", "configs_sha256", "study") if now52[k] != m["phase52"][k]]
    return {"closed": True, "unchanged": not changed, "changed": changed, "manifest": ms[-1].name,
            "closed_at": m["closed_at"], "retired": list(m["retired_from_promotion"]),
            "spent_until": m["spent_data"]["spent_until"]}


if __name__ == "__main__":
    if sys.argv[1] == "--close":
        print(close(" ".join(sys.argv[2:]) or "Phase 5 strategy search closed"))
    else:
        print(json.dumps(check(), indent=1))
