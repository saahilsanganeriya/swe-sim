"""Aggregating the audit over a record: pooling rules, per-model metrics, violations, exclusions, Markdown rendering."""
from __future__ import annotations

import json

from swesim.audit import llm_audit as L
from swesim.audit import report as REP
from swesim.audit import run as RUN
from test_audit_llm import FakeChat
from test_audit_run import HAIKU, SONNET, make_record, trajectory, v2_events, write_trial


def build(tmp_path):
    ws, spec = make_record(tmp_path)
    chat = FakeChat()
    judge = L.Judge(chat=chat)
    RUN.audit_scenario(ws, spec.id, judge=judge)
    # A: Sonnet, v2, asked before implementing, reply names a hidden-test identifier
    a = write_trial(ws, spec, model=SONNET, events=v2_events(reply="ValueError; test_raises_value_error covers it."), traj=trajectory())
    # B: Haiku, v2 spec, asked nothing
    b = write_trial(ws, spec, model=HAIKU, events=[], traj=trajectory(confirm=False))
    # C: Haiku, legacy stakeholder v1 (no filter annotations, no confirm channel)
    c = write_trial(ws, spec, model=HAIKU, attempt=1, events=v2_events(annotated=False)[:2], traj=trajectory(confirm=False))
    # D: Sonnet on an archived spec hash
    d = write_trial(ws, spec, model=SONNET, attempt=1, events=v2_events(), traj=trajectory(), spec_hash="f" * 64)
    # E: a full-condition trial (no interaction)
    e = write_trial(ws, spec, model=HAIKU, condition="full", events=None, traj=None)
    # F: Sonnet with an integrity signal and a question asked after the edit
    f = write_trial(ws, spec, model=SONNET, attempt=2, events=v2_events(), traj=trajectory(ask_first=False),
                    signals=[{"kind": "protected-path-edit"}], dropped=["tests/test_x.py"])
    for tid in (a, b, c, d, e, f):
        RUN.audit_trial(ws, tid, judge=judge)
    return ws, spec, dict(a=a, b=b, c=c, d=d, e=e, f=f), judge


def test_report_pools_only_current_v2_interactive_trials_and_lists_the_rest(tmp_path):
    ws, spec, ids, judge = build(tmp_path)
    rep = REP.build_report(ws, ws.registry_path, llm_usage=judge.usage())
    assert rep["trials"] == {"trial_dirs": 6, "audited": 6, "non_interactive": 1, "included": 3, "legacy": 1, "archived_spec": 1,
                             "unregistered_scenario": 0, "unknown_version": 1, "invalid_audit": 0}
    assert {e["trial_id"]: e["reason"] for e in rep["excluded"]} == {ids["c"]: "legacy stakeholder v1",
                                                                     ids["d"]: rep["excluded"][[e["trial_id"] for e in rep["excluded"]].index(ids["d"])]["reason"]}
    assert "archived spec" in next(e["reason"] for e in rep["excluded"] if e["trial_id"] == ids["d"])
    sonnet, haiku = rep["models"][SONNET], rep["models"][HAIKU]
    assert sonnet["trials"] == 2 and sonnet["asked_trials"] == 2 and sonnet["asked_before_implementing"] == {"true": 1, "false": 1, "unknown": 0, "rate": 0.5}
    assert sonnet["questions"] == 2 and sonnet["useful_questions"] == 2 and sonnet["useful_question_rate"] == 1.0 and sonnet["questions_per_trial"] == 1.0
    assert sonnet["questions_before_implementation"] == 1 and sonnet["questions_after_implementation"] == 1
    assert sonnet["replies"] == 4 and sonnet["confirmations"] == 2 and sonnet["test_or_identifier_disclosed"] == 1
    assert sonnet["integrity_signals"] == 1 and sonnet["flagged_trials"][0]["trial_id"] == ids["f"] and sonnet["flagged_trials"][0]["dropped_paths"] == ["tests/test_x.py"]
    assert haiku["trials"] == 1 and haiku["asked_trials"] == 0 and haiku["asked_before_implementing"] == {"true": 0, "false": 1, "unknown": 0, "rate": 0.0}
    assert haiku["questions"] == 0 and haiku["useful_question_rate"] is None and haiku["unknown_version_trials"] == [ids["b"]]
    v = rep["disclosure_violations"]
    assert [(x["trial_id"], x["seq"], x["kinds"]) for x in v] == [(ids["a"], 2, ["test_or_identifier_disclosed"])]
    assert "test_raises_value_error" in v[0]["hits"] and "ValueError" not in v[0]["hits"]
    s = rep["scenarios"][0]
    assert s["scenario_id"] == spec.id and s["current"] is True and s["coherent"] is True and s["fair_tests"] is False and s["leaderboard_eligible"] is False
    assert s["unfair_tests"] == ["t1"] and rep["unaudited_scenarios"] == []
    ps = rep["per_scenario"][spec.id]
    assert ps["trials"] == 3 and ps["models"] == {SONNET: 2, HAIKU: 1} and ps["questions"] == 2 and ps["replies"] == 4
    assert rep["llm_usage"]["calls"] == 1 + 3  # scenario + trials a, d, f (b has no exchanges, c is legacy, e is not interactive)


def test_markdown_and_files_render_from_the_same_dict(tmp_path):
    ws, spec, ids, judge = build(tmp_path)
    rep = REP.build_report(ws, ws.registry_path, llm_usage=judge.usage())
    md = REP.render_markdown(rep)
    assert SONNET in md and HAIKU in md and spec.id in md and ids["a"] in md and ids["c"] in md
    md_path, json_path = REP.write_report(rep, ws.root / "evidence" / "audit", "2026-09-13-task-sense")
    assert md_path.read_text() == md and json.loads(json_path.read_text())["trials"]["included"] == 3
    assert md_path.name == "2026-09-13-task-sense.md" and json_path.name == "2026-09-13-task-sense.json"


def test_report_on_an_empty_record_is_empty_not_broken(tmp_path):
    ws, spec = make_record(tmp_path)
    rep = REP.build_report(ws, ws.registry_path)
    assert rep["models"] == {} and rep["scenarios"] == [] and rep["unaudited_scenarios"] == [spec.id] and rep["trials"]["trial_dirs"] == 0
    assert "None." in REP.render_markdown(rep)
