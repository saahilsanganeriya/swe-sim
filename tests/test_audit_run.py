"""Auditing one scenario or one trial from the record and writing ``audit.json`` idempotently; the Temporal activities.

Offline: the record is a temporary workspace mirroring the laptop/VM layout (scenario dir with assets, trial dirs with the
frozen files, Harbor run dirs under ``jobs/runs``, exports); the LLM is ``FakeChat`` from ``test_audit_llm``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError

from swesim.audit import activities as ACT
from swesim.audit import llm_audit as L
from swesim.audit import record as REC
from swesim.audit import rubric as R
from swesim.audit import run as RUN
from swesim.models import RunConfig, ScenarioSpec, trial_id
from swesim.paths import Workspace, atomic_json
from swesim.stakeholder.session import CONFIRM_PREFIX
from test_audit_llm import FakeChat, spec_with_withholding
from test_audit_rubric import F05_7283, P2P_7283, TEST_PATCH_7283, spec_dict_7283

HAIKU = "anthropic/claude-haiku-4-5-20251001"
SONNET = "anthropic/claude-sonnet-5"
TEST_PATCH = ("diff --git a/tests/test_x.py b/tests/test_x.py\n+def test_raises_value_error(loader_fixture):\n"
              "+    with pytest.raises(ValueError):\n+        loader_fixture.load()\n")
ORACLE = "--- a/pkg/x.py\n+++ b/pkg/x.py\n-    raise KeyError(key)\n+    raise ValueError(key)\n"


def trajectory(*, ask_first: bool = True, path: str = "pkg/x.py", confirm: bool = True) -> dict:
    ask = {"step_id": 2, "timestamp": "2026-09-13T10:00:05Z", "source": "agent", "message": "asking",
           "tool_calls": [{"tool_call_id": "c1", "function_name": "bash", "arguments": {"command": "ask-user 'Which exception should be raised?'"}}],
           "observation": {"results": [{"source_call_id": "c1", "content": "ValueError.\n[availability remaining: 5]"}]}}
    edit = {"step_id": 3, "timestamp": "2026-09-13T10:00:20Z", "source": "agent", "message": "editing",
            "tool_calls": [{"tool_call_id": "c2", "function_name": "bash", "arguments": {"command": f"sed -i s/KeyError/ValueError/ {path}"}}],
            "observation": {"results": [{"source_call_id": "c2", "content": ""}]}}
    conf = {"step_id": 4, "timestamp": "2026-09-13T10:00:40Z", "source": "agent", "message": "confirming",
            "tool_calls": [{"tool_call_id": "c3", "function_name": "bash", "arguments": {"command": "confirm-behavior 'raises ValueError'"}}],
            "observation": {"results": [{"source_call_id": "c3", "content": "Yes."}]}}
    steps = [{"step_id": 1, "timestamp": "2026-09-13T10:00:00Z", "source": "user", "message": "Fix it."}]
    steps += [ask, edit] if ask_first else [edit, ask]
    if confirm:
        steps.append(conf)
    return {"schema_version": "ATIF-v1.4", "session_id": "s", "agent": {"name": "mini-swe-agent"}, "steps": steps}


def v2_events(reply: str = "ValueError, not KeyError.", fact_ids: list[str] | None = None, *, annotated: bool = True,
              redacted: list[str] | None = None) -> list[dict]:
    fact_ids = ["f01"] if fact_ids is None else fact_ids
    ev = [{"kind": "message", "ts": 1789293605.0, "seq": 1, "role": "user", "actor": "coding-agent", "exchange": "ask", "request_id": "r1",
           "content": "Which exception should be raised?"},
          {"kind": "message", "ts": 1789293606.0, "seq": 2, "role": "assistant", "actor": "stakeholder", "exchange": "ask", "request_id": "r1",
           "content": reply, "disclosed_fact_ids": fact_ids, "tier": "full", "patience_remaining": 5, "tool_calls": [],
           "reviewed_diff_sha256": None, "delivery": "generated"},
          {"kind": "message", "ts": 1789293640.0, "seq": 3, "role": "user", "actor": "coding-agent", "exchange": "confirm", "request_id": "r2",
           "content": CONFIRM_PREFIX + "The loader raises ValueError for a missing key."},
          {"kind": "message", "ts": 1789293641.0, "seq": 4, "role": "assistant", "actor": "stakeholder", "exchange": "confirm", "request_id": "r2",
           "content": "Yes, that matches.", "disclosed_fact_ids": ["f01"], "tier": "full", "patience_remaining": 3, "tool_calls": [],
           "reviewed_diff_sha256": None, "delivery": "generated"}]
    if annotated:
        for e in ev:
            if e["role"] == "assistant":
                e.update(regenerated=False, redacted_terms=list(redacted or []), redaction_count=len(redacted or []), withheld_sentences=[])
    return ev


def make_record(tmp_path: Path, spec: ScenarioSpec | None = None) -> tuple[Workspace, ScenarioSpec]:
    spec = spec or spec_with_withholding()
    ws = Workspace(tmp_path / "workspace", tmp_path / "registry.json")
    sdir = ws.scenario_dir(spec.id)
    (sdir / "assets").mkdir(parents=True)
    (sdir / "scenario.json").write_text(spec.model_dump_json(indent=2))
    (sdir / "validation.json").write_text(json.dumps({"status": "valid", "spec_hash": spec.spec_hash}))
    (sdir / "assets" / "test.patch").write_text(TEST_PATCH)
    (sdir / "assets" / "oracle.diff").write_text(ORACLE)
    atomic_json(ws.registry_path, {"scenarios": {spec.id: {"id": spec.id, "version": spec.version, "spec_hash": spec.spec_hash,
                                                            "validation_status": "valid", "source": "authored", "source_ref": "x"}}})
    return ws, spec


def write_trial(ws: Workspace, spec: ScenarioSpec, *, model: str = SONNET, condition: str = "interactive", attempt: int = 0,
                events: list[dict] | None = None, traj: dict | None = None, traj_where: str = "harbor",
                applied: list[str] | None = None, dropped: list[str] | None = None, signals: list[dict] | None = None,
                prompt_version: str = "v1", spec_hash: str | None = None, transcript: bool = True) -> str:
    rc = RunConfig(condition=condition, model=model, compiled_hash="c" * 64, brief_profile="natural" if condition == "full" else "withheld",
                   stakeholder={"prompt_version": prompt_version})
    tid = trial_id(spec.spec_hash, condition, rc.run_config_hash, attempt)
    tdir = ws.root / "trials" / tid
    tdir.mkdir(parents=True)
    harbor = ws.root / "jobs" / "runs" / tid / "task__abc"
    events = list(events or [])
    interactive = condition == "interactive"
    tr = ({"trial_id": tid, "events": [{"kind": "session", "seq": 0, "ts": 1789293600.0}, *events], "patience_start": 6,
           "patience_end": min([e["patience_remaining"] for e in events if e.get("role") == "assistant"] or [6]), "frozen": True,
           "transcript_hash": hashlib.sha256(json.dumps(events, sort_keys=True).encode()).hexdigest()} if interactive else {})
    report = {"outcome_code": 1, "applied_paths": ["pkg/x.py"] if applied is None else applied, "dropped_paths": dropped or [],
              "refused_paths": [], "integrity_signals": signals or [], "transcript_hash": tr.get("transcript_hash"),
              "transcript_binding_valid": True if interactive else None}
    record = {"trial_id": tid, "scenario_id": spec.id, "condition": condition, "attempt_index": attempt,
              "run_config_hash": rc.run_config_hash, "task_dir": str(tdir / "task"), "trial_dir": f"/opt/swesim/workspace/jobs/runs/{tid}/task__abc",
              "reward": {"reward": 1.0, "success": 1, "outcome_code": 1}, "report": report, "transcript": tr, "status": "completed",
              "spec_hash": spec_hash or spec.spec_hash, "model": model, "purpose": "comparison", "compiled_hash": "c" * 64,
              "brief_profile": rc.brief_profile, "started_at": "2026-09-13T10:00:00+00:00"}
    for name, obj in (("record.json", record), ("report.json", report), ("run_config.json", rc.model_dump(mode="json")),
                      ("spec.json", json.loads(spec.model_dump_json())), ("reward.json", record["reward"])):
        (tdir / name).write_text(json.dumps(obj, indent=2))
    if transcript:
        (tdir / "transcript.json").write_text(json.dumps(tr, indent=2))
    if traj is not None and traj_where == "harbor":
        (harbor / "agent").mkdir(parents=True)
        (harbor / "agent" / "trajectory.json").write_text(json.dumps(traj))
    if traj is not None and traj_where == "export":
        (ws.root / "exports").mkdir(exist_ok=True)
        (ws.root / "exports" / f"{tid}.episode.json").write_text(json.dumps({"trial_id": tid, "policy_visible": {"agent_trajectory": traj}}))
    return tid


# ---------------------------------------------------------------- reading the record

def test_stakeholder_version_from_run_config_then_transcript_annotations():
    assert REC.stakeholder_version({"stakeholder": {"prompt_version": "v2"}}, {"events": []}) == ("v2", "run_config.stakeholder.prompt_version")
    assert REC.stakeholder_version({"stakeholder": {"prompt_version": "v3"}}, {"events": v2_events()}) == ("v3", "run_config.stakeholder.prompt_version")
    v, why = REC.stakeholder_version({"stakeholder": {"prompt_version": "v1"}}, {"events": v2_events()})
    assert v == "v2" and "regenerated" in why
    v, why = REC.stakeholder_version({"stakeholder": {"prompt_version": "v1"}}, {"events": v2_events(annotated=False)[:2]})
    assert v == "v1" and "no filter annotations" in why
    v, why = REC.stakeholder_version({"stakeholder": {"prompt_version": "v1"}}, {"events": []})
    assert v is None and "no exchange" in why


def test_load_trial_reads_the_frozen_files_trajectory_and_forbidden_terms(tmp_path):
    ws, spec = make_record(tmp_path)
    tid = write_trial(ws, spec, events=v2_events(), traj=trajectory())
    b = REC.load_trial(ws, tid)
    assert b.trial_id == tid and b.scenario_id == spec.id and b.spec_hash == spec.spec_hash and b.interactive
    assert b.stakeholder_version == "v2" and b.trajectory_source == "jobs/runs" and b.trajectory["steps"]
    assert b.submission_paths == ["pkg/x.py"] and b.solver_model == SONNET
    assert "test_raises_value_error" in b.forbidden_terms and "loader_fixture" in b.forbidden_terms
    assert "test.patch" in b.test_assets and b.brief_profile == "withheld" and "Traceback" not in b.brief_seen_by_agent
    assert {f["id"] for f in b.visible_facts} == {"f01", "withheld-001"}


def test_load_trial_falls_back_to_the_export_trajectory_and_reports_none(tmp_path):
    ws, spec = make_record(tmp_path)
    tid = write_trial(ws, spec, events=v2_events(), traj=trajectory(), traj_where="export")
    assert REC.load_trial(ws, tid).trajectory_source == "export"
    tid2 = write_trial(ws, spec, events=v2_events(), traj=None, attempt=1)
    b = REC.load_trial(ws, tid2)
    assert b.trajectory is None and b.trajectory_source is None


def test_load_trial_rejects_missing_transcripts_and_bad_ids(tmp_path):
    ws, spec = make_record(tmp_path)
    tid = write_trial(ws, spec, events=v2_events(), transcript=False)
    with pytest.raises(REC.AuditInputError, match="transcript.json"):
        REC.load_trial(ws, tid)
    with pytest.raises(REC.AuditInputError, match="trial id"):
        REC.load_trial(ws, "not-a-trial")
    with pytest.raises(REC.AuditInputError, match="not found"):
        REC.load_trial(ws, "0" * 24)


def test_load_scenario_reads_assets_and_forbidden_terms(tmp_path):
    ws, spec = make_record(tmp_path)
    b = REC.load_scenario(ws, spec.id)
    assert b.spec_hash == spec.spec_hash and b.withheld_brief and "Traceback" not in b.withheld_brief
    assert b.test_assets == {"test.patch": TEST_PATCH} and b.oracle_diff == ORACLE and b.validation_status == "valid"
    assert set(b.asset_hashes) == {"test.patch", "oracle.diff"} and "test_raises_value_error" in b.forbidden_terms
    with pytest.raises(REC.AuditInputError):
        REC.load_scenario(ws, "missing-scenario")


# ---------------------------------------------------------------- audit_trial

def test_audit_trial_writes_the_interfaces_shape_and_combines_mechanical_and_model_labels(tmp_path):
    ws, spec = make_record(tmp_path)
    tid = write_trial(ws, spec, events=v2_events(reply="ValueError; the test_raises_value_error case covers it."), traj=trajectory())
    chat = FakeChat()
    audit = RUN.audit_trial(ws, tid, judge=L.Judge(chat=chat))
    path = ws.root / "trials" / tid / "audit.json"
    on_disk = json.loads(path.read_text())
    assert set(on_disk) >= {"questions", "replies", "asked_before_implementing", "useful_question_rate", "model", "audited_at", "rubric_version"}
    assert on_disk["rubric_version"] == R.RUBRIC_VERSION and on_disk["model"] == L.AUDIT_MODEL and on_disk["legacy"] is False
    assert audit.stakeholder_version == "v2" and audit.asked_before_implementing is True and audit.trajectory_source == "jobs/runs"
    q = audit.questions[0]
    assert q.seq == 1 and q.before_implementation is True and q.specific is True and q.redundant is False and q.reason
    r = {x.seq: x for x in audit.replies}
    # the model said no identifier was disclosed, but the reply filter's own terms find a test name: the mechanical evidence
    # wins; the generic exception name is not a hit whatever the deny-list contains
    assert r[2].test_or_identifier_disclosed is True and "test_raises_value_error" in r[2].hits and "ValueError" not in r[2].hits
    assert "test_raises_value_error" in r[2].reason
    assert r[2].targeted is True and r[2].behaviour_level is True
    assert r[4].seq == 4 and r[4].test_or_identifier_disclosed is False    # the confirm reply was not labelled by the fake: mechanical only
    assert audit.useful_question_rate == 1.0 and audit.patience.spent == 3 and audit.patience.sensible is True
    assert len(chat.calls) == 1 and json.loads(chat.calls[0]["messages"][0]["content"])["hidden_tests"]["test.patch"] == TEST_PATCH


def test_audit_trial_is_idempotent_until_inputs_or_rubric_change(tmp_path):
    ws, spec = make_record(tmp_path)
    tid = write_trial(ws, spec, events=v2_events(), traj=trajectory())
    chat = FakeChat()
    first = RUN.audit_trial(ws, tid, judge=L.Judge(chat=chat))
    again = RUN.audit_trial(ws, tid, judge=L.Judge(chat=chat))
    assert again == first and len(chat.calls) == 1
    forced = RUN.audit_trial(ws, tid, judge=L.Judge(chat=chat), force=True)
    assert len(chat.calls) == 2 and forced.inputs_hash == first.inputs_hash
    # a changed transcript changes the inputs hash and re-audits
    tdir = ws.root / "trials" / tid
    tr = json.loads((tdir / "transcript.json").read_text())
    tr["events"][2]["content"] = "ValueError, and log a warning."
    tr["transcript_hash"] = "0" * 64
    (tdir / "transcript.json").write_text(json.dumps(tr))
    changed = RUN.audit_trial(ws, tid, judge=L.Judge(chat=chat))
    assert changed.inputs_hash != first.inputs_hash and len(chat.calls) == 3
    # a stale rubric version on disk re-audits too
    stale = json.loads((tdir / "audit.json").read_text())
    stale["rubric_version"] = "2000-01-01.0"
    (tdir / "audit.json").write_text(json.dumps(stale))
    RUN.audit_trial(ws, tid, judge=L.Judge(chat=chat))
    assert len(chat.calls) == 4


def test_audit_trial_labels_stakeholder_v1_as_legacy_without_calling_the_model(tmp_path):
    ws, spec = make_record(tmp_path)
    # stakeholder v1: an ask exchange without the reply filter's annotations and without the confirm channel
    tid = write_trial(ws, spec, events=v2_events(annotated=False)[:2], traj=trajectory(confirm=False))
    chat = FakeChat()
    audit = RUN.audit_trial(ws, tid, judge=L.Judge(chat=chat))
    assert audit.legacy is True and audit.stakeholder_version == "v1" and audit.model is None and chat.calls == []
    assert audit.asked_before_implementing is True                      # mechanical labels still computed
    assert audit.questions[0].specific is None and audit.questions[0].before_implementation is True
    assert audit.replies[0].behaviour_level is None and audit.replies[0].test_or_identifier_disclosed is None
    assert R.code_like_hits(audit.replies[0].hits) == []                 # "ValueError, not KeyError." names no test identifier


def test_audit_trial_without_exchanges_or_non_interactive_needs_no_model(tmp_path):
    ws, spec = make_record(tmp_path)
    quiet = write_trial(ws, spec, model=HAIKU, events=[], traj=trajectory(confirm=False))
    chat = FakeChat()
    a = RUN.audit_trial(ws, quiet, judge=L.Judge(chat=chat))
    assert a.stakeholder_version is None and a.legacy is False and a.asked_before_implementing is False
    assert a.questions == [] and a.replies == [] and a.useful_question_rate is None and chat.calls == []
    full = write_trial(ws, spec, condition="full", events=None, traj=None, attempt=0)
    b = RUN.audit_trial(ws, full, judge=L.Judge(chat=chat))
    assert b.asked_before_implementing is None and b.questions == [] and b.model is None and chat.calls == []
    assert (ws.root / "trials" / full / "audit.json").exists()


def test_audit_trial_without_trajectory_leaves_before_implementation_unknown(tmp_path):
    ws, spec = make_record(tmp_path)
    tid = write_trial(ws, spec, events=v2_events(), traj=None)
    a = RUN.audit_trial(ws, tid, judge=L.Judge(chat=FakeChat()))
    assert a.asked_before_implementing is None and a.questions[0].before_implementation is None and a.trajectory_source is None


def test_audit_trial_flags_a_reply_that_restates_an_ungrounded_requirement(tmp_path):
    ws, spec = make_record(tmp_path)
    events = v2_events()
    events[3]["content"] = "Yes. The remaining requirement is bounding the backward scan so crafted files fail quickly rather than scanning indefinitely."
    tid = write_trial(ws, spec, events=events, traj=trajectory())
    a = RUN.audit_trial(ws, tid, judge=L.Judge(chat=FakeChat()))
    r = {x.seq: x for x in a.replies}
    assert r[4].ungrounded == ["r02"] and r[4].test_or_identifier_disclosed is True and "r02" in r[4].reason
    assert r[2].ungrounded == []


def test_audit_trial_edit_before_ask_and_volunteered_facts(tmp_path):
    ws, spec = make_record(tmp_path)
    tid = write_trial(ws, spec, events=v2_events(fact_ids=["f01", "withheld-001"]), traj=trajectory(ask_first=False))
    fake = FakeChat(trial={"questions": [{"seq": 1, "specific": True, "decision_oriented": True, "fishing": False, "redundant": False, "reason": "r"}],
                           "replies": [{"seq": 2, "targeted": False, "volunteered": ["withheld-001"], "test_or_identifier_disclosed": False,
                                        "behaviour_level": True, "reason": "restates the traceback unasked"}]})
    a = RUN.audit_trial(ws, tid, judge=L.Judge(chat=fake))
    assert a.asked_before_implementing is False and a.questions[0].before_implementation is False
    r = a.replies[0]
    assert r.targeted is False and r.volunteered == ["withheld-001"]


# ---------------------------------------------------------------- audit_scenario

def test_audit_scenario_writes_labels_reasons_and_unfair_tests(tmp_path):
    ws, spec = make_record(tmp_path)
    chat = FakeChat()
    a = RUN.audit_scenario(ws, spec.id, judge=L.Judge(chat=chat))
    on_disk = json.loads((ws.scenario_dir(spec.id) / "audit.json").read_text())
    assert set(on_disk) >= {"coherent", "fair_tests", "plausible_withholding", "reasons", "model", "audited_at", "rubric_version"}
    assert a.coherent is True and a.fair_tests is False and a.plausible_withholding is True and a.unfair_tests == ["t1"]
    assert a.reasons["fair_tests"] and a.model == L.AUDIT_MODEL and a.spec_hash == spec.spec_hash
    # the fact-scope label: the model judged both visible facts inside the contract, nothing mechanical contradicts it
    assert a.facts_within_contract is True and [(fs.fact_id, fs.within_contract) for fs in a.fact_scope] == [("f01", True), ("withheld-001", True)]
    assert "f09" not in {fs.fact_id for fs in a.fact_scope} and a.reasons["facts_within_contract"]
    assert on_disk["facts_within_contract"] is True and on_disk["fact_scope"][0]["fact_id"] == "f01"   # what the index projects to task_sense
    payload = json.loads(chat.calls[0]["messages"][0]["content"])
    assert payload["hidden_tests"]["test.patch"] == TEST_PATCH and "raise ValueError" in payload["oracle_diff"]
    assert RUN.audit_scenario(ws, spec.id, judge=L.Judge(chat=chat)) == a and len(chat.calls) == 1


def _record_7283(tmp_path):
    spec = ScenarioSpec.model_validate(spec_dict_7283())
    ws, spec = make_record(tmp_path, spec)
    (ws.scenario_dir(spec.id) / "assets" / "test.patch").write_text(TEST_PATCH_7283)
    return ws, spec


def _scenario_answer(fact_scope: list[dict]) -> dict:
    return {"coherent": {"label": True, "reason": "one symptom"}, "fair_tests": {"label": True, "reason": "follows from f01"},
            "plausible_withholding": {"label": None, "reason": "n/a"}, "fact_scope": fact_scope}


def test_audit_scenario_mechanical_fact_scope_overrides_a_lenient_model(tmp_path):
    """pytest-7283: the model calls f05 within the contract; the mechanical rule finds it names the PASS_TO_PASS-only
    parametrisation and wins. The combined reason keeps both pieces of evidence."""
    ws, spec = _record_7283(tmp_path)
    lenient = FakeChat(scenario=_scenario_answer([{"fact_id": "f01", "within_contract": True, "reason": "the failing test checks it"},
                                                  {"fact_id": "f05", "within_contract": True, "reason": "harmless generalisation"},
                                                  {"fact_id": "f09", "within_contract": False, "reason": "not visible anyway"}]))
    a = RUN.audit_scenario(ws, spec.id, judge=L.Judge(chat=lenient))
    by_id = {fs.fact_id: fs for fs in a.fact_scope}
    assert set(by_id) == {"f01", "f05"} and a.facts_within_contract is False and a.coherent is True and a.fair_tests is True
    assert by_id["f01"].within_contract is True and by_id["f01"].reason.endswith("the failing test checks it")
    assert by_id["f05"].within_contract is False and by_id["f05"].offending_tests == [P2P_7283[0]] and by_id["f05"].markers == ["@pytest.mark.skip"]
    assert P2P_7283[0] in by_id["f05"].reason and "harmless generalisation" in by_id["f05"].reason
    assert "f05" in a.reasons["facts_within_contract"] and P2P_7283[0] in a.reasons["facts_within_contract"]
    payload = json.loads(lenient.calls[0]["messages"][0]["content"])
    assert payload["pass_to_pass"] == P2P_7283 and F05_7283 in json.dumps(payload["stakeholder_visible_facts"])
    on_disk = json.loads((ws.scenario_dir(spec.id) / "audit.json").read_text())
    assert on_disk["facts_within_contract"] is False and on_disk["rubric_version"] == R.RUBRIC_VERSION


def test_audit_scenario_takes_the_model_verdict_where_the_mechanical_rule_is_silent(tmp_path):
    ws, spec = _record_7283(tmp_path)
    d = spec_dict_7283()
    d["ledger"] = [f for f in d["ledger"] if f["id"] != "f05"]
    spec = ScenarioSpec.model_validate(d)
    (ws.scenario_dir(spec.id) / "scenario.json").write_text(spec.model_dump_json(indent=2))
    strict = FakeChat(scenario=_scenario_answer([{"fact_id": "f01", "within_contract": False, "reason": "also promises tearDown ordering nobody tests"}]))
    a = RUN.audit_scenario(ws, spec.id, judge=L.Judge(chat=strict))
    assert [(fs.fact_id, fs.within_contract) for fs in a.fact_scope] == [("f01", False)] and a.facts_within_contract is False
    assert a.fact_scope[0].offending_tests == [] and "nobody tests" in a.fact_scope[0].reason
    silent = FakeChat(scenario=_scenario_answer([]))
    a = RUN.audit_scenario(ws, spec.id, force=True, judge=L.Judge(chat=silent))
    assert a.fact_scope[0].within_contract is None and a.facts_within_contract is None and "not judged" in a.fact_scope[0].reason


def test_audit_scenario_without_withheld_brief_marks_withholding_not_applicable(tmp_path):
    from swesim.models import minimal_spec_dict
    ws, spec = make_record(tmp_path, ScenarioSpec.model_validate(minimal_spec_dict()))
    a = RUN.audit_scenario(ws, spec.id, judge=L.Judge(chat=FakeChat()))
    assert a.plausible_withholding is None and "no withheld brief" in a.reasons["plausible_withholding"]


# ---------------------------------------------------------------- activities

def test_activities_are_registered_with_exact_names():
    assert [a.__name__ for a in ACT.ALL] == ["audit_scenario_activity", "audit_transcript_activity"]
    from temporalio import activity
    assert activity._Definition.from_callable(ACT.audit_scenario_activity).name == "audit_scenario_activity"
    assert activity._Definition.from_callable(ACT.audit_transcript_activity).name == "audit_transcript_activity"


def test_activities_read_the_settings_workspace_and_are_idempotent(tmp_path, monkeypatch):
    ws, spec = make_record(tmp_path)
    tid = write_trial(ws, spec, events=v2_events(), traj=trajectory())
    monkeypatch.setenv("SWESIM_WORKSPACE", str(ws.root))
    monkeypatch.setenv("SWESIM_REGISTRY", str(ws.registry_path))
    monkeypatch.setenv("SWESIM_ENV_FILE", str(tmp_path / "none.env"))
    from swesim.settings import reset_settings
    reset_settings()
    fake = FakeChat()
    monkeypatch.setattr(L, "chat_json_with_usage", fake)
    params = {"trial_id": tid, "audit_model": L.AUDIT_FALLBACK_MODEL}
    out = asyncio.run(ACT.audit_transcript_activity(params))
    assert fake.calls[0]["model"] == L.AUDIT_FALLBACK_MODEL
    assert out["trial_id"] == tid and out["asked_before_implementing"] is True and out["written"] is True
    assert Path(out["path"]) == ws.root / "trials" / tid / "audit.json" and out["llm_calls"] == 1
    again = asyncio.run(ACT.audit_transcript_activity(params))
    assert again["written"] is False and len(fake.calls) == 1
    s = asyncio.run(ACT.audit_scenario_activity({"scenario_id": spec.id}))
    assert s["scenario_id"] == spec.id and s["coherent"] is True and s["fair_tests"] is False and s["written"] is True
    assert s["facts_within_contract"] is True
    with pytest.raises(ApplicationError) as e:
        asyncio.run(ACT.audit_transcript_activity({"trial_id": "0" * 24}))
    assert e.value.non_retryable is True
    with pytest.raises(ApplicationError):
        asyncio.run(ACT.audit_scenario_activity({}))
    reset_settings()
