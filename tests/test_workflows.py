"""Offline, deterministic tests for the Temporal orchestration stream (Tasks 11 and 13).

No Modal, no Harbor, no LLM, no network. Stage modules owned by other streams are
replaced with fake modules in sys.modules; systemctl/systemd-run are replaced with a
fake runner. Temporal's time-skipping test server is used when it can start; otherwise
the local dev server on PATH; otherwise those tests skip with an explicit reason.
"""
from __future__ import annotations

import asyncio
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
from temporalio.common import WorkflowIDReusePolicy

from swesim.models import RunConfig, ScenarioSpec, minimal_spec_dict, trial_id
from swesim.paths import Workspace, atomic_json
from swesim.workflows import activities as A
from swesim.workflows import workflows as W

ROOT = Path(__file__).resolve().parents[1]
BIG = {"candidates_path": "c", "judged_path": "j", "instance_ids": ["a"], "scenario_id": "aaa", "draft_path": "d",
       "status": "valid", "failures": [], "trials": [], "spec_hash": "h"}


# ---------------------------------------------------------------- brief's three tests

def test_approval_wait_none_is_success(monkeypatch):
    async def scenario():
        w = W.PipelineWorkflow(); calls = []
        async def act(name, p, minutes=10):
            calls.append(name); return dict(BIG)
        async def wait(predicate, timeout):
            w.approve_spec({"scenario_id": "aaa", "spec_hash": "h"}); assert predicate(); return None
        monkeypatch.setattr(W, "act", act); monkeypatch.setattr(W.workflow, "wait_condition", wait)
        result = await w.run({"approval_mode": "human"})
        assert result["scenarios"][0]["status"] == "complete" and "verify_approval_activity" in calls and "validate_activity" in calls
    asyncio.run(scenario())


@pytest.mark.parametrize("approved", [True, False])
def test_auto_approval_never_waits_and_rejection_stops_validation(monkeypatch, approved):
    async def scenario():
        w = W.PipelineWorkflow(); calls = []
        async def act(name, p, minutes=10):
            calls.append(name)
            if name == "auto_approve_activity":
                return {"approved": approved, "spec_hash": "h", "reasons": [] if approved else ["gate rejected"]}
            return dict(BIG)
        async def wait(*args, **kwargs):
            raise AssertionError("automatic approval must not wait for a person")
        monkeypatch.setattr(W, "act", act); monkeypatch.setattr(W.workflow, "wait_condition", wait)
        result = await w.run({})
        assert "auto_approve_activity" in calls
        assert ("verify_approval_activity" in calls) == approved
        assert ("validate_activity" in calls) == approved
        assert ("register_activity" in calls) == approved
        expected = "complete" if approved else "auto-approval-rejected"
        assert result["scenarios"][0]["status"] == expected
    asyncio.run(scenario())


def test_matrix_uses_precomputed_distinct_model_ids(monkeypatch):
    async def scenario():
        ids = []
        async def child(fn, p, **kw):
            ids.append(kw["id"]); assert kw["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
            return {"trial_id": kw["id"]}
        async def act(*args, **kw): return {}
        monkeypatch.setattr(W.workflow, "execute_child_workflow", child); monkeypatch.setattr(W, "act", act)
        await W.RunMatrixWorkflow().run({"scenario_id": "aaa", "trials": [{"trial_id": "model-a"}, {"trial_id": "model-b"}]})
        assert ids == ["model-a", "model-b"]
    asyncio.run(scenario())


# ---------------------------------------------------------------- more pipeline behaviour (direct, no Temporal)

def test_signal_mode_verifies_the_signalled_hash_and_times_out_cleanly(monkeypatch):
    async def scenario():
        w = W.PipelineWorkflow(); seen = {}
        async def act(name, p, minutes=10):
            if name == "verify_approval_activity": seen.update(p)
            return dict(BIG)
        async def wait(predicate, timeout):
            w.approve_spec({"scenario_id": "aaa", "spec_hash": "signalled"}); return None
        monkeypatch.setattr(W, "act", act); monkeypatch.setattr(W.workflow, "wait_condition", wait)
        await w.run({"approval_mode": "signal"})
        assert seen == {"scenario_id": "aaa", "spec_hash": "signalled"}
        # Timeout: no boolean is read; the TimeoutError is the only signal of a missing approval.
        w2 = W.PipelineWorkflow()
        async def wait_timeout(predicate, timeout): raise TimeoutError()
        monkeypatch.setattr(W.workflow, "wait_condition", wait_timeout)
        r = await w2.run({"approval_mode": "signal"})
        assert r["status"] == "awaiting-approval-timeout" and r["pending"]["scenario_id"] == "aaa"
        assert w2.progress()["stage"] == "awaiting-approval"
    asyncio.run(scenario())


def test_unknown_approval_mode_fails_fast_instead_of_waiting(monkeypatch):
    from temporalio.exceptions import ApplicationError
    async def scenario():
        async def act(name, p, minutes=10): raise AssertionError("no activity may run with an invalid approval mode")
        monkeypatch.setattr(W, "act", act)
        with pytest.raises(ApplicationError, match="approval_mode"): await W.PipelineWorkflow().run({"approval_mode": "atuo"})
    asyncio.run(scenario())


def test_invalid_validation_skips_register_and_trials(monkeypatch):
    async def scenario():
        w = W.PipelineWorkflow(); calls = []
        async def act(name, p, minutes=10):
            calls.append(name)
            if name == "auto_approve_activity": return {"approved": True, "spec_hash": "h", "reasons": []}
            if name == "validate_activity": return {"status": "invalid", "failures": ["oracle earned 0"]}
            return dict(BIG)
        monkeypatch.setattr(W, "act", act)
        r = await w.run({})
        assert r["scenarios"][0]["status"] == "invalid" and "register_activity" not in calls and "plan_trials_activity" not in calls
    asyncio.run(scenario())


def test_auto_rejection_for_withholding_leak_redrafts_once_then_moves_on(monkeypatch):
    async def scenario():
        w = W.PipelineWorkflow(); calls = []
        async def act(name, p, minutes=10):
            calls.append((name, p.get("force", False)))
            if name == "auto_approve_activity": return {"approved": False, "spec_hash": "h", "reasons": ["withheld brief still contains excerpt of f01"]}
            if name == "judge_activity": return {"judged_path": "j", "instance_ids": ["a", "b", "c", "d"]}
            return dict(BIG)
        monkeypatch.setattr(W, "act", act)
        r = await w.run({"max_scenarios": 1, "max_candidates_tried": 2})
        drafts = [f for n, f in calls if n == "draft_activity"]
        assert drafts == [False, True, False, True]          # two candidates tried, one re-draft each
        assert [s["status"] for s in r["scenarios"]] == ["auto-approval-rejected"] * 2
    asyncio.run(scenario())


@pytest.mark.parametrize("reasons,gate,redraft", [
    (["withheld brief still contains excerpt of f01"], None, True),      # the approve stage's actual wording
    (["withholding: removed excerpt visible"], None, True),
    (["Brief leak detected in profile withheld"], None, True),
    (["judge did not accept (no non-derivable gap)"], None, False),
    (["controls incomplete"], None, False),
    (["controls incomplete"], "withholding-leak", True),                 # structured gate wins over the text
])
def test_redraft_trigger_is_structured(monkeypatch, reasons, gate, redraft):
    # Fix round 1, Minor 7.
    async def scenario():
        w = W.PipelineWorkflow(); calls = []
        async def act(name, p, minutes=10):
            calls.append(name)
            if name == "auto_approve_activity":
                out = {"approved": False, "spec_hash": "h", "reasons": reasons}
                if gate: out["gate"] = gate
                return out
            return dict(BIG)
        monkeypatch.setattr(W, "act", act)
        await w.run({"max_candidates_tried": 1})
        assert calls.count("draft_activity") == (2 if redraft else 1)
    asyncio.run(scenario())
    assert W._is_leak_reason("Withholding leak") and not W._is_leak_reason("judge rejected")


def test_pipeline_result_counts_and_failure_status(monkeypatch):
    # Fix round 1, Minor 8.
    async def scenario():
        async def act(name, p, minutes=10):
            if name == "judge_activity": return {"judged_path": "j", "instance_ids": ["a", "b", "c"]}
            if name == "draft_activity": return {"scenario_id": p["instance_id"], "draft_path": "d"}
            if name == "auto_approve_activity": return {"approved": p["scenario_id"] != "a", "spec_hash": "h", "reasons": ["judge did not accept"]}
            if name == "verify_approval_activity": return {"scenario_id": p["scenario_id"], "spec_hash": p["spec_hash"]}
            if name == "validate_activity": return {"status": "invalid" if p["scenario_id"] == "b" else "valid", "failures": ["oracle earned 0"]}
            return dict(BIG)
        monkeypatch.setattr(W, "act", act)
        r = await W.PipelineWorkflow().run({"max_scenarios": 1, "max_candidates_tried": 3})
        assert (r["tried"], r["completed"], r["rejected"], r["invalid"]) == (3, 1, 1, 1) and r["status"] == "complete"
        assert [s["status"] for s in r["scenarios"]] == ["auto-approval-rejected", "invalid", "complete"]
        r2 = await W.PipelineWorkflow().run({"max_scenarios": 1, "max_candidates_tried": 1})
        assert r2["status"] == "complete-with-failures" and (r2["tried"], r2["completed"], r2["rejected"]) == (1, 0, 1)
    asyncio.run(scenario())


def test_solver_unavailable_registers_but_launches_nothing(monkeypatch):
    # Fix round 1, Minor 9.
    async def scenario():
        w = W.PipelineWorkflow(); calls = []
        async def act(name, p, minutes=10):
            calls.append(name)
            if name == "probe_models_activity":
                return {"substitutions": {}, "solver_unavailable": ["openai/gpt-5.5"], "needs_human": ["solver model openai/gpt-5.5 unavailable; no silent substitution"]}
            if name == "auto_approve_activity": return {"approved": True, "spec_hash": "h", "reasons": []}
            return dict(BIG)
        async def child(*a, **k): raise AssertionError("no trial may launch with an unavailable solver")
        monkeypatch.setattr(W, "act", act); monkeypatch.setattr(W.workflow, "execute_child_workflow", child)
        r = await w.run({"models": ["openai/gpt-5.5"]})
        s = r["scenarios"][0]
        assert s["status"] == "complete-no-trials" and s["solver_unavailable"] == ["openai/gpt-5.5"] and s["needs_human"]
        assert "register_activity" in calls and "plan_trials_activity" not in calls and "score_activity" not in calls
        assert r["completed"] == 1 and r["status"] == "complete"
    asyncio.run(scenario())


def test_probe_substitutions_flow_into_later_activities(monkeypatch):
    async def scenario():
        w = W.PipelineWorkflow(); seen = {}
        async def act(name, p, minutes=10):
            if name == "probe_models_activity": return {"substitutions": {"judge_model": "anthropic/claude-sonnet-5"}}
            if name == "judge_activity": seen["judge_model"] = p.get("judge_model")
            if name == "auto_approve_activity": return {"approved": False, "spec_hash": "h", "reasons": ["x"]}
            return dict(BIG)
        monkeypatch.setattr(W, "act", act)
        await w.run({"judge_model": "openai/gpt-5.5"})
        assert seen["judge_model"] == "anthropic/claude-sonnet-5"
    asyncio.run(scenario())


def test_matrix_records_duplicate_rejection_and_still_scores(monkeypatch):
    from temporalio.exceptions import WorkflowAlreadyStartedError
    async def scenario():
        acts = []
        async def child(fn, p, **kw):
            if p["trial_id"] == "dup": raise WorkflowAlreadyStartedError("dup", "TrialWorkflow", run_id=None)
            return {"trial_id": p["trial_id"], "status": "completed"}
        async def act(name, p, minutes=10): acts.append(name); return {}
        monkeypatch.setattr(W.workflow, "execute_child_workflow", child); monkeypatch.setattr(W, "act", act)
        r = await W.RunMatrixWorkflow().run({"scenario_id": "aaa", "trials": [{"trial_id": "dup"}, {"trial_id": "ok"}]})
        assert [t["status"] for t in r["trials"]] == ["duplicate-rejected", "completed"]
        assert acts == ["score_activity", "export_activity"]
    asyncio.run(scenario())


def test_trial_workflow_launch_failure_is_swallowed_and_await_decides(monkeypatch):
    async def scenario():
        calls = []
        async def act(name, p, minutes=10):
            calls.append(name); raise RuntimeError("launch requires reconciliation")
        async def execute_activity(name, p, **kw):
            calls.append(name); return {"trial_id": p["trial_id"], "status": "launch-failed"}
        monkeypatch.setattr(W, "act", act); monkeypatch.setattr(W.workflow, "execute_activity", execute_activity)
        r = await W.TrialWorkflow().run({"trial_id": "t1"})
        assert calls == ["launch_trial_activity", "await_trial_activity"] and r["status"] == "launch-failed"
    asyncio.run(scenario())


# ---------------------------------------------------------------- fixtures for activities

SCRIPT_WRITE_RECORD = (
    "import json,sys,pathlib,time\n"
    "req=pathlib.Path(sys.argv[1]); p=json.loads(req.read_text()); d=req.parent\n"
    "(d/'status.json').write_text(json.dumps({'status':'running','trial_id':p['trial_id']}))\n"
    "time.sleep(0.2)\n"
    "(d/'record.json').write_text(json.dumps({'trial_id':p['trial_id'],'status':'completed','reward':{'reward':1.0,'success':1}}))\n"
    "(d/'status.json').write_text(json.dumps({'status':'completed','trial_id':p['trial_id']}))\n")


class FakeSystemd:
    """Fake `systemctl --user show` and `systemd-run --user`. Units live in .units."""
    def __init__(self, run_returncode=0):
        self.units: dict[str, dict] = {}; self.calls: list[list[str]] = []; self.run_returncode = run_returncode
        self.systemctl_failure = None; self.systemctl_rc = 0; self.systemctl_stdout_override = None
    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "systemctl":
            if self.systemctl_failure: return subprocess.CompletedProcess(argv, 1, "", self.systemctl_failure)
            unit = next(a for a in argv[3:] if not a.startswith("--"))
            st = self.units.get(unit, {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead", "Result": "success"})
            stdout = self.systemctl_stdout_override if self.systemctl_stdout_override is not None else "".join(f"{k}={v}\n" for k, v in st.items())
            return subprocess.CompletedProcess(argv, self.systemctl_rc, stdout, "")
        if argv[0] == "systemd-run":
            unit = next(a for a in argv if a.startswith("--unit=")).split("=", 1)[1]
            if self.run_returncode: return subprocess.CompletedProcess(argv, self.run_returncode, "", "Failed to start transient service unit")
            self.units[unit] = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success"}
            return subprocess.CompletedProcess(argv, 0, "", f"Running as unit: {unit}\n")
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    w = Workspace(tmp_path / "workspace", tmp_path / "registry.json")
    monkeypatch.setattr(A, "WS", w); monkeypatch.setattr(A, "LAUNCHER", "systemd")
    monkeypatch.setattr(A, "POLL_SECONDS", 0.0); monkeypatch.setattr(A, "LAUNCH_GRACE_POLLS", 2)
    monkeypatch.setattr(A, "REPO_ROOT", tmp_path)
    return w


@pytest.fixture
def systemd(monkeypatch):
    fake = FakeSystemd(); monkeypatch.setattr(A, "RUN", fake); return fake


def frozen_scenario(ws: Workspace, sid="example-scenario", compiled_hash="c" * 64):
    spec = ScenarioSpec.model_validate(minimal_spec_dict()); sdir = ws.scenario_dir(sid); sdir.mkdir(parents=True)
    atomic_json(sdir / "scenario.json", spec.model_dump(mode="json"))
    atomic_json(sdir / "approval.json", {"spec_hash": spec.spec_hash, "approved_by": "auto-validators", "review_needed": True})
    atomic_json(sdir / "compile.json", {"compiled_hash": compiled_hash, "spec_hash": spec.spec_hash, "image_digest": "sha256:abc"})
    return spec


def trial_request(ws, spec, model="anthropic/claude-haiku-4-5-20251001", condition="interactive", attempt=0):
    rc = RunConfig(condition=condition, model=model, compiled_hash="c" * 64, brief_profile="natural")
    tid = trial_id(spec.spec_hash, condition, rc.run_config_hash, attempt)
    return dict(trial_id=tid, scenario_id=spec.id, spec_hash=spec.spec_hash, condition=condition, model=model,
                run_config=rc.model_dump(mode="json"), run_config_hash=rc.run_config_hash, attempt_index=attempt, stakeholder_url="https://stake.example")


# ---------------------------------------------------------------- pure decision function

def ev(**kw):
    base = dict(has_record=False, has_runner_error=False, status=None, launch_phase=None, launch_returncode=None,
                load_state="not-found", active_state="inactive", sub_state="dead")
    base.update(kw); return A.TrialEvidence(**base)


@pytest.mark.parametrize("evidence,decision", [
    (ev(), "launch"),
    (ev(has_record=True), "ingest"),
    (ev(has_record=True, load_state="loaded", active_state="active", sub_state="running"), "ingest"),
    (ev(status="completed"), "ingest"),
    (ev(has_runner_error=True), "runner-error"),
    (ev(load_state="loaded", active_state="active", sub_state="running"), "reconcile"),
    (ev(load_state="loaded", active_state="failed", sub_state="failed"), "reconcile"),
    (ev(load_state="loaded", active_state="active", sub_state="exited"), "reconcile"),
    (ev(launch_phase="intent"), "unresolved"),
    (ev(launch_phase="acknowledged", launch_returncode=0), "unresolved"),
    (ev(status="running"), "unresolved"),
    (ev(launch_phase="acknowledged", launch_returncode=1), "launch-failed"),
    (ev(launch_phase="failed"), "launch-failed"),
])
def test_decide_launch(evidence, decision):
    assert A.decide_launch(evidence) == decision


def test_decision_never_launches_when_any_launch_evidence_exists():
    for phase in ("intent", "acknowledged", "failed"):
        for load in ("loaded", "not-found"):
            assert A.decide_launch(ev(launch_phase=phase, load_state=load)) != "launch"


# ---------------------------------------------------------------- launch once, then reconcile (fake systemd)

def test_launch_once_then_reconcile_never_runs_systemd_run_twice(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec)
    first = A.launch_once(p)
    assert first["decision"] == "launch" and first["launched"] is True and first["unit"] == "swesim-trial-" + p["trial_id"]
    runs = [c for c in systemd.calls if c[0] == "systemd-run"]
    assert len(runs) == 1
    argv = runs[0]
    assert argv[:2] == ["systemd-run", "--user"] and f"--unit=swesim-trial-{p['trial_id']}" in argv and "--property=RemainAfterExit=yes" in argv
    assert "--collect" not in argv                     # a failed unit must stay visible for reconciliation
    launch = json.loads((ws.root / "trials" / p["trial_id"] / "launch.json").read_text())
    assert launch["phase"] == "acknowledged" and launch["returncode"] == 0 and launch["launcher"] == "systemd"
    request = json.loads((ws.root / "trials" / p["trial_id"] / "request.json").read_text())
    assert request == p
    second = A.launch_once(p)
    assert second["decision"] == "reconcile" and second["launched"] is False
    assert len([c for c in systemd.calls if c[0] == "systemd-run"]) == 1


def test_launch_argv_carries_paths_only_never_secret_values(ws, systemd, monkeypatch):
    # Fake values deliberately do not match real credential prefixes (verify.sh scans tracked files for those).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-value-123456"); monkeypatch.setenv("SWESIM_WORKER_SECRET", "fake-worker-secret-xyz")
    monkeypatch.setenv("GH_TOKEN", "fake-github-token-value"); monkeypatch.setenv("SWESIM_ENV_FILE", str(ws.root.parent / "env"))
    spec = frozen_scenario(ws); p = trial_request(ws, spec); A.launch_once(p)
    argv = next(c for c in systemd.calls if c[0] == "systemd-run"); joined = " ".join(argv)
    for secret in ("fake-anthropic-value-123456", "fake-worker-secret-xyz", "fake-github-token-value"):
        assert secret not in joined
    assert f"--setenv=SWESIM_ENV_FILE={ws.root.parent / 'env'}" in argv and f"--setenv=SWESIM_WORKSPACE={ws.root}" in argv
    # the command inside the unit is the trial runner module with the request path, no inline code
    assert "-m" in argv and "swesim.workflows.activities" in argv and argv[-1].endswith("request.json") and "-c" not in argv


def test_launch_failure_is_recorded_and_raised_not_retried(ws, monkeypatch):
    fake = FakeSystemd(run_returncode=1); monkeypatch.setattr(A, "RUN", fake)
    spec = frozen_scenario(ws); p = trial_request(ws, spec)
    with pytest.raises(A.LaunchFailed): A.launch_once(p)
    launch = json.loads((ws.root / "trials" / p["trial_id"] / "launch.json").read_text())
    assert launch["phase"] == "acknowledged" and launch["returncode"] == 1 and "Failed" in launch["stderr"]
    # a second call reconciles the failed launch rather than launching again
    assert A.launch_once(p)["decision"] == "launch-failed" and len([c for c in fake.calls if c[0] == "systemd-run"]) == 1


def test_launch_intent_without_unit_is_unresolved_never_relaunched(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]
    atomic_json(d / "request.json", p); atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "unit": "swesim-trial-" + p["trial_id"], "phase": "intent", "launcher": "systemd"})
    r = A.launch_once(p)
    assert r["decision"] == "unresolved" and not [c for c in systemd.calls if c[0] == "systemd-run"]


def test_request_binding_conflict_is_rejected(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]
    atomic_json(d / "request.json", {**p, "model": "someone-else"})
    with pytest.raises(A.TrialBindingConflict): A.launch_once(p)
    assert not [c for c in systemd.calls if c[0] == "systemd-run"]


def test_existing_record_is_ingested_without_launch(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]
    atomic_json(d / "record.json", {"trial_id": p["trial_id"], "status": "completed", "reward": {"reward": 0.5, "success": 0}})
    assert A.launch_once(p)["decision"] == "ingest" and not [c for c in systemd.calls if c[0] == "systemd-run"]


# ---------------------------------------------------------------- await_trial_activity outcomes

def test_await_returns_record_when_present(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]
    atomic_json(d / "record.json", {"trial_id": p["trial_id"], "status": "completed", "reward": {"reward": 1.0, "success": 1}})
    r = asyncio.run(A.await_trial_activity(p))
    assert r == {"trial_id": p["trial_id"], "status": "completed", "reward": {"reward": 1.0, "success": 1}}


def test_await_reports_runner_error_launch_failed_and_unresolved(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]
    atomic_json(d / "runner-error.json", {"trial_id": p["trial_id"], "error": "ValueError('frozen spec changed')"})
    assert asyncio.run(A.await_trial_activity(p))["status"] == "runner-error"
    (d / "runner-error.json").unlink()
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "unit": "u", "phase": "acknowledged", "returncode": 1, "launcher": "systemd"})
    assert asyncio.run(A.await_trial_activity(p))["status"] == "launch-failed"
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "unit": "u", "phase": "acknowledged", "returncode": 0, "launcher": "systemd"})
    r = asyncio.run(A.await_trial_activity(p))
    assert r["status"] == "unresolved" and "never relaunch" in r["reason"]


def test_await_with_no_launch_evidence_reports_launch_failed_after_grace(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec)
    r = asyncio.run(A.await_trial_activity(p))
    assert r["status"] == "launch-failed" and "no launch evidence" in r["reason"]


def test_await_polls_running_unit_then_ingests(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]
    unit = "swesim-trial-" + p["trial_id"]
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "unit": unit, "phase": "acknowledged", "returncode": 0, "launcher": "systemd"})
    systemd.units[unit] = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success"}
    polls = {"n": 0}
    real_read = A.read_evidence
    def read_evidence(tid):
        polls["n"] += 1
        if polls["n"] == 3:
            atomic_json(d / "record.json", {"trial_id": tid, "status": "completed", "reward": {"reward": 0.0, "success": 0}})
            systemd.units[unit]["SubState"] = "exited"
        return real_read(tid)
    A.read_evidence = read_evidence
    try: r = asyncio.run(A.await_trial_activity(p))
    finally: A.read_evidence = real_read
    assert r["status"] == "completed" and polls["n"] == 3


def test_await_unit_failed_without_record_is_unit_failed(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]; unit = "swesim-trial-" + p["trial_id"]
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "unit": unit, "phase": "acknowledged", "returncode": 0, "launcher": "systemd"})
    systemd.units[unit] = {"LoadState": "loaded", "ActiveState": "failed", "SubState": "failed", "Result": "exit-code"}
    r = asyncio.run(A.await_trial_activity(p))
    assert r["status"] == "unit-failed" and r["unit"] == unit


def test_await_terminal_status_without_record_returns_status_unless_unit_still_running(ws, systemd):
    # Fix round 1, Important 1: a terminal status.json with no record.json must not poll to the 3 h deadline.
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]; unit = "swesim-trial-" + p["trial_id"]
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "unit": unit, "phase": "acknowledged", "returncode": 0, "launcher": "systemd"})
    atomic_json(d / "status.json", {"status": "launch-failed", "trial_id": p["trial_id"]})
    r = asyncio.run(A.await_trial_activity(p))                       # unit gone: the terminal status is the answer, now
    assert r["status"] == "launch-failed" and r["reward"] == {} and "record.json" in r["reason"]
    atomic_json(d / "status.json", {"status": "completed", "trial_id": p["trial_id"]})
    systemd.units[unit] = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success"}
    polls = {"n": 0}; real_read = A.read_evidence
    def read_evidence(tid):                                           # unit still running: keep polling until the record lands
        polls["n"] += 1
        if polls["n"] == 3: atomic_json(d / "record.json", {"trial_id": tid, "status": "completed", "reward": {"reward": 1.0, "success": 1}})
        return real_read(tid)
    A.read_evidence = read_evidence
    try: r = asyncio.run(A.await_trial_activity(p))
    finally: A.read_evidence = real_read
    assert r["status"] == "completed" and r["reward"]["success"] == 1 and polls["n"] == 3
    assert not hasattr(A, "TERMINAL_STATUSES")


def test_systemctl_failure_is_retryable_error_not_unit_gone(ws, systemd):
    # Fix round 1, Important 2: a failing systemctl must never read as "unit not found".
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]; unit = "swesim-trial-" + p["trial_id"]
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "unit": unit, "phase": "acknowledged", "returncode": 0, "launcher": "systemd"})
    systemd.systemctl_failure = "Failed to connect to bus: No medium found"
    with pytest.raises(A.UnitStateUnavailable, match="Failed to connect to bus"): A.read_evidence(p["trial_id"])
    with pytest.raises(A.UnitStateUnavailable): asyncio.run(A.await_trial_activity(p))
    with pytest.raises(A.UnitStateUnavailable): A.launch_once(p)      # launch never guesses either
    assert not [c for c in systemd.calls if c[0] == "systemd-run"]
    systemd.systemctl_failure = None
    systemd.systemctl_rc = 1                                          # nonzero exit but an explicit not-found is trusted
    assert A.systemd_unit_state(unit)["load_state"] == "not-found"
    systemd.systemctl_rc = 0; systemd.systemctl_stdout_override = "Result=success\n"
    with pytest.raises(A.UnitStateUnavailable): A.systemd_unit_state(unit)   # LoadState missing from the output


def test_register_activity_result_is_json_serializable(ws, monkeypatch):
    # Fix round 1, Important 5.
    spec = frozen_scenario(ws)
    def register_scenario(ws_, spec_): return {"id": spec_.id, "validation_path": Path("/x/validation.json"), "runs": [], "nested": {"p": Path("/y")}}
    monkeypatch.setitem(sys.modules, "swesim.stages.register", fake_module("swesim.stages.register", register_scenario=register_scenario))
    r = asyncio.run(A.register_activity({"scenario_id": spec.id}))
    assert r == {"registry_entry": {"id": spec.id, "validation_path": "/x/validation.json", "runs": [], "nested": {"p": "/y"}}}
    json.dumps(r)


# ---------------------------------------------------------------- subprocess launcher (non-production local smoke)

def test_subprocess_launcher_end_to_end_and_reconcile(ws, monkeypatch):
    monkeypatch.setattr(A, "LAUNCHER", "subprocess")
    monkeypatch.setattr(A, "build_trial_command", lambda request_path: [sys.executable, "-c", SCRIPT_WRITE_RECORD, str(request_path)])
    spec = frozen_scenario(ws); p = trial_request(ws, spec)
    first = A.launch_once(p)
    assert first["launched"] is True and json.loads((ws.root / "trials" / p["trial_id"] / "launch.json").read_text())["launcher"] == "subprocess"
    r = asyncio.run(A.await_trial_activity(p))
    assert r["status"] == "completed" and r["reward"]["success"] == 1
    assert A.launch_once(p)["decision"] == "ingest"


# ---------------------------------------------------------------- plan_trials, verify_approval, judge, probe

def test_plan_trials_distinct_ids_per_model_and_profile_rule(ws):
    spec = frozen_scenario(ws)
    out = A.plan_trials({"scenario_id": spec.id, "models": ["anthropic/claude-haiku-4-5-20251001", "openai/gpt-5.5"], "conditions": ["full", "hidden"],
                         "k": 2, "purpose": "comparison", "brief_profile": "withheld", "stakeholder_url": "https://s"})
    trials = out["trials"]; assert len(trials) == 8 and len({t["trial_id"] for t in trials}) == 8
    for t in trials:
        rc = RunConfig.model_validate(t["run_config"])
        assert rc.compiled_hash == "c" * 64 and rc.purpose == "comparison" and rc.model == t["model"]
        assert rc.brief_profile == "natural"                   # minimal spec has no withheld brief; full always natural
        assert t["trial_id"] == trial_id(spec.spec_hash, t["condition"], rc.run_config_hash, t["attempt_index"])
        assert t["run_config_hash"] == rc.run_config_hash
    assert {t["model"] for t in trials} == {"anthropic/claude-haiku-4-5-20251001", "openai/gpt-5.5"}
    with pytest.raises(ValueError): A.plan_trials({"scenario_id": spec.id, "models": ["m", "m"], "stakeholder_url": "https://s"})
    # A manual retry is a new attempt index (new trial id), never a Temporal retry of the old one.
    more = A.plan_trials({"scenario_id": spec.id, "models": ["m"], "conditions": ["hidden"], "k": 2, "attempt_offset": 3, "stakeholder_url": "https://s"})["trials"]
    assert [t["attempt_index"] for t in more] == [3, 4] and not {t["trial_id"] for t in more} & {t["trial_id"] for t in trials}


def test_verify_approval_matches_frozen_spec_hash(ws):
    spec = frozen_scenario(ws)
    r = asyncio.run(A.verify_approval_activity({"scenario_id": spec.id, "spec_hash": spec.spec_hash}))
    assert r == {"scenario_id": spec.id, "spec_hash": spec.spec_hash}
    with pytest.raises(Exception, match="approval hash differs"): asyncio.run(A.verify_approval_activity({"scenario_id": spec.id, "spec_hash": "0" * 64}))
    (ws.scenario_dir(spec.id) / "approval.json").unlink()
    with pytest.raises(Exception, match="approval.json"): asyncio.run(A.verify_approval_activity({"scenario_id": spec.id, "spec_hash": spec.spec_hash}))


def fake_module(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items(): setattr(m, k, v)
    return m


def test_judge_activity_is_idempotent_and_filters_keep(ws, monkeypatch):
    calls = []
    def judge_candidate(row, model_id):
        calls.append((row["instance_id"], model_id)); return {"gaps": [], "verified_rubric_score": 2, "keep": row["instance_id"] == "a__1"}
    monkeypatch.setitem(sys.modules, "swesim.stages.judge", fake_module("swesim.stages.judge", judge_candidate=judge_candidate))
    cand = ws.candidates_dir / "swebench-annotated.jsonl"; cand.parent.mkdir(parents=True)
    cand.write_text("".join(json.dumps({"instance_id": i}) + "\n" for i in ("a__1", "b__2", "c__3")))
    r = asyncio.run(A.judge_activity({"candidates_path": str(cand), "judge_model": "openai/gpt-5.5", "max_candidates": 2}))
    assert r["instance_ids"] == ["a__1"] and Path(r["judged_path"]).name == "swebench-annotated.judged.jsonl" and len(calls) == 2
    r2 = asyncio.run(A.judge_activity({"candidates_path": str(cand), "judge_model": "openai/gpt-5.5"}))
    assert r2 == r and len(calls) == 2                       # retained output; no second LLM pass


def test_draft_activity_derives_scenario_id_and_supports_forced_redraft(ws, monkeypatch):
    drafted = []
    def draft(row, judged, model_id, out_dir, **kw):
        drafted.append((row["instance_id"], model_id, kw)); out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "scenario.draft.json").write_text("{}"); return out_dir / "scenario.draft.json"
    monkeypatch.setitem(sys.modules, "swesim.stages.draft_spec", fake_module("swesim.stages.draft_spec", draft=draft))
    judged = ws.candidates_dir / "x.judged.jsonl"; judged.parent.mkdir(parents=True)
    judged.write_text(json.dumps({"instance_id": "Py-Pdf__pypdf-3011", "judge": {"keep": True}}) + "\n")
    r = asyncio.run(A.draft_activity({"judged_path": str(judged), "instance_id": "Py-Pdf__pypdf-3011", "draft_model": "m"}))
    assert r["scenario_id"] == "py-pdf--pypdf-3011" and Path(r["draft_path"]).exists() and len(drafted) == 1
    asyncio.run(A.draft_activity({"judged_path": str(judged), "instance_id": "Py-Pdf__pypdf-3011", "draft_model": "m"}))
    assert len(drafted) == 1                                   # existing draft retained
    r3 = asyncio.run(A.draft_activity({"judged_path": str(judged), "instance_id": "Py-Pdf__pypdf-3011", "draft_model": "m", "force": True, "reject_reasons": ["leak"]}))
    assert len(drafted) == 2 and (Path(r3["draft_path"]).parent / "scenario.draft.rejected-1.json").exists()


def test_mine_activity_adapts_to_either_mine_signature(ws, monkeypatch):
    seen = {}
    def mine_swebench_annotated(ws_, csv_path, limit=None, pytest_repos_only=True):
        seen.update(csv_path=str(csv_path), limit=limit, pytest_repos_only=pytest_repos_only); return ws_.candidates_dir / "swebench-annotated.jsonl"
    def mine_github_repo(ws_, repo, since="2026-06-01", pr=None, max_candidates=20):
        seen.update(repo=repo, pr=pr, since=since); return ws_.candidates_dir / "github-repo--x.jsonl"
    monkeypatch.setitem(sys.modules, "swesim.stages.mine", fake_module("swesim.stages.mine", mine_swebench_annotated=mine_swebench_annotated, mine_github_repo=mine_github_repo))
    r = asyncio.run(A.mine_activity({"source": "swebench-annotated", "limit": 5}))
    assert r["candidates_path"].endswith("swebench-annotated.jsonl") and seen["limit"] == 5 and seen["pytest_repos_only"] is True
    r = asyncio.run(A.mine_activity({"source": "github-repo", "repo": "https://github.com/py-pdf/pypdf", "pr": 12, "since": "2026-07-01", "unknown_key": 1}))
    assert r["candidates_path"].endswith("github-repo--x.jsonl") and seen["pr"] == 12 and seen["since"] == "2026-07-01"


def test_probe_models_substitutes_judge_and_draft_but_never_solver(ws, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-value-1234")
    def chat_json(model_id, system, messages, schema_hint, temperature=0.0, max_tokens=4000):
        if model_id.startswith("openai/"): raise RuntimeError("401 unauthorized for key fake-openai-key-value-1234")
        return {"ok": True}
    monkeypatch.setattr("swesim.llm.chat_json", chat_json)
    r = A.probe_models({"judge_model": "openai/gpt-5.5", "draft_model": "anthropic/claude-sonnet-5", "models": ["openai/gpt-5.5", "anthropic/claude-haiku-4-5-20251001"]})
    assert r["substitutions"] == {"judge_model": A.FALLBACK_MODEL}
    assert r["models"]["openai/gpt-5.5"]["ok"] is False and r["models"]["anthropic/claude-sonnet-5"]["ok"] is True
    assert r["needs_human"] == ["solver model openai/gpt-5.5 unavailable; no silent substitution"]
    assert r["solver_unavailable"] == ["openai/gpt-5.5"]
    err = r["models"]["openai/gpt-5.5"]["error"]
    assert "fake-openai-key-value-1234" not in err and "[redacted:OPENAI_API_KEY]" in err   # provider error text is redacted
    assert json.loads((ws.root / "evidence" / "model-probe.json").read_text())["substitutions"] == {"judge_model": A.FALLBACK_MODEL}


def test_execute_trial_request_writes_runner_error_on_spec_mismatch(ws, monkeypatch):
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]
    atomic_json(d / "request.json", {**p, "spec_hash": "0" * 64})
    monkeypatch.setenv("SWESIM_WORKSPACE", str(ws.root)); monkeypatch.setenv("SWESIM_ENV_FILE", str(ws.root.parent / "missing.env"))
    monkeypatch.setitem(sys.modules, "swesim.stages.run", fake_module("swesim.stages.run", run_trial=lambda *a, **k: pytest.fail("must not run")))
    with pytest.raises(ValueError, match="frozen spec changed"): A.execute_trial_request(d / "request.json")
    assert "frozen spec changed" in json.loads((d / "runner-error.json").read_text())["error"]


def test_execute_trial_request_runs_run_trial_with_full_run_config(ws, monkeypatch):
    spec = frozen_scenario(ws); p = trial_request(ws, spec, condition="full"); d = ws.root / "trials" / p["trial_id"]
    atomic_json(d / "request.json", p); seen = {}
    class Rec:
        def __init__(s, tid): s.trial_id = tid; s.status = "completed"; s.reward = {"reward": 1.0, "success": 1}
    def run_trial(ws_, spec_, rc, attempt_index, stakeholder_url, worker_secret):
        seen.update(rc=rc, attempt=attempt_index, url=stakeholder_url, secret=worker_secret); return Rec(p["trial_id"])
    monkeypatch.setitem(sys.modules, "swesim.stages.run", fake_module("swesim.stages.run", run_trial=run_trial))
    monkeypatch.setenv("SWESIM_WORKSPACE", str(ws.root)); monkeypatch.setenv("SWESIM_ENV_FILE", str(ws.root.parent / "missing.env")); monkeypatch.setenv("SWESIM_WORKER_SECRET", "s")
    out = A.execute_trial_request(d / "request.json")
    assert out["status"] == "completed" and seen["rc"] == RunConfig.model_validate(p["run_config"]) and seen["secret"] == "s" and seen["attempt"] == 0


# ---------------------------------------------------------------- real Temporal test environment (fakes for activities)

def _run_in_env(scenario):
    """Run `scenario(env)` in a Temporal test environment; skip explicitly when none can start offline."""
    from temporalio.testing import WorkflowEnvironment
    async def go():
        env = None; errors = []
        for name, start in (("time-skipping", lambda: WorkflowEnvironment.start_time_skipping()),
                            ("local dev server", lambda: WorkflowEnvironment.start_local(dev_server_existing_path=shutil.which("temporal"), dev_server_log_level="error"))):
            try: env = await start(); break
            except Exception as e: errors.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
        if env is None: pytest.skip("no Temporal test server available offline: " + "; ".join(errors))
        try: await scenario(env)
        finally: await env.shutdown()
    asyncio.run(go())


def test_env_trial_workflow_rejects_duplicate_id_and_launches_once():
    from temporalio import activity
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.worker import Worker
    launches = []
    @activity.defn(name="launch_trial_activity")
    async def launch(p): launches.append(p["trial_id"]); return {"trial_id": p["trial_id"], "unit": "swesim-trial-" + p["trial_id"], "decision": "launch", "launched": True}
    @activity.defn(name="await_trial_activity")
    async def collect(p): return {"trial_id": p["trial_id"], "status": "completed", "reward": {"reward": 1.0, "success": 1}}
    async def scenario(env):
        async with Worker(env.client, task_queue="swesim", workflows=[W.TrialWorkflow], activities=[launch, collect]):
            tid = trial_id("s", "interactive", "r", 0)
            r = await env.client.execute_workflow(W.TrialWorkflow.run, {"trial_id": tid}, id=tid, task_queue="swesim", id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
            assert r["status"] == "completed" and launches == [tid]
            with pytest.raises(WorkflowAlreadyStartedError):
                await env.client.start_workflow(W.TrialWorkflow.run, {"trial_id": tid}, id=tid, task_queue="swesim", id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
            assert launches == [tid]
    _run_in_env(scenario)


def test_env_pipeline_signal_mode_waits_then_verifies_signalled_hash():
    from temporalio import activity
    from temporalio.worker import Worker
    calls = []
    def fake(name, result):
        @activity.defn(name=name)
        async def f(p): calls.append((name, p)); return result(p) if callable(result) else result
        return f
    acts = [fake("probe_models_activity", {"substitutions": {}, "models": {}, "needs_human": []}),
            fake("mine_activity", {"candidates_path": "c"}), fake("judge_activity", {"judged_path": "j", "instance_ids": ["a__1"]}),
            fake("draft_activity", {"scenario_id": "a--1", "draft_path": "d"}),
            fake("verify_approval_activity", lambda p: {"scenario_id": p["scenario_id"], "spec_hash": p["spec_hash"]}),
            fake("compile_activity", {"compile_path": "x"}), fake("validate_activity", {"status": "valid", "failures": []}),
            fake("register_activity", {"registry_entry": {}}), fake("plan_trials_activity", {"scenario_id": "a--1", "trials": []}),
            fake("score_activity", {"scores_path": "s"}), fake("export_activity", {"manifest_paths": []})]
    async def scenario(env):
        async with Worker(env.client, task_queue="swesim", workflows=[W.PipelineWorkflow, W.TrialWorkflow], activities=acts):
            h = await env.client.start_workflow(W.PipelineWorkflow.run, {"approval_mode": "signal", "judge_model": "m", "draft_model": "m", "models": ["m"], "stakeholder_url": "https://s"},
                                                id="pipeline-test", task_queue="swesim", id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
            for _ in range(200):
                state = await h.query(W.PipelineWorkflow.progress)
                if state.get("stage") == "awaiting-approval": break
                await asyncio.sleep(0.05)
            assert state["stage"] == "awaiting-approval" and state["scenario_id"] == "a--1"
            await h.signal(W.PipelineWorkflow.approve_spec, {"scenario_id": "a--1", "spec_hash": "sig-hash"})
            result = await h.result()
            assert result["status"] == "complete" and [(s["scenario_id"], s["status"]) for s in result["scenarios"]] == [("a--1", "complete")]
            verify = next(p for n, p in calls if n == "verify_approval_activity"); assert verify["spec_hash"] == "sig-hash"
            names = [n for n, _ in calls]
            assert names.index("verify_approval_activity") < names.index("compile_activity") < names.index("validate_activity") < names.index("register_activity")
    _run_in_env(scenario)


def test_env_matrix_runs_children_serially_with_trial_ids_then_scores():
    from temporalio import activity
    from temporalio.worker import Worker
    order = []
    @activity.defn(name="launch_trial_activity")
    async def launch(p): order.append(("launch", p["trial_id"])); return {"launched": True}
    @activity.defn(name="await_trial_activity")
    async def collect(p): order.append(("await", p["trial_id"])); return {"trial_id": p["trial_id"], "status": "completed", "reward": {"reward": 0.0, "success": 0}}
    @activity.defn(name="score_activity")
    async def score(p): order.append(("score", p["scenario_id"])); return {"scores_path": "s"}
    @activity.defn(name="export_activity")
    async def export(p): order.append(("export", p["scenario_id"])); return {"manifest_paths": []}
    trials = [{"trial_id": trial_id("s", "hidden", "r", i), "scenario_id": "a--1"} for i in range(2)]
    async def scenario(env):
        async with Worker(env.client, task_queue="swesim", workflows=[W.RunMatrixWorkflow, W.TrialWorkflow], activities=[launch, collect, score, export]):
            r = await env.client.execute_workflow(W.RunMatrixWorkflow.run, {"scenario_id": "a--1", "trials": trials}, id="matrix-test", task_queue="swesim")
            assert [t["status"] for t in r["trials"]] == ["completed", "completed"]
            assert order == [("launch", trials[0]["trial_id"]), ("await", trials[0]["trial_id"]), ("launch", trials[1]["trial_id"]), ("await", trials[1]["trial_id"]), ("score", "a--1"), ("export", "a--1")]
            # Re-running the matrix cannot relaunch: the child ids are already used.
            r2 = await env.client.execute_workflow(W.RunMatrixWorkflow.run, {"scenario_id": "a--1", "trials": trials}, id="matrix-test-2", task_queue="swesim")
            assert [t["status"] for t in r2["trials"]] == ["duplicate-rejected", "duplicate-rejected"] and len([o for o in order if o[0] == "launch"]) == 2
    _run_in_env(scenario)


# ---------------------------------------------------------------- worker and client surface

def test_worker_registers_all_workflows_and_activities_serial_trials():
    from swesim.workflows import worker as WK
    assert WK.TASK_QUEUE == "swesim" and WK.MAX_CONCURRENT_ACTIVITIES == 4
    names = {a.__name__ for a in A.ALL}
    assert {"mine_activity", "judge_activity", "draft_activity", "auto_approve_activity", "verify_approval_activity", "compile_activity", "validate_activity",
            "register_activity", "plan_trials_activity", "launch_trial_activity", "await_trial_activity", "score_activity", "export_activity", "probe_models_activity"} <= names
    assert set(WK.WORKFLOWS) == {W.TrialWorkflow, W.RunMatrixWorkflow, W.PipelineWorkflow}
    assert A.LAUNCH_SLOTS._value == 1                        # one launch at a time per worker


def test_client_ids_and_signal_payload(monkeypatch):
    from swesim.workflows import client as C
    class Handle:
        def __init__(s): s.signals = []
        async def signal(s, fn, arg): s.signals.append((fn, arg))
    class FakeClient:
        def __init__(s): s.started = []; s.handle = Handle()
        async def start_workflow(s, fn, arg, **kw): s.started.append((fn, arg, kw))
        def get_workflow_handle(s, wid): s.wid = wid; return s.handle
    fc = FakeClient()
    async def connect(): return fc
    monkeypatch.setattr(C, "connect", connect)
    wid = asyncio.run(C.start_pipeline({"source": "github-repo"}))
    assert wid.startswith("pipeline-") and fc.started[0][2]["id"] == wid and fc.started[0][2]["task_queue"] == "swesim"
    assert fc.started[0][2]["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    asyncio.run(C.approve(wid, "a--1", "h"))
    assert fc.wid == wid and fc.handle.signals == [(W.PipelineWorkflow.approve_spec, {"scenario_id": "a--1", "spec_hash": "h"})]
    mid = asyncio.run(C.start_matrix({"scenario_id": "a--1", "trials": []}))
    assert mid.startswith("matrix-") and fc.started[1][0] == W.RunMatrixWorkflow.run


# ---------------------------------------------------------------- Task 13 deployment files (syntax and contracts)

@pytest.mark.parametrize("script", ["create.sh", "setup.sh", "tunnel.sh"])
def test_vm_scripts_parse(script):
    path = ROOT / "scripts" / "vm" / script
    assert path.is_file() and os.access(path, os.X_OK), script
    subprocess.run(["bash", "-n", str(path)], check=True)
    text = path.read_text()
    assert "set -euo pipefail" in text and "export $(" not in text and "eval " not in text and "source " not in text and ". \"$" not in text


def _unit(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None); cp.optionxform = str  # systemd repeats Environment=
    cp.read_string((ROOT / "scripts" / "vm" / name).read_text()); return cp


def test_temporal_unit_binds_loopback_and_persists_db():
    u = _unit("swesim-temporal.service"); exec_ = u["Service"]["ExecStart"]
    assert "server start-dev" in exec_ and "--db-filename /var/lib/swesim/temporal.db" in exec_ and "--ip 127.0.0.1" in exec_
    assert "--port 7233" in exec_ and "--ui-port 8233" in exec_ and u["Service"]["Restart"] == "always" and u["Install"]["WantedBy"] == "default.target"


def test_worker_unit_uses_env_file_and_repo_checkout():
    u = _unit("swesim-worker.service"); s = u["Service"]
    assert s["EnvironmentFile"] == "/etc/swesim/env" and s["WorkingDirectory"] == "/opt/swesim" and s["Restart"] == "always"
    assert "swesim.workflows.worker" in s["ExecStart"] or "swesim worker" in s["ExecStart"]
    assert "swesim-temporal.service" in u["Unit"]["After"]
    assert "KillMode=" not in (ROOT / "scripts/vm/swesim-worker.service").read_text() or s.get("KillMode") == "process"


def test_dashboard_unit_binds_loopback_8080():
    u = _unit("swesim-dashboard.service"); s = u["Service"]
    assert "uvicorn swesim.dashboard.app:app" in s["ExecStart"] and "--host 127.0.0.1" in s["ExecStart"] and "--port 8080" in s["ExecStart"]
    assert s["EnvironmentFile"] == "/etc/swesim/env" and s["WorkingDirectory"] == "/opt/swesim"


def test_create_script_contract():
    text = (ROOT / "scripts/vm/create.sh").read_text()
    assert "gcloud compute instances create --quiet" in text and "e2-standard-4" in text and "100GB" in text
    assert ("debian-12" in text) or ("ubuntu-2404-lts" in text)
    assert "GCP_PROJECT" in text and "GCP_ZONE" in text and "SWESIM_ENV_FILE" in text


def test_tunnel_forwards_all_three_ports():
    text = (ROOT / "scripts/vm/tunnel.sh").read_text()
    for port in ("8233", "7233", "8080"): assert f"{port}:127.0.0.1:{port}" in text or f"{port}:localhost:{port}" in text
    assert "gcloud compute ssh" in text


def test_setup_script_contract():
    text = (ROOT / "scripts/vm/setup.sh").read_text()
    for needle in ("astral.sh/uv/install.sh", "uv python install 3.12", "temporal.download/cli/archive/v1.8.3", "/etc/swesim/env", "chmod 600",
                   "loginctl enable-linger", "systemd-analyze --user verify", "systemctl --user enable --now", "swesim-temporal", "swesim-worker", "swesim-dashboard", "/opt/swesim", "/var/lib/swesim"):
        assert needle in text, needle
    assert "docker" not in text.lower() or "not required" in text.lower()


def test_ops_doc_has_required_sections():
    text = (ROOT / "docs/ops.md").read_text()
    for section in ("## Topology", "## Deploy (VM)", "## Laptop-off acceptance test", "## Worker restart test", "## Model probe and fallbacks", "## Known failures and residuals", "## Manual retry lineage"):
        assert section in text, section
    assert "workspace/evidence/ops.json" in text and "systemctl --user restart swesim-worker" in text and "swesim-trial-" in text
    # Fix round 1, Important 4: the launch-count evidence must match systemd >= 250's "Started <unit>.service - <description>." line.
    assert text.count("Started swesim-trial-") >= 2 and "Started swesim trial'" not in text and 'count("Started swesim trial")' not in text
    assert "complete-with-failures" in text and "complete-no-trials" in text and "withholding-leak" in text


def _bash_functions(text, names):
    parts = []
    for name in names:
        m = re.search(rf"^{re.escape(name)}\(\) \{{$.*?^\}}$", text, re.S | re.M)
        assert m, name
        parts.append(m.group(0))
    return "\n".join(parts)


def test_worker_env_is_rendered_with_only_the_five_keys(tmp_path):
    # Fix round 1, Important 3: the laptop dotenv file is never shipped; a filtered KEY='value' file becomes /etc/swesim/env.
    from dotenv import dotenv_values
    src = tmp_path / "laptop.env"
    src.write_text("# laptop env\nexport ANTHROPIC_API_KEY=\"fake-anthropic-123\"\nOPENAI_API_KEY=fake-openai-456 \n"
                   "SWESIM_WORKER_SECRET='fake-worker-secret'\nSWESIM_STAKEHOLDER_URL=https://stake.example\nGH_TOKEN=fake-gh-token\n"
                   "GCP_PROJECT=my-proj\nGCP_ZONE=us-central1-a\nAWS_SECRET_ACCESS_KEY=must-not-ship\nHF_TOKEN=must-not-ship-either\n")
    create = (ROOT / "scripts/vm/create.sh").read_text(); setup = (ROOT / "scripts/vm/setup.sh").read_text()
    funcs = _bash_functions(create, ["env_get", "render_worker_env"])
    assert funcs == _bash_functions(setup, ["env_get", "render_worker_env"])          # identical copies in both scripts
    dst = tmp_path / "worker.env"
    subprocess.run(["bash", "-c", funcs + '\nrender_worker_env "$1" "$2"', "bash", str(src), str(dst)], check=True, capture_output=True, text=True)
    assert oct(dst.stat().st_mode & 0o777) == "0o600"
    text = dst.read_text()
    assert "GCP_" not in text and "AWS_" not in text and "HF_TOKEN" not in text and "export" not in text and "#" not in text
    assert all(re.fullmatch(r"[A-Z_]+='[^'\n]*'", line) for line in text.splitlines())   # systemd EnvironmentFile= safe
    assert dotenv_values(dst) == {"ANTHROPIC_API_KEY": "fake-anthropic-123", "OPENAI_API_KEY": "fake-openai-456 ", "SWESIM_WORKER_SECRET": "fake-worker-secret",
                                  "SWESIM_STAKEHOLDER_URL": "https://stake.example", "GH_TOKEN": "fake-gh-token"}
    # create.sh ships the rendered temp file, and setup.sh renders again into /etc/swesim/env
    assert '"${SCP[@]}" "$WORKER_ENV" "$VM_NAME":~/swesim-bootstrap/env' in create and 'render_worker_env "$ENV_FILE" "$WORKER_ENV"' in create
    assert 'render_worker_env "$BOOT/env" /etc/swesim/env' in setup and 'install -m 600 "$BOOT/env"' not in setup
    # a value with a single quote is refused rather than written malformed
    bad = tmp_path / "bad.env"; bad.write_text("ANTHROPIC_API_KEY=it's-bad\nSWESIM_WORKER_SECRET=x\nGH_TOKEN=y\n")
    cp = subprocess.run(["bash", "-c", funcs + '\nrender_worker_env "$1" "$2"', "bash", str(bad), str(tmp_path / "bad.out")], capture_output=True, text=True)
    assert cp.returncode != 0 and "single quote" in cp.stderr
