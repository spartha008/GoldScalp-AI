"""Closing the Phase 5 strategy search: Phase 5.3 frozen by hash, candidates retired, no further validation@1 run,
spent data recorded, both databases only read."""
# ruff: noqa: F811 - pytest fixtures (client, world) are shared from other Phase 5 test modules
from __future__ import annotations

import json

from goldscalp.monthly import closure as cl
from tests.test_phase5_monthly import _table_counts, world  # noqa: F401
from tests.test_phase53_validation import client  # noqa: F401

P53 = {"files": {"r.md": "1"}, "results": {"rows": 80, "sha256": "x"},
       "study": {"status": "COMPLETED", "params_sha256": "p", "configs_sha256": "c", "analysis_sha256": "a"},
       "classes": {"R2": {"candidate": "rejected", "original": "rejected"}}}
P52 = {"files": {"v.json": "2"}, "results": {"rows": 3894, "sha256": "y"}, "configs_sha256": "c", "study": {"status": "COMPLETED"}}


def _patch(monkeypatch, tmp_path, p53=P53, p52=P52):
    monkeypatch.setattr(cl, "REPORTS", tmp_path)
    monkeypatch.setattr(cl, "MANIFEST", tmp_path / "phase5-research-cycle-closed.json")
    monkeypatch.setattr(cl, "phase53_fingerprint", lambda store=None: p53)
    monkeypatch.setattr(cl.vr, "phase52_fingerprint", lambda store=None: p52)
    monkeypatch.setattr(cl.vr, "check", lambda: {"frozen": True, "unchanged": True})
    monkeypatch.setattr(cl, "phase4_record", lambda app_db=None: [{"monitor": 1, "status": "active"}])
    monkeypatch.setattr(cl, "spent_data", lambda store=None, app_db=None: {"spent_until": "2026-11-13T21:56:00+00:00"})


def test_close_records_everything_and_check_detects_any_change(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    assert cl.check() == {"closed": False} and not cl.closed()
    path = cl.close("closing")
    m = json.loads(path.read_text(encoding="utf-8"))
    assert set(m["retired_from_promotion"]) == {"R2|v-d-Xb", "R5|v-d-Xd-tr", "R7|v-c-Xe-tr", "R1|v-b-Xd"}
    assert all(not v["paper_monitor_observation"] for v in m["retired_from_promotion"].values())
    assert m["retired_from_promotion"]["R1|v-b-Xd"]["class"].startswith("inconclusive")
    assert "regime" in m["next_research_question"] and m["phase53"]["classes"] == P53["classes"]
    assert cl.check()["unchanged"] and cl.closed()
    for p53, p52, what in (({**P53, "files": {"r.md": "9"}}, P52, "r.md"),
                           ({**P53, "classes": {"R2": {"candidate": "promising", "original": "rejected"}}}, P52, "phase53.classes"),
                           ({**P53, "results": {"rows": 81, "sha256": "z"}}, P52, "phase53.results"),
                           (P53, {**P52, "results": {"rows": 1, "sha256": "q"}}, "phase52.results")):
        monkeypatch.setattr(cl, "phase53_fingerprint", lambda store=None, v=p53: v)
        monkeypatch.setattr(cl.vr, "phase52_fingerprint", lambda store=None, v=p52: v)
        chk = cl.check()
        assert not chk["unchanged"] and what in chk["changed"]
    monkeypatch.setattr(cl, "phase53_fingerprint", lambda store=None: P53)
    monkeypatch.setattr(cl.vr, "phase52_fingerprint", lambda store=None: P52)
    assert cl.close("amendment").name.endswith("-v2.json") and path.exists()      # versioned, never overwritten


def test_close_refuses_an_incomplete_study(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path, p53={**P53, "study": {"status": "RUNNING"}})
    try:
        cl.close("x")
        raise AssertionError("closed an incomplete study")
    except SystemExit as exc:
        assert "not a completed study" in str(exc)
    assert not cl.closed()


def test_no_validation_run_after_closure_and_readers_never_write(client, world, monkeypatch):
    c, store, _fps = client
    before = _table_counts(world["app"])
    monkeypatch.setattr(cl, "closed", lambda: True)
    r = c.post("/api/monthly/validation-studies")
    assert r.status_code == 409 and "closed" in r.json()["detail"]
    spent = cl.spent_data(store=store, app_db=world["app"])
    assert spent["spent_until"] is not None and spent["by_database"]["app_db"]["first"] is not None
    assert isinstance(cl.phase4_record(world["app"]), list)
    assert _table_counts(world["app"]) == before
