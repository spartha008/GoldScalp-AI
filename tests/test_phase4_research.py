"""Phase 4 strategy improvement engine: windows (disjoint, live evidence after the holdout), findings, the proposal
catalogue, the validation verdict, a full study on RECORDED MT5 data, and the user-only adoption workflow
(approve -> new version; reject; never automatic)."""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from goldscalp.research import improvement
from goldscalp.storage import improvement_repo, strategy_repo
from goldscalp.strategies import builder
from tests.test_phase4_live import lab, monitor, mark_validated, ny_strategy  # noqa: F401  (fixture re-export)

D0 = datetime(2026, 11, 1, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ windows
def test_windows_are_disjoint_and_live_evidence_sits_after_the_holdout():
    w = improvement.plan_windows(D0, D0 + timedelta(days=10), discovery_pct=0.6, embargo_pct=0.05, live_first=None,
                                 use_live=False)
    assert w["discovery"]["end"] == D0 + timedelta(days=6) and w["holdout"]["start"] == D0 + timedelta(days=6.5)
    assert w["holdout"]["end"] == D0 + timedelta(days=10) and w["live_evidence_used"] is False
    live = improvement.plan_windows(D0, D0 + timedelta(days=10), discovery_pct=0.6, embargo_pct=0.05,
                                    live_first=D0 + timedelta(days=8), use_live=True)
    assert live["live_evidence_used"] and live["holdout"]["end"] == D0 + timedelta(days=8)    # history stops at live data
    too_early = improvement.plan_windows(D0, D0 + timedelta(days=10), discovery_pct=0.6, embargo_pct=0.05,
                                         live_first=D0 + timedelta(days=2), use_live=True)
    assert not too_early["live_evidence_used"] and "excluded" in too_early["live_note"]
    bad = {**w, "holdout": {"start": D0 + timedelta(days=5), "end": D0 + timedelta(days=9)}}
    with pytest.raises(improvement.StudyError, match="overlap"):
        improvement.check_disjoint(bad)
    leak = {**live, "live_after": D0 + timedelta(days=7)}
    with pytest.raises(improvement.StudyError, match="live-paper evidence overlaps"):
        improvement.check_disjoint(leak)
    with pytest.raises(improvement.StudyError, match="too short"):
        improvement.plan_windows(D0, D0 + timedelta(hours=3), discovery_pct=0.6, embargo_pct=0.05, live_first=None,
                                 use_live=False)


# ------------------------------------------------------------------------------------------------ findings / proposals
def _trade(i, session, r, *, vol="normal", h1="with", exit_reason="target", mfe_r=1.0, hold=10.0, stop_atr=1.5):
    return {"id": i, "session": session, "volatility": vol, "h1_rel": h1, "m15_rel": "with", "r_multiple": r,
            "net_pnl": r * 10, "exit_reason": exit_reason, "mfe_r": mfe_r, "holding_minutes": hold, "stop": 3990.0,
            "entry_price": 4000.0, "entry_time": f"2026-11-0{1 + i % 8}T10:00:00Z", "entry_spread": 0.2,
            "stop_atr": stop_atr}


def base_config():
    return builder.validate({"name": "Base", "direction": "long", "evaluation": {"step_timeframe": "M5"},
                             "conditions": [{"id": "s", "type": "structure_event", "timeframe": "M5"}],
                             "exit": {"stop": {"type": "atr", "atr_multiple": 1.5}, "max_holding_minutes": 60}})


def test_findings_map_to_catalogue_proposals_from_existing_conditions():
    trades = [_trade(i, "Asian", -0.6, exit_reason="stop", mfe_r=0.2, hold=5, stop_atr=0.7) for i in range(9)]
    trades += [_trade(100 + i, "London", 0.8) for i in range(10)]
    trades += [_trade(200 + i, "New York", 0.4, vol="contracting", h1="against") for i in range(4)]
    cfg = base_config()
    found = improvement.findings(trades, cfg, stress={"1": 50.0, "1.5": 10.0, "2": -20.0}, live=None)
    kinds = {f["kind"] for f in found}
    assert {"false_signals", "weak_session", "spread_sensitivity", "invalidation"} <= kinds
    weak = next(f for f in found if f["kind"] == "weak_session")
    assert weak["group"] == "Asian" and weak["n"] == 9 and weak["expectancy_r"] == -0.6
    assert not any(f["kind"] == "poor_volatility" for f in found)              # 4 trades < MIN_GROUP: not a finding
    props = improvement.propose(cfg, found)
    by_kind = {p["kind"]: p for p in props}
    sess = next(c for c in by_kind["weak_session"]["candidate"]["conditions"] if c["type"] == "session")
    assert sess["sessions"] == ["London", "New York"]
    spread = next(c for c in by_kind["spread_sensitivity"]["candidate"]["conditions"] if c["type"] == "spread")
    assert spread["max_spread"] == 0.2
    assert by_kind["invalidation"]["candidate"]["exit"]["stop"]["atr_multiple"] == 2.0
    for p in props:
        assert builder.validate(p["candidate"]) == p["candidate"]                     # valid strategy@1
        assert builder.fingerprint(p["candidate"]) != builder.fingerprint(cfg)       # a real change
        assert p["explanation"] and p["evidence"]
    assert cfg == base_config()                                                       # the base is never modified


def test_validation_verdict_rules():
    m = lambda n, net, exp, dd: {"trades": n, "net": net, "expectancy_r": exp, "max_drawdown": dd}  # noqa: E731
    assert improvement.decide_verdict(m(10, 5, 0.1, 10), m(40, 50, 0.3, 5), 3, 30)[0] == "insufficient_sample"
    assert improvement.decide_verdict(m(40, 5, 0.1, 10), m(40, 50, 0.3, 12), 2, 30)[0] == "improved"
    assert improvement.decide_verdict(m(40, 5, 0.1, 10), m(40, 50, 0.3, 12), 1, 30)[0] == "not_improved"   # sub-windows
    assert improvement.decide_verdict(m(40, 5, 0.1, 10), m(40, 50, 0.05, 5), 3, 30)[0] == "not_improved"   # expectancy
    assert improvement.decide_verdict(m(40, 5, 0.1, 10), m(40, 50, 0.3, 13), 3, 30)[0] == "not_improved"   # drawdown
    assert improvement.decide_verdict(m(40, 50, 0.1, 10), m(40, 5, 0.3, 5), 3, 30)[0] == "not_improved"    # net


# ------------------------------------------------------------------------------------------------ full study + adoption
def wait_study(c, sid, seconds=300):
    for _ in range(seconds * 10):
        s = c.get(f"/api/research/studies/{sid}").json()
        if s["status"] not in ("queued", "running"):
            return s
        time.sleep(0.1)
    raise AssertionError("study did not finish")


def test_study_on_recorded_data_and_user_only_adoption(lab):  # noqa: F811
    rec = ny_strategy(lab, hold=60)
    mark_validated(lab.gs, rec)
    m = monitor(lab, rec, mode="observe")
    lab.clock.set(datetime(2026, 11, 14, 0, 0, tzinfo=UTC))
    started = lab.post("/api/research/studies", json={"strategy_id": rec["id"], "use_live_evidence": True,
                                                      "assumptions": {"min_trades_for_ratios": 5}})
    assert started.status_code == 202, started.text
    study = wait_study(lab, started.json()["study"]["id"])
    assert study["status"] == "complete", study.get("error")
    res = study["result"]
    w = res["windows"]
    assert w["discovery"]["end"] <= w["embargo"]["end"] == w["holdout"]["start"] < w["holdout"]["end"]
    assert res["definition"] == "improve@1" and "SIMULATED" in res["disclaimer"] and res["spread_stress"]["1"] is not None
    assert {f["kind"] for f in res["findings"]} >= {"false_signals", "spread_sensitivity"}
    props = lab.get(f"/api/proposals?strategy_id={rec['id']}").json()["proposals"]
    for p in props:
        assert p["validation"]["definition"] == "validate@1" and p["limitations"] and p["status"] in (
            "validated", "inconclusive", "not_improved")
    # nothing changed by itself
    with lab.gs.db.read() as con:
        assert strategy_repo.get(con, rec["id"])["current_version"] == 1
    assert lab.get("/api/live/monitor").json()["monitored"][0]["version"] == 1
    # adoption is the user's decision, with the rules enforced
    pid = _insert_proposal(lab, rec, "inconclusive", "insufficient_sample", study["id"])
    refused = lab.post(f"/api/proposals/{pid}/approve", json={})
    assert refused.status_code == 409 and "acknowledge_insufficient_sample" in refused.json()["detail"]
    ok = lab.post(f"/api/proposals/{pid}/approve", json={"acknowledge_insufficient_sample": True, "note": "try it"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["new_version"] == 2 and body["proposal"]["status"] == "approved"
    assert "approved proposal" in body["proposal"]["versions"][0]["note"]
    assert lab.post(f"/api/proposals/{pid}/approve", json={}).status_code == 409             # decided once
    with lab.gs.db.read() as con:
        assert strategy_repo.get(con, rec["id"])["current_version"] == 2
    assert lab.get("/api/live/monitor").json()["monitored"][0]["version"] == 1                 # monitoring unchanged
    worse = _insert_proposal(lab, rec, "not_improved", "not_improved", study["id"], base_version=2)
    assert lab.post(f"/api/proposals/{worse}/approve", json={}).status_code == 409
    stale = _insert_proposal(lab, rec, "validated", "improved", study["id"], base_version=1)
    gone = lab.post(f"/api/proposals/{stale}/approve", json={})
    assert gone.status_code == 409 and "changed since the study" in gone.json()["detail"]
    rej = lab.post(f"/api/proposals/{stale}/reject", json={"note": "not now"}).json()["proposal"]
    assert rej["status"] == "rejected" and rej["decision_note"] == "not now"
    assert m["id"]


def _insert_proposal(c, rec, status, verdict, study_id, base_version=1):
    import secrets
    pid = "sp_" + secrets.token_hex(4)
    cand = builder.validate({**rec["config"], "conditions": rec["config"]["conditions"] + [
        {"id": "h1", "type": "structure_state", "timeframe": "H1", "state": "not_against"}]})
    with c.gs.db.transaction() as con:
        improvement_repo.insert_proposal(con, pid, study_id=study_id, strategy_id=rec["id"], base_version=base_version,
                                         base_fingerprint=rec["fingerprint"],
                                         p={"kind": "htf_combination", "title": "test", "candidate": cand,
                                            "diff": "new condition h1", "explanation": "e", "evidence": {"n": 9},
                                            "validation": {"verdict": verdict, "why": "w", "holdout": {}}},
                                         candidate_fingerprint=builder.fingerprint(cand), limitations=["small"],
                                         now=c.gs.clock.now())
        con.execute("UPDATE strategy_proposals SET status = ? WHERE id = ?", (status, pid))
    return pid


def test_research_and_assistant_packages_cannot_execute_or_adopt():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "goldscalp"
    for folder in ("research", "assistant"):
        for path in (root / folder).glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "open_position(" not in text and "close_position(" not in text, path.name
            assert "strategy_repo.save" not in text and "MetaTrader5" not in text.replace('"MetaTrader5"', ""), path.name
