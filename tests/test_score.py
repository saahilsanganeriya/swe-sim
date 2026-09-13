"""Task 12: register, score, export. Offline; fixtures mirror the frozen record/transcript/report layouts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swesim.models import RunConfig, ScenarioSpec, minimal_spec_dict, trial_id
from swesim.paths import Workspace
from swesim.stages.export import export_scenario
from swesim.stages.register import register_scenario
from swesim.stages.score import (
    OUTCOME_NAMES,
    load_trial_records,
    normalize_record,
    pass_at_k,
    pass_pow_k,
    score_scenario,
    summarize,
)

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
                harbor_exception: str | None = None, trajectory: dict | None = None) -> str:
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
        (harbor / "agent" / "trajectory.json").write_text(json.dumps(trajectory or TRAJ))
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


def write_unresolved(ws: Workspace, spec: ScenarioSpec, *, kind: str, attempt: int = 0, condition: str = "interactive",
                     model: str = HAIKU, purpose: str = "calibration") -> str:
    """An attempt directory that ended without record.json, in one of the shapes the run stage / Temporal runner leave:
    status-error (run.py unknown-exception path: status.json + materialized task/task.toml), status-error-rc (same plus a
    run_config.json sibling), runner-error (runner-error.json + request.json + launch.json), request-only, launch-failed,
    stale-running (status.json at running, nothing else), request-no-rc (request.json without run_config)."""
    rc = RunConfig(condition=condition, model=model, purpose=purpose, compiled_hash=COMPILED,
                   brief_profile="natural" if condition == "full" else "withheld")
    tid = trial_id(spec.spec_hash, rc.condition, rc.run_config_hash, attempt)
    d = ws.root / "trials" / tid
    d.mkdir(parents=True)
    request = {"trial_id": tid, "scenario_id": spec.id, "spec_hash": spec.spec_hash, "condition": condition, "model": model,
               "run_config": rc.model_dump(mode="json"), "run_config_hash": rc.run_config_hash, "attempt_index": attempt,
               "stakeholder_url": "https://stakeholder.example.test"}
    started, finished = "2026-09-13T12:00:00Z", "2026-09-13T12:03:00Z"
    if kind in ("status-error", "status-error-rc"):
        (d / "task").mkdir()
        (d / "task" / "task.toml").write_text(
            f'[metadata]\ntrial_id = "{tid}"\nrun_config_hash = "{rc.run_config_hash}"\ncompiled_hash = "{COMPILED}"\n'
            f'condition = "{condition}"\nbrief_profile = "{rc.brief_profile}"\n')
        (d / "status.json").write_text(json.dumps({"status": "error", "trial_id": tid, "started_at": started, "finished_at": finished,
                                                   "error": "RuntimeError"}))
        if kind == "status-error-rc":
            (d / "run_config.json").write_text(json.dumps(rc.model_dump(mode="json")))
    elif kind == "runner-error":
        (d / "request.json").write_text(json.dumps(request))
        (d / "launch.json").write_text(json.dumps({"trial_id": tid, "phase": "acknowledged", "unit": f"swesim-trial-{tid}", "returncode": 0}))
        (d / "runner-error.json").write_text(json.dumps({"trial_id": tid, "error": "HarborRunError('no trial dir')",
                                                         "type": "HarborRunError", "at": T0 + 100}))
    elif kind == "request-only":
        (d / "request.json").write_text(json.dumps(request))
    elif kind == "launch-failed":
        (d / "request.json").write_text(json.dumps(request))
        (d / "launch.json").write_text(json.dumps({"trial_id": tid, "phase": "failed", "error": "FileNotFoundError('systemd-run')"}))
    elif kind == "stale-running":
        (d / "status.json").write_text(json.dumps({"status": "running", "trial_id": tid, "started_at": started, "finished_at": None}))
    elif kind == "request-no-rc":
        (d / "request.json").write_text(json.dumps({"trial_id": tid, "scenario_id": spec.id, "spec_hash": spec.spec_hash}))
    else:
        raise ValueError(kind)
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


def _rec(code, success, reward, **extra):
    return {"condition": "hidden", "brief_profile": "withheld", "run_config_hash": "r", "model": HAIKU, "purpose": "calibration",
            "reward": {"success": success, "reward": reward, "outcome_code": code}, "transcript": {}, "report": {}, **extra}


def test_summarize_agent_attributable_outcomes_enter_denominators():
    # Ruling: outcomes 1-4 are all agent-attributable attempts (1 success, 2-4 failures); 5-7 are excluded from n.
    s = summarize([_rec(1, 1, 1.0), _rec(2, 0, 0.0), _rec(2, 0, 0.5), _rec(3, 0, 0.0), _rec(4, 0, 0.0), _rec(7, 0, 0.0)], k=3)["hidden|withheld|r"]
    assert s["n_total"] == 6 and s["n_valid"] == 5 and s["n"] == 5 and s["n_agent_failures"] == 2 and s["n_infra_failures"] == 1
    assert s["success_count"] == 1 and s["pass_at_k"] == pytest.approx(1 - 4 / 10) and s["pass_pow_k"] == 0.0
    assert s["mean_reward"] == pytest.approx(1.5 / 5) and s["model"] == HAIKU and s["purpose"] == "calibration"
    assert s["outcomes"] == {"valid-correct": 1, "valid-incorrect": 2, "agent-invalid-submission": 1, "agent-runtime-failure": 1,
                             "evaluator-failure": 1}


def test_agent_failure_lowers_pass_at_1_but_infra_failure_does_not_change_n():
    base = [_rec(1, 1, 1.0, trial_id="a" * 24), _rec(1, 1, 1.0, trial_id="b" * 24)]
    s0 = summarize(base, k=1)["hidden|withheld|r"]
    assert s0["n"] == 2 and s0["pass_at_k"] == pytest.approx(1.0)
    s3 = summarize(base + [_rec(3, 0, 0.0, trial_id="c" * 24)], k=1)["hidden|withheld|r"]
    assert s3["n"] == 3 and s3["pass_at_k"] == pytest.approx(2 / 3) and s3["pass_at_k"] < s0["pass_at_k"]
    assert s3["outcomes"]["agent-invalid-submission"] == 1 and s3["n_agent_failures"] == 1
    s6 = summarize(base + [_rec(3, 0, 0.0, trial_id="c" * 24), _rec(6, 0, 0.0, trial_id="d" * 24)], k=1)["hidden|withheld|r"]
    assert s6["n"] == 3 and s6["pass_at_k"] == pytest.approx(2 / 3) and s6["n_total"] == 4
    assert s6["outcomes"]["simulator-failure"] == 1 and s6["n_infra_failures"] == 1 and s6["success_count"] == 2
    assert s6["inconsistent_reward"] == []


def test_summarize_flags_reward_files_where_success_disagrees_with_outcome():
    ok = summarize([_rec(1, 1, 1.0, trial_id="a" * 24), _rec(2, 0, 0.5, trial_id="b" * 24), _rec(5, 0, 0.0, trial_id="c" * 24)], k=1)["hidden|withheld|r"]
    assert ok["inconsistent_reward"] == [] and ok["success_count"] == 1
    bad = summarize([_rec(2, 1, 1.0, trial_id="a" * 24),   # valid-incorrect yet success 1
                     _rec(1, 0, 1.0, trial_id="b" * 24),   # valid-correct yet success 0
                     _rec(6, 1, 0.0, trial_id="c" * 24),   # infrastructure failure yet success 1
                     _rec(1, 1, 1.0)], k=1)["hidden|withheld|r"]  # consistent, no trial_id
    assert bad["inconsistent_reward"] == ["a" * 24, "b" * 24, "c" * 24]
    assert bad["success_count"] == 2 and bad["n"] == 3  # `success` still drives the metric; the flag makes the skew visible


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


def test_load_trial_records_yields_unresolved_attempts_for_each_source_kind(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec)
    rc = RunConfig(condition="interactive", model=HAIKU, purpose="calibration", compiled_hash=COMPILED, brief_profile="withheld")
    done = write_trial(ws, spec, attempt=0)
    kinds = ("status-error", "status-error-rc", "runner-error", "request-only", "launch-failed")
    tids = {kind: write_unresolved(ws, spec, kind=kind, attempt=i + 1) for i, kind in enumerate(kinds)}
    stale = write_unresolved(ws, spec, kind="stale-running", attempt=9)
    recs = {r["trial_id"]: r for r in load_trial_records(ws)}
    assert set(recs) == {done, stale, *tids.values()}
    assert not recs[done].get("unresolved")
    for tid in tids.values():
        u = recs[tid]
        assert u["unresolved"] is True and u["reward"] == {} and u["report"] == {} and u["transcript"] == {}
    u = recs[tids["status-error"]]  # run.py unknown-exception path launched from the CLI: only status.json + task/task.toml
    assert (u["status"], u["error_type"], u["source_file"]) == ("error", "RuntimeError", "status.json")
    assert u["started_at"] == "2026-09-13T12:00:00Z" and u["finished_at"] == "2026-09-13T12:03:00Z"
    assert u["run_config_source"] == "task/task.toml" and u["run_config_hash"] == rc.run_config_hash
    assert u["condition"] == "interactive" and u["brief_profile"] == "withheld" and u["compiled_hash"] == COMPILED
    assert u["scenario_id"] is None and u["model"] is None and u["source_files"] == ["status.json"]
    u = recs[tids["status-error-rc"]]
    assert u["run_config_source"] == "run_config.json" and u["run_config_hash"] == rc.run_config_hash
    assert u["model"] == HAIKU and u["purpose"] == "calibration" and u["status"] == "error"
    u = recs[tids["runner-error"]]
    assert (u["status"], u["error_type"], u["source_file"]) == ("runner-error", "HarborRunError", "runner-error.json")
    assert u["run_config_source"] == "request.json" and u["run_config_hash"] == rc.run_config_hash
    assert u["scenario_id"] == spec.id and u["spec_hash"] == spec.spec_hash and u["attempt_index"] == 3
    assert u["source_files"] == ["runner-error.json", "request.json", "launch.json"] and u["started_at"] is None
    u = recs[tids["request-only"]]
    assert (u["status"], u["error_type"], u["source_file"]) == ("requested", None, "request.json") and u["model"] == HAIKU
    u = recs[tids["launch-failed"]]
    assert (u["status"], u["error_type"], u["source_file"]) == ("launch-failed", "launch-failed", "launch.json")
    u = recs[stale]
    assert (u["status"], u["error_type"], u["source_file"]) == ("running", None, "status.json")
    assert u["run_config_hash"] is None and u["scenario_id"] is None and u["started_at"] == "2026-09-13T12:00:00Z"
    # scenario attribution: request/spec id, else compiled hash (task.toml / run_config.json); the stale one is unattributable
    scoped = {r["trial_id"] for r in load_trial_records(ws, spec.id)}
    assert scoped == {done, *tids.values()}
    assert {r["trial_id"] for r in load_trial_records(ws, spec.id, include_unresolved=False)} == {done}
    other = make_spec("other-scenario")
    make_workspace(tmp_path, other)
    write_unresolved(ws, other, kind="runner-error", attempt=0)
    assert {r["trial_id"] for r in load_trial_records(ws, spec.id)} == scoped
    assert len(load_trial_records(ws)) == 8
    # an unreadable record.json is an unresolved attempt too, never a silently skipped trial
    (ws.root / "trials" / done / "record.json").write_text("{not json")
    u = {r["trial_id"]: r for r in load_trial_records(ws)}[done]
    assert u["unresolved"] is True and u["status"] == "unreadable-record" and u["source_file"] == "record.json"


def test_unresolved_attempts_never_change_metrics(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec)
    for i, (code, success, reward) in enumerate([(1, 1, 1.0), (2, 0, 0.5), (3, 0, 0.0)]):
        write_trial(ws, spec, attempt=i, outcome_code=code, success=success, reward=reward)
    before = json.loads(score_scenario(ws, spec.id, k=2).read_text())
    assert len(before) == 1
    key = next(iter(before))
    assert before[key]["attempts_total"] == 3 and before[key]["attempts_unresolved"] == 0 and before[key]["unresolved"] == []
    for i, kind in enumerate(("status-error", "runner-error", "request-only", "launch-failed", "status-error-rc")):
        write_unresolved(ws, spec, kind=kind, attempt=10 + i)
    write_unresolved(ws, spec, kind="stale-running", attempt=20)  # unattributable: not in this scenario's table
    after = json.loads(score_scenario(ws, spec.id, k=2).read_text())
    assert set(after) == {key}
    volatile = {"attempts_total", "attempts_unresolved", "unresolved_by_status", "unresolved"}
    assert {k: v for k, v in before[key].items() if k not in volatile} == {k: v for k, v in after[key].items() if k not in volatile}
    g = after[key]
    assert g["n"] == 3 and g["n_total"] == 3 and g["success_count"] == 1 and g["pass_at_k"] == pytest.approx(2 / 3)
    assert g["mean_reward"] == pytest.approx(0.5) and len(g["trial_ids"]) == 3
    assert g["attempts_total"] == 8 and g["attempts_unresolved"] == 5
    assert g["unresolved_by_status"] == {"error": 2, "launch-failed": 1, "requested": 1, "runner-error": 1}
    assert len(g["unresolved"]) == 5 and all(set(x) == {"trial_id", "status", "error_type", "source_file", "started_at", "finished_at"} for x in g["unresolved"])
    assert not (set(g["trial_ids"]) & {x["trial_id"] for x in g["unresolved"]})
    # an attributable attempt with no recoverable run config lands in the unknown-config group, with no metrics
    write_unresolved(ws, spec, kind="request-no-rc", attempt=30)
    final = json.loads(score_scenario(ws, spec.id, k=2).read_text())
    assert set(final) == {key, "unknown-config"}
    ug = final["unknown-config"]
    assert ug["n"] == 0 and ug["n_total"] == 0 and ug["success_count"] == 0 and ug["pass_at_k"] is None and ug["mean_reward"] is None
    assert ug["attempts_total"] == 1 and ug["attempts_unresolved"] == 1 and ug["unresolved_by_status"] == {"requested": 1}
    assert ug["condition"] is None and ug["model"] is None and ug["run_config_hash"] is None and ug["trial_ids"] == []
    assert {k: v for k, v in final[key].items() if k not in volatile} == {k: v for k, v in before[key].items() if k not in volatile}
    # summarize() alone: unresolved-only input, duplicate detection across kinds
    only = summarize([{"trial_id": "a" * 24, "unresolved": True, "status": "error"}], k=1)
    assert set(only) == {"unknown-config"} and only["unknown-config"]["attempts_unresolved"] == 1 and only["unknown-config"]["n"] == 0
    with pytest.raises(ValueError, match="duplicate"):
        summarize([_rec(1, 1, 1.0, trial_id="a" * 24), {"trial_id": "a" * 24, "unresolved": True, "status": "error"}], k=1)


def test_export_and_register_skip_unresolved_attempts(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec)
    done = write_trial(ws, spec, attempt=0)
    runner = write_unresolved(ws, spec, kind="runner-error", attempt=1)  # carries spec_hash via request.json
    write_unresolved(ws, spec, kind="status-error", attempt=2)  # no spec_hash anywhere
    assert [p.name for p in export_scenario(ws, spec.id)] == [f"{done}.episode.json"]
    entry = register_scenario(ws, spec)
    assert entry["calibration_runs"] == [done] and entry["comparison_runs"] == [] and entry["unresolved_runs"] == [runner]


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
    # paths are relative to the workspace root: the workspace moves between the laptop and the VM
    assert [t["path"] for t in ds["tasks"]] == [f"tasks/{spec.id}__{c}" for c in ("full", "hidden", "interactive")]
    assert all((ws.root / t["path"]) == ws.task_dir(spec.id, t["condition"]) for t in ds["tasks"])
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
    assert ln == {"spec_hash": spec.spec_hash, "spec_source": "trial-spec.json", "spec_mismatch": False,
                  "loaded_spec_hash": spec.spec_hash, "run_config_hash": rc.run_config_hash, "compiled_hash": COMPILED,
                  "image_digest": IMAGE, "purpose": "calibration", "patch_sha256": "p" * 64, "transcript_hash": "h" * 64,
                  "harbor_trial_dir": str(ws.jobs_dir / "runs" / tid / f"{spec.id}__interactive__1"), "attempt_index": 0}
    assert m["reward"]["outcome_code"] == 1 and m["simulator_validity"] == "unadjudicated" and m["training_eligible"] is False
    assert m["interaction"] == {"patience_start": 6, "patience_end": 3, "questions": 1, "reviews": 1,
                                "questions_observed_by_policy": 1, "reviews_observed_by_policy": 0}
    f = json.loads((ws.root / "exports" / f"{full}.episode.json").read_text())
    assert f["simulator_validity"] == "n/a" and f["training_eligible"] is True and f["interaction"]["questions"] == 0
    # full condition has no stakeholder: nothing is exported as a reply even though the shared fixture trajectory
    # contains an ask-user command, and the policy-observed counters stay 0
    assert f["evaluator_only"]["reviewer_label"] is None and f["policy_visible"]["stakeholder_replies"] == []
    assert f["interaction"]["questions_observed_by_policy"] == 0 and f["interaction"]["reviews_observed_by_policy"] == 0


NOISY_TRAJ = {"steps": [
    {"step_id": 1, "timestamp": "2026-09-13T10:00:00Z", "source": "user", "message": "ask-user is available."},
    {"step_id": 2, "timestamp": "2026-09-13T10:00:01Z", "source": "agent", "message": "inspect",
     "tool_calls": [{"tool_call_id": "a", "function_name": "bash", "arguments": {"command": "cat /usr/local/bin/ask-user"}}],
     "observation": {"results": [{"source_call_id": "a", "content": "#!/bin/sh\ncurl ..."}]}},
    {"step_id": 3, "timestamp": "2026-09-13T10:00:02Z", "source": "agent", "message": "grep",
     "tool_calls": [{"tool_call_id": "b", "function_name": "bash", "arguments": {"command": "grep -rn request-review README.md"}}],
     "observation": {"results": [{"source_call_id": "b", "content": "README.md:3: run request-review"}]}},
    {"step_id": 4, "timestamp": "2026-09-13T10:00:03Z", "source": "agent", "message": "help",
     "tool_calls": [{"tool_call_id": "c", "function_name": "bash", "arguments": {"command": "ask-user --help"}}],
     "observation": {"results": [{"source_call_id": "c", "content": "usage: ask-user QUESTION"}]}},
    {"step_id": 5, "timestamp": "2026-09-13T10:00:04Z", "source": "agent", "message": "ask",
     "tool_calls": [{"tool_call_id": "d", "function_name": "bash", "arguments": {"command": "cd /app && ask-user 'Which exception?'"}}],
     "observation": {"results": [{"source_call_id": "d", "content": "ValueError.\n[availability remaining: 5]"}]}},
    {"step_id": 6, "timestamp": "2026-09-13T10:00:05Z", "source": "agent", "message": "review",
     "tool_calls": [{"tool_call_id": "e", "function_name": "bash", "arguments": {"command": "/usr/local/bin/request-review"}}],
     "observation": {"results": [{"source_call_id": "e", "content": "Looks right.\n[availability remaining: 3]"}]}},
    {"step_id": 7, "timestamp": "2026-09-13T10:00:06Z", "source": "agent", "message": "mention only",
     "tool_calls": [{"tool_call_id": "f", "function_name": "bash", "arguments": {"command": "echo 'call ask-user later'; ls ask-user-notes"}}],
     "observation": {"results": [{"source_call_id": "f", "content": "call ask-user later"}]}},
]}


def test_shim_detection_is_anchored_to_the_command_token():
    from swesim.stages.export import delivered_replies, shim_invocation
    for cmd in ("cat /usr/local/bin/ask-user", "grep -rn request-review README.md", "ask-user --help", "ask-user -h",
                "echo 'call ask-user later'", "ls ask-user-notes", "ask-userx q", "", "  "):
        assert shim_invocation(cmd) is None, cmd
    assert shim_invocation("ask-user 'Which exception?'") == "ask-user"
    assert shim_invocation("cd /app && ask-user 'q'") == "ask-user"
    assert shim_invocation("/usr/local/bin/request-review") == "request-review"
    assert shim_invocation("ask-user") == "ask-user" and shim_invocation("request-review; ls") == "request-review"
    replies = delivered_replies(NOISY_TRAJ)
    assert [(r["step_id"], r["shim"]) for r in replies] == [(5, "ask-user"), (6, "request-review")]
    assert replies[0]["observation"] == "ValueError.\n[availability remaining: 5]" and replies[1]["observation"].startswith("Looks right.")
    assert delivered_replies({}) == [] and delivered_replies({"steps": [{"source": "user", "message": "ask-user q"}]}) == []


def test_export_counts_only_real_shim_invocations_and_only_when_interactive(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec)
    inter = write_trial(ws, spec, condition="interactive", trajectory=NOISY_TRAJ, events=ask_events(T0 + 4, "Which exception?", "ValueError.", ["f01"], 5))
    hidden = write_trial(ws, spec, condition="hidden", trajectory=NOISY_TRAJ)
    export_scenario(ws, spec.id)
    read = lambda t: json.loads((ws.root / "exports" / f"{t}.episode.json").read_text())
    m = read(inter)
    assert [r["step_id"] for r in m["policy_visible"]["stakeholder_replies"]] == [5, 6]
    assert m["interaction"]["questions_observed_by_policy"] == 1 and m["interaction"]["reviews_observed_by_policy"] == 1
    assert m["interaction"]["questions"] == 1  # service-side count reported alongside, not merged
    hm = read(hidden)
    assert hm["policy_visible"]["stakeholder_replies"] == [] and hm["interaction"]["questions_observed_by_policy"] == 0
    assert hm["policy_visible"]["agent_trajectory"]["steps"][4]["message"] == "ask"  # trajectory itself is still exported


def test_export_lineage_spec_hash_comes_from_record_and_flags_mismatch(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec)
    frozen = write_trial(ws, spec, condition="full", attempt=0)
    fallback = write_trial(ws, spec, condition="full", attempt=1)
    (ws.root / "trials" / fallback / "spec.json").unlink()  # no frozen spec: export must use scenario.json
    changed = make_spec(with_author_only=True)  # scenario.json is now a newer version of the scenario
    (ws.scenario_dir(spec.id) / "scenario.json").write_text(changed.model_dump_json())
    export_scenario(ws, spec.id)
    read = lambda t: json.loads((ws.root / "exports" / f"{t}.episode.json").read_text())
    fz = read(frozen)
    assert fz["lineage"]["spec_source"] == "trial-spec.json" and fz["lineage"]["spec_mismatch"] is False
    assert fz["lineage"]["spec_hash"] == spec.spec_hash == fz["lineage"]["loaded_spec_hash"] and fz["training_eligible"] is True
    fb = read(fallback)
    assert fb["lineage"]["spec_hash"] == spec.spec_hash  # the record's hash, never the loaded spec's
    assert fb["lineage"]["spec_source"] == "scenario.json" and fb["lineage"]["spec_mismatch"] is True
    assert fb["lineage"]["loaded_spec_hash"] == changed.spec_hash and fb["training_eligible"] is False
    assert fb["evaluator_only"]["author_only_fact_ids"] == ["f09"]  # what was actually loaded is visible, and flagged
    # matching scenario.json: fallback without mismatch
    (ws.scenario_dir(spec.id) / "scenario.json").write_text(spec.model_dump_json())
    export_scenario(ws, spec.id)
    assert read(fallback)["lineage"]["spec_mismatch"] is False and read(fallback)["training_eligible"] is True
    # a record without a frozen spec hash cannot be exported
    p = ws.root / "trials" / fallback / "record.json"
    rec = json.loads(p.read_text())
    rec.pop("spec_hash")
    p.write_text(json.dumps(rec))
    with pytest.raises(ValueError, match="lineage"):
        export_scenario(ws, spec.id)


def test_export_infrastructure_outcomes_are_never_training_eligible(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec)
    collection = write_trial(ws, spec, condition="full", attempt=0, outcome_code=5, success=0, reward=0.0)
    simulator = write_trial(ws, spec, condition="hidden", attempt=0, outcome_code=6, success=0, reward=0.0)
    evaluator = write_trial(ws, spec, condition="full", attempt=1, outcome_code=7, success=0, reward=0.0)
    wrong = write_trial(ws, spec, condition="full", attempt=2, outcome_code=2, success=0, reward=0.0)
    timeout = write_trial(ws, spec, condition="hidden", attempt=1, outcome_code=3, success=0, reward=0.0)
    export_scenario(ws, spec.id)
    read = lambda t: json.loads((ws.root / "exports" / f"{t}.episode.json").read_text())
    for t in (collection, simulator, evaluator):
        assert read(t)["training_eligible"] is False and read(t)["policy_visible"]["agent_trajectory"]["steps"]
    assert read(wrong)["training_eligible"] is True and read(timeout)["training_eligible"] is True  # agent-attributable failures are signal


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
    read = lambda t: json.loads((ws.root / "exports" / f"{t}.episode.json").read_text())
    assert read(no_traj)["training_eligible"] is False and read(no_traj)["policy_visible"]["agent_trajectory"] == {}
    assert read(no_traj)["policy_visible"]["stakeholder_replies"] == []
    assert read(unbound)["training_eligible"] is False
    assert read(adjudicated)["training_eligible"] is True and read(adjudicated)["simulator_validity"] == "ok"


def test_export_ignores_other_scenarios_and_handles_no_trials(tmp_path):
    spec = make_spec()
    ws = make_workspace(tmp_path, spec)
    assert export_scenario(ws, spec.id) == []
    assert (ws.root / "exports").is_dir()
