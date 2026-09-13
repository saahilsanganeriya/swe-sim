"""Task 12: register, score, export. Offline; fixtures mirror the frozen record/transcript/report layouts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swesim.models import RunConfig, ScenarioSpec, minimal_spec_dict, trial_id
from swesim.paths import Workspace
from swesim.stages.export import export_scenario
from swesim.stages.register import register_scenario
from swesim.stages.score import (OUTCOME_NAMES, load_trial_records, normalize_record, pass_at_k, pass_pow_k,
                                 score_scenario, summarize)

COMPILED = "c" * 64
IMAGE = "sha256:" + "d" * 64
HAIKU = "anthropic/claude-haiku-4-5-20251001"
SONNET = "anthropic/claude-sonnet-5"
T0 = 1789293600.0  # 2026-09-13T10:00:00Z

TRAJ = {
    "schema_version": "ATIF-v1.4", "session_id": "sess",
    "agent": {"name": "mini-swe-agent", "version": "2.4.6", "model_name": HAIKU},
    "steps": [
        {"step_id": 1, "timestamp": "2026-09-13T10:00:00Z", "source": "user", "message": "Fix the bug. It breaks."},
        {"step_id": 2, "timestamp": "2026-09-13T10:00:05Z", "source": "agent", "message": "I will ask which exception.",
         "tool_calls": [{"tool_call_id": "c1", "function_name": "bash",
                         "arguments": {"command": "ask-user 'Which exception should be raised?'"}}],
         "observation": {"results": [{"source_call_id": "c1", "content": "ValueError.\n[availability remaining: 5]"}]}},
        {"step_id": 3, "timestamp": "2026-09-13T10:00:20Z", "source": "agent", "message": "Editing.",
         "tool_calls": [{"tool_call_id": "c2", "function_name": "bash", "arguments": {"command": "sed -i s/KeyError/ValueError/ pkg/x.py"}}],
         "observation": {"results": [{"source_call_id": "c2", "content": ""}]}},
    ],
}


def ask_events(t0: float, question: str, reply: str, fact_ids: list[str], remaining: int, rid: str = "q1") -> list[dict]:
    return [
        {"kind": "message", "ts": t0, "seq": 0, "role": "user", "actor": "coding-agent", "content": question,
         "request_id": rid, "exchange": "ask"},
        {"kind": "message", "ts": t0 + 2, "seq": 1, "role": "assistant", "actor": "stakeholder", "content": reply,
         "request_id": rid, "exchange": "ask", "disclosed_fact_ids": fact_ids, "tier": "full",
         "patience_remaining": remaining, "tool_calls": [], "reviewed_diff_sha256": None, "delivery": "generated"},
    ]


def review_events(t0: float, remaining: int, rid: str = "r1") -> list[dict]:
    sha = "e" * 64
    return [
        {"kind": "message", "ts": t0, "seq": 2, "role": "user", "actor": "coding-agent",
         "content": "Review this agent-supplied diff; it is not verified against the workspace.\n--- a\n+++ b",
         "request_id": rid, "exchange": "review"},
        {"kind": "message", "ts": t0 + 3, "seq": 3, "role": "assistant", "actor": "stakeholder", "content": "Looks right.",
         "request_id": rid, "exchange": "review", "disclosed_fact_ids": [], "tier": "full", "patience_remaining": remaining,
         "tool_calls": [{"tool": "read_diff", "sha256": sha, "verified": False}], "reviewed_diff_sha256": sha,
         "delivery": "generated"},
    ]


def make_spec(sid: str = "example-scenario", version: int = 1, with_author_only: bool = False) -> ScenarioSpec:
    d = minimal_spec_dict()
    d["id"] = sid
    d["version"] = version
    if with_author_only:
        d["ledger"].append({"id": "f09", "text": "SECRET-AUTHOR-ONLY-NOTE", "kind": "context", "provenance": "authored",
                            "authored_rationale": "internal", "discovery": "author-only"})
    return ScenarioSpec.model_validate(d)


def make_workspace(tmp_path: Path, spec: ScenarioSpec, *, status: str = "valid", compiled: str = COMPILED,
                   validation_compiled: str | None = None) -> Workspace:
    ws = Workspace(tmp_path / "workspace", tmp_path / "registry.json")
    sdir = ws.scenario_dir(spec.id)
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "scenario.json").write_text(spec.model_dump_json(indent=2))
    (sdir / "validation.json").write_text(json.dumps({
        "status": status, "failures": [] if status == "valid" else ["oracle failed"], "expected_base": {"t1": "failed"},
        "expected_gold": {"t1": "passed"}, "controls": {"nop": {"reward": 0, "success": 0, "outcome": "valid-incorrect"}},
        "probe": {}, "trial_dirs": {}, "spec_hash": spec.spec_hash, "source": spec.source.value,
        "source_ref": spec.source_ref, "environment_provider": "modal", "image_digest": IMAGE,
        "compiled_hash": validation_compiled or compiled}))
    (sdir / "compile.json").write_text(json.dumps({
        "compiled_hash": compiled, "spec_hash": spec.spec_hash, "run_config_hash": "x" * 64, "image_digest": IMAGE,
        "brief_profile": "withheld", "task_dirs": {c: str(ws.task_dir(spec.id, c)) for c in ("full", "hidden", "interactive")}}))
    return ws


def write_trial(ws: Workspace, spec: ScenarioSpec, *, condition: str = "interactive", model: str = HAIKU,
                purpose: str = "calibration", attempt: int = 0, outcome_code: int = 1, success: int = 1,
                reward: float = 1.0, events: list[dict] | None = None, traj: bool = True, shape: str = "flat",
                brief_profile: str = "withheld", label: dict | None = None, status: str = "completed",
                harbor_exception: str | None = None) -> str:
    rc = RunConfig(condition=condition, model=model, purpose=purpose, compiled_hash=COMPILED,
                   brief_profile="natural" if condition == "full" else brief_profile)
    tid = trial_id(spec.spec_hash, rc.condition, rc.run_config_hash, attempt)
    tdir = ws.root / "trials" / tid
    (tdir / "task").mkdir(parents=True)
    (tdir / "task" / "instruction.md").write_text("# Task\n\nIt breaks.\n\nAsk the stakeholder with `ask-user`.\n")
    harbor = ws.jobs_dir / "runs" / tid / f"{spec.id}__{condition}__1"
    (harbor / "agent").mkdir(parents=True)
    (harbor / "verifier").mkdir()
    if traj:
        (harbor / "agent" / "trajectory.json").write_text(json.dumps(TRAJ))
    interactive = condition == "interactive"
    events = list(events or [])
    asks = sum(1 for e in events if e.get("role") == "user" and e.get("exchange") == "ask")
    reviews = sum(1 for e in events if e.get("role") == "user" and e.get("exchange") == "review")
    reward_obj = {"reward": reward, "success": success, "outcome_code": outcome_code, "f2p_passed": success, "p2p_passed": 1}
    report = {"outcome": OUTCOME_NAMES[outcome_code], "per_test": [{"id": "t1", "kind": "fail_to_pass", "expected": "passed",
                                                                     "observed": "passed" if success else "failed"},
                                                                    {"id": "t0", "kind": "pass_to_pass", "expected": "passed",
                                                                     "observed": "passed"}],
              "per_group": [{"id": "g01", "weight": 1.0, "passed": bool(success)}], "patch_sha256": "p" * 64,
              "transcript_hash": "h" * 64 if interactive else None, "reviewed_diff_sha256": None,
              "integrity_signals": [], "dropped_paths": [], "reviewed_equals_submitted": None,
              "image_digest": IMAGE, "simulator_validity": "unadjudicated" if interactive else "n/a",
              "harbor_exception": harbor_exception}
    if interactive:
        report["transcript_binding_valid"] = True
    transcript = ({"trial_id": tid, "events": events, "patience_start": 6, "patience_end": 6 - asks - 2 * reviews,
                   "frozen": True, "transcript_hash": "h" * 64, "created_at": T0} if interactive else {})
    (harbor / "verifier" / "reward.json").write_text(json.dumps(reward_obj))
    (harbor / "verifier" / "report.json").write_text(json.dumps(report))
    (harbor / "result.json").write_text(json.dumps({"trial_name": harbor.name, "exception_info": None}))
    record = {"trial_id": tid, "scenario_id": spec.id, "condition": condition, "attempt_index": attempt,
              "run_config_hash": rc.run_config_hash, "task_dir": str(tdir / "task"), "trial_dir": str(harbor),
              "reward": reward_obj, "report": report, "transcript": transcript, "status": status,
              "spec_hash": spec.spec_hash, "model": model, "purpose": purpose, "compiled_hash": COMPILED,
              "brief_profile": rc.brief_profile}
    if shape == "nested":  # the other accepted dump: lineage under run_config, no flat copies
        for k in ("scenario_id", "run_config_hash", "model", "purpose", "compiled_hash", "brief_profile"):
            record.pop(k)
        record["run_config"] = rc.model_dump(mode="json")
    for name, obj in (("record.json", record), ("reward.json", reward_obj), ("report.json", report),
                      ("transcript.json", transcript), ("run_config.json", rc.model_dump(mode="json")),
                      ("spec.json", json.loads(spec.model_dump_json())),
                      ("status.json", {"status": "finished" if status == "completed" else status, "trial_id": tid,
                                       "started_at": "2026-09-13T09:59:00Z", "finished_at": "2026-09-13T10:05:00Z"})):
        (tdir / name).write_text(json.dumps(obj, indent=2))
    if label:
        (tdir / "label.json").write_text(json.dumps(label))
    return tid


# ---------------------------------------------------------------- pass metrics (brief tests, verbatim semantics)

def test_pass_metrics():
    assert pass_at_k(3, 2, 1) == pytest.approx(2 / 3) and round(pass_at_k(3, 2, 3), 9) == 1.0 and pass_at_k(3, 0, 3) == 0.0
    assert pass_pow_k(3, 2, 3) == 0 and pass_pow_k(3, 3, 3) == 1
    assert pass_at_k(2, 1, 3) is None and pass_pow_k(2, 1, 3) is None


def test_pass_metrics_reject_impossible_counts():
    for bad in ((3, 4, 1), (3, -1, 1), (3, 1, 0)):
        with pytest.raises(ValueError):
            pass_at_k(*bad)
        with pytest.raises(ValueError):
            pass_pow_k(*bad)
    assert pass_at_k(0, 0, 1) is None and pass_pow_k(0, 0, 1) is None


def test_summarize_outcomes():
    recs = [{"condition": "interactive", "run_config_hash": "r", "reward": {"success": 1, "reward": 1.0, "outcome_code": 1},
             "transcript": {"events": [{"kind": "message", "role": "user", "exchange": "ask"}], "patience_end": 5, "patience_start": 6},
             "report": {}, "status": "completed"},
            {"condition": "interactive", "run_config_hash": "r", "reward": {"success": 0, "reward": 0.5, "outcome_code": 2},
             "transcript": {"events": []}, "report": {}, "status": "completed"},
            {"condition": "interactive", "run_config_hash": "r", "reward": {"outcome_code": 5}, "transcript": {}, "report": {},
             "status": "completed"}]
    s = summarize(recs, k=3)["interactive|natural|r"]
    assert s["n_valid"] == 2 and s["success_count"] == 1 and s["outcomes"]["collection-failure"] == 1 and s["mean_reward"] == 0.75 and s["questions_asked"] == [1, 0]
    assert s["n"] == 2 and s["n_total"] == 3 and s["requested_k"] == 3
    assert s["pass_at_k"] is None and s["pass_pow_k"] is None  # n_valid < k: null, never a silently reduced k
    assert s["reviews"] == [0, 0] and s["patience_end"] == [5, None] and s["reviewed_equals_submitted"] == [None, None]
    assert s["labels"] == {}


def test_summarize_only_valid_outcomes_enter_denominators():
    def rec(code, success, reward):
        return {"condition": "hidden", "brief_profile": "withheld", "run_config_hash": "r", "model": HAIKU, "purpose": "calibration",
                "reward": {"success": success, "reward": reward, "outcome_code": code}, "transcript": {}, "report": {}}
    s = summarize([rec(1, 1, 1.0), rec(2, 0, 0.0), rec(2, 0, 0.5), rec(3, 0, 0.0), rec(4, 0, 0.0), rec(7, 0, 0.0)], k=3)["hidden|withheld|r"]
    assert s["n_total"] == 6 and s["n_valid"] == 3 and s["n_agent_failures"] == 2 and s["n_infra_failures"] == 1
    assert s["success_count"] == 1 and s["pass_at_k"] == pytest.approx(1.0) and s["pass_pow_k"] == 0.0
    assert s["mean_reward"] == pytest.approx(0.5) and s["model"] == HAIKU and s["purpose"] == "calibration"
    assert s["outcomes"] == {"valid-correct": 1, "valid-incorrect": 2, "agent-invalid-submission": 1, "agent-runtime-failure": 1,
                             "evaluator-failure": 1}


def test_summarize_never_pools_purposes_or_configs():
    base = {"condition": "interactive", "brief_profile": "withheld", "reward": {"success": 1, "reward": 1.0, "outcome_code": 1},
            "transcript": {}, "report": {}}
    two = summarize([{**base, "run_config_hash": "cal", "purpose": "calibration"},
                     {**base, "run_config_hash": "cmp", "purpose": "comparison"}], k=1)
    assert set(two) == {"interactive|withheld|cal", "interactive|withheld|cmp"}
    with pytest.raises(ValueError, match="pool"):
        summarize([{**base, "run_config_hash": "same", "purpose": "calibration"},
                   {**base, "run_config_hash": "same", "purpose": "comparison"}], k=1)
    with pytest.raises(ValueError, match="duplicate"):
        summarize([{**base, "run_config_hash": "r", "trial_id": "a" * 24}, {**base, "run_config_hash": "r", "trial_id": "a" * 24}], k=1)
    assert summarize([], k=3) == {}


# ---------------------------------------------------------------- record normalisation and scoring on disk

def test_normalize_record_accepts_flat_and_nested_dumps(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec)
    flat = write_trial(ws, spec, attempt=0, shape="flat", events=ask_events(T0 + 6, "Which exception?", "ValueError.", ["f01"], 5))
    nested = write_trial(ws, spec, attempt=1, shape="nested")
    recs = {r["trial_id"]: r for r in load_trial_records(ws, spec.id)}
    assert set(recs) == {flat, nested}
    rc = RunConfig(condition="interactive", model=HAIKU, purpose="calibration", compiled_hash=COMPILED, brief_profile="withheld")
    for r in recs.values():
        assert r["scenario_id"] == spec.id and r["run_config_hash"] == rc.run_config_hash and r["model"] == HAIKU
        assert r["purpose"] == "calibration" and r["brief_profile"] == "withheld" and r["spec_hash"] == spec.spec_hash
        assert r["compiled_hash"] == COMPILED and r["reward"]["outcome_code"] == 1 and r["status_file"]["status"] == "finished"
    assert normalize_record({"trial_id": "x", "condition": "full"}) == {"trial_id": "x", "condition": "full", "label": None}
    other = make_spec("other-scenario")
    (ws.scenario_dir(other.id)).mkdir(parents=True)
    write_trial(ws, other, attempt=0)
    assert {r["trial_id"] for r in load_trial_records(ws, spec.id)} == {flat, nested}
    assert len(load_trial_records(ws)) == 3


def test_score_scenario_writes_scores(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec)
    q = ask_events(T0 + 6, "Which exception?", "ValueError.", ["f01"], 5)
    a = write_trial(ws, spec, attempt=0, events=q, label={"label": "unresolved", "note": ""})
    b = write_trial(ws, spec, attempt=1, outcome_code=2, success=0, reward=0.0, events=q + review_events(T0 + 30, 3),
                    label={"label": "no-question", "note": "asked nothing useful"})
    c = write_trial(ws, spec, attempt=2, outcome_code=5, success=0, reward=0.0)
    d = write_trial(ws, spec, attempt=0, purpose="comparison", model=SONNET, shape="nested")
    e = write_trial(ws, spec, attempt=0, condition="full", purpose="calibration")
    out = score_scenario(ws, spec.id, k=2)
    assert out == ws.scenario_dir(spec.id) / "scores.json"
    scores = json.loads(out.read_text())
    cal = RunConfig(condition="interactive", model=HAIKU, purpose="calibration", compiled_hash=COMPILED, brief_profile="withheld")
    cmp_ = RunConfig(condition="interactive", model=SONNET, purpose="comparison", compiled_hash=COMPILED, brief_profile="withheld")
    full = RunConfig(condition="full", model=HAIKU, purpose="calibration", compiled_hash=COMPILED, brief_profile="natural")
    assert set(scores) == {f"interactive|withheld|{cal.run_config_hash}", f"interactive|withheld|{cmp_.run_config_hash}",
                           f"full|natural|{full.run_config_hash}"}
    g = scores[f"interactive|withheld|{cal.run_config_hash}"]
    assert g["n_total"] == 3 and g["n_valid"] == 2 and g["success_count"] == 1 and g["outcomes"]["collection-failure"] == 1
    assert g["pass_at_k"] == pytest.approx(1.0) and g["pass_pow_k"] == 0.0 and g["mean_reward"] == 0.5
    assert g["questions_asked"] == [1, 1] and g["reviews"] == [0, 1] and g["patience_end"] == [5, 3]
    assert g["reviewed_equals_submitted"] == [None, None] and g["labels"] == {"unresolved": 1, "no-question": 1}
    assert g["model"] == HAIKU and g["purpose"] == "calibration" and g["configured_k"] == 3 and g["requested_k"] == 2
    assert g["trial_ids"] == [a, b, c] and g["condition"] == "interactive" and g["brief_profile"] == "withheld"
    cmpg = scores[f"interactive|withheld|{cmp_.run_config_hash}"]
    assert cmpg["model"] == SONNET and cmpg["purpose"] == "comparison" and cmpg["n_valid"] == 1 and cmpg["pass_at_k"] is None
    assert cmpg["trial_ids"] == [d]
    fg = scores[f"full|natural|{full.run_config_hash}"]
    assert fg["trial_ids"] == [e] and fg["questions_asked"] == [0] and fg["patience_end"] == [None]


def test_score_scenario_requires_frozen_lineage(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec)
    tid = write_trial(ws, spec)
    p = ws.root / "trials" / tid / "record.json"
    rec = json.loads(p.read_text())
    rec.pop("spec_hash")
    p.write_text(json.dumps(rec))
    (ws.root / "trials" / tid / "spec.json").unlink()
    with pytest.raises(ValueError, match="lineage"):
        score_scenario(ws, spec.id)
    with pytest.raises(ValueError):
        score_scenario(ws, "Bad Id")


def test_score_scenario_without_trials_writes_empty(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec)
    assert json.loads(score_scenario(ws, spec.id).read_text()) == {}


# ---------------------------------------------------------------- register

def test_register_scenario_writes_registry_and_dataset(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec)
    cal = write_trial(ws, spec, attempt=0, purpose="calibration")
    cmp_ = write_trial(ws, spec, attempt=0, purpose="comparison", model=SONNET, shape="nested")
    demo = write_trial(ws, spec, attempt=0, purpose="demonstration", condition="hidden")
    other = make_spec("other-scenario")
    ws.scenario_dir(other.id).mkdir(parents=True)
    write_trial(ws, other, attempt=0)  # different spec_hash: must not be listed under example-scenario
    entry = register_scenario(ws, spec)
    reg = json.loads(ws.registry_path.read_text())
    assert reg["scenarios"][spec.id] == entry
    assert entry["id"] == spec.id and entry["version"] == 1 and entry["spec_hash"] == spec.spec_hash
    assert entry["source"] == "authored" and entry["source_ref"] == "example" and entry["validation_status"] == "valid"
    assert entry["validation_path"] == str(ws.scenario_dir(spec.id) / "validation.json")
    assert entry["conditions"] == ["full", "hidden", "interactive"] and entry["image_digest"] == IMAGE
    assert entry["compiled_hash"] == COMPILED
    assert entry["calibration_runs"] == [cal] and entry["comparison_runs"] == [cmp_] and entry["demonstration_runs"] == [demo]
    ds = json.loads((ws.root / "dataset.json").read_text())
    assert [t["path"] for t in ds["tasks"]] == [str(ws.task_dir(spec.id, c)) for c in ("full", "hidden", "interactive")]
    assert all(t["scenario_id"] == spec.id and t["spec_hash"] == spec.spec_hash for t in ds["tasks"])
    assert {t["condition"] for t in ds["tasks"]} == {"full", "hidden", "interactive"}
    # idempotent re-register keeps a single entry
    register_scenario(ws, spec)
    assert list(json.loads(ws.registry_path.read_text())["scenarios"]) == [spec.id]
    assert (ws.registry_path.parent / "registry.json.lock").exists()


def test_register_rejects_unvalidated_or_changed(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec, status="invalid")
    with pytest.raises(ValueError, match="unvalidated|changed"):
        register_scenario(ws, spec)
    ws = make_workspace(tmp_path / "b", spec, validation_compiled="f" * 64)  # validated build differs from compiled build
    with pytest.raises(ValueError, match="unvalidated|changed"):
        register_scenario(ws, spec)
    ws = make_workspace(tmp_path / "c", spec)
    register_scenario(ws, spec)
    changed = make_spec(with_author_only=True)  # same id/version, different content
    make_workspace(tmp_path / "c", changed)
    with pytest.raises(ValueError, match="version"):
        register_scenario(ws, changed)
    bumped = make_spec(version=2, with_author_only=True)
    make_workspace(tmp_path / "c", bumped)
    entry = register_scenario(ws, bumped)
    reg = json.loads(ws.registry_path.read_text())
    assert entry["version"] == 2 and reg["scenarios"][spec.id]["spec_hash"] == bumped.spec_hash
    assert len(reg["scenarios"]) == 1
    make_workspace(tmp_path / "c", spec)  # v1 validated again, but v2 is already registered
    with pytest.raises(ValueError, match="version"):
        register_scenario(ws, spec)  # cannot go back to version 1 content


# ---------------------------------------------------------------- export

def test_export_scenario_manifest(tmp_path):
    spec = make_spec(with_author_only=True)
    ws = make_workspace(tmp_path, spec)
    q = ask_events(T0 + 6, "Which exception should be raised?", "ValueError.", ["f01"], 5)
    tid = write_trial(ws, spec, events=q + review_events(T0 + 30, 3), label={"label": "unresolved", "note": "n"})
    full = write_trial(ws, spec, condition="full", attempt=0)
    other = make_spec("other-scenario")
    ws.scenario_dir(other.id).mkdir(parents=True)
    write_trial(ws, other)
    outs = export_scenario(ws, spec.id)
    assert sorted(p.name for p in outs) == sorted([f"{tid}.episode.json", f"{full}.episode.json"])
    m = json.loads((ws.root / "exports" / f"{tid}.episode.json").read_text())
    rc = RunConfig(condition="interactive", model=HAIKU, purpose="calibration", compiled_hash=COMPILED, brief_profile="withheld")
    assert m["trial_id"] == tid and m["family_id"] == "example" and m["condition"] == "interactive" and m["brief_profile"] == "withheld"
    assert m["scenario_id"] == spec.id and m["model"] == HAIKU
    pv = m["policy_visible"]
    assert pv["instruction"].startswith("# Task") and pv["agent_trajectory"]["steps"][1]["source"] == "agent"
    assert pv["stakeholder_replies"] == [{"step_id": 2, "ts": "2026-09-13T10:00:05Z", "shim": "ask-user",
                                          "command": "ask-user 'Which exception should be raised?'",
                                          "observation": "ValueError.\n[availability remaining: 5]"}]
    assert pv["stakeholder_replies_source"] == "agent-trajectory-observations"
    assert "SECRET-AUTHOR-ONLY-NOTE" not in json.dumps(pv) and "disclosed_fact_ids" not in json.dumps(pv)
    eo = m["evaluator_only"]
    assert [f["id"] for f in eo["ledger"]] == ["f01", "f09"] and eo["author_only_fact_ids"] == ["f09"]
    assert eo["oracle_asset"] == "oracle.diff" and eo["test_assets"] == ["test.patch"]
    assert eo["report"]["patch_sha256"] == "p" * 64 and len(eo["stakeholder_audit"]) == 4
    assert eo["stakeholder_audit"][1]["disclosed_fact_ids"] == ["f01"] and eo["stakeholder_audit"][1]["delivery"] == "generated"
    assert eo["reviewer_label"] == {"label": "unresolved", "note": "n"}
    ln = m["lineage"]
    assert ln == {"spec_hash": spec.spec_hash, "run_config_hash": rc.run_config_hash, "compiled_hash": COMPILED,
                  "image_digest": IMAGE, "purpose": "calibration", "patch_sha256": "p" * 64, "transcript_hash": "h" * 64,
                  "harbor_trial_dir": str(ws.jobs_dir / "runs" / tid / f"{spec.id}__interactive__1"), "attempt_index": 0}
    assert m["reward"]["outcome_code"] == 1 and m["simulator_validity"] == "unadjudicated" and m["training_eligible"] is False
    assert m["interaction"] == {"patience_start": 6, "patience_end": 3, "questions": 1, "reviews": 1,
                                "questions_observed_by_policy": 1, "reviews_observed_by_policy": 0}
    f = json.loads((ws.root / "exports" / f"{full}.episode.json").read_text())
    assert f["simulator_validity"] == "n/a" and f["training_eligible"] is True and f["interaction"]["questions"] == 0
    assert f["evaluator_only"]["reviewer_label"] is None and f["policy_visible"]["stakeholder_replies"] == [
        {"step_id": 2, "ts": "2026-09-13T10:00:05Z", "shim": "ask-user", "command": "ask-user 'Which exception should be raised?'",
         "observation": "ValueError.\n[availability remaining: 5]"}]  # shared fixture trajectory; full condition has no shim


def test_export_without_trajectory_or_binding_is_not_training_eligible(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec)
    no_traj = write_trial(ws, spec, condition="full", traj=False)
    unbound = write_trial(ws, spec, condition="interactive", attempt=1)
    p = ws.root / "trials" / unbound / "record.json"
    rec = json.loads(p.read_text())
    rec["report"]["transcript_binding_valid"] = False
    rec["report"]["simulator_validity"] = "ok"
    p.write_text(json.dumps(rec))
    adjudicated = write_trial(ws, spec, condition="interactive", attempt=2)
    p = ws.root / "trials" / adjudicated / "record.json"
    rec = json.loads(p.read_text())
    rec["report"]["simulator_validity"] = "ok"
    p.write_text(json.dumps(rec))
    export_scenario(ws, spec.id)
    read = lambda t: json.loads((ws.root / "exports" / f"{t}.episode.json").read_text())  # noqa: E731
    assert read(no_traj)["training_eligible"] is False and read(no_traj)["policy_visible"]["agent_trajectory"] == {}
    assert read(no_traj)["policy_visible"]["stakeholder_replies"] == []
    assert read(unbound)["training_eligible"] is False
    assert read(adjudicated)["training_eligible"] is True and read(adjudicated)["simulator_validity"] == "ok"


def test_export_ignores_other_scenarios_and_handles_no_trials(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec)
    assert export_scenario(ws, spec.id) == []
    assert (ws.root / "exports").is_dir()
