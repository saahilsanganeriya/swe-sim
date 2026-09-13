"""Validate stage expectation logic and harbor_cli wrapper. Offline: no Harbor process is started."""
import json, os, subprocess, sys
from pathlib import Path

import pytest

from swesim import harbor_cli
from swesim.models import ScenarioSpec, RunConfig, minimal_spec_dict
from swesim.paths import Workspace
from swesim.stages import validate
from swesim.stages.compile import CompileResult
from swesim.stages.validate import evaluate_controls, gold_lines


def test_expectations():
    results = {"nop": {"reward": 0, "success": 0, "outcome_code": 3}, "oracle": {"reward": 1.0, "success": 1, "outcome_code": 1},
               "plausible-wrong:keyerror": {"reward": 0.0, "success": 0, "outcome_code": 2},
               "tamper:root-conftest": {"reward": 0.0, "success": 0, "outcome_code": 2}}
    spec_controls = {"plausible_wrong": [{"name": "keyerror", "expected_success": 0, "expected_reward_max": 0.5}],
                     "alternative_correct": [], "tamper": [{"name": "root-conftest", "cls": "incorrect-exploit", "expected_reward": 0.0}]}
    ok, failures = evaluate_controls(results, spec_controls)
    assert ok and failures == []
    results["tamper:root-conftest"]["reward"] = 1.0; results["tamper:root-conftest"]["success"] = 1
    ok, failures = evaluate_controls(results, spec_controls)
    assert not ok and "tamper:root-conftest" in failures[0]


def test_expectations_edge_cases():
    controls = {"plausible_wrong": [{"name": "k", "expected_success": 0, "expected_reward_max": 0.0}], "alternative_correct": [{"name": "alt"}],
                "tamper": [{"name": "t", "cls": "incorrect-exploit"}, {"name": "benign", "cls": "benign-edit"}]}
    ok, failures = evaluate_controls({"nop": {}, "oracle": {}}, controls)
    assert not ok and failures[0].startswith("missing control results") and "alt-correct:alt" in failures[0]
    good = {"nop": {"reward": 0, "success": 0, "outcome_code": 3}, "oracle": {"reward": 1, "success": 1, "outcome_code": 1},
            "plausible-wrong:k": {"reward": 0, "success": 0, "outcome_code": 2}, "alt-correct:alt": {"reward": 1, "success": 1, "outcome_code": 1},
            "tamper:t": {"reward": 0, "success": 0, "outcome_code": 2}, "tamper:benign": {"reward": 1, "success": 1, "outcome_code": 1}}
    assert evaluate_controls(good, controls) == (True, [])
    infra = json.loads(json.dumps(good)); infra["tamper:t"] = {"reward": 0, "success": 0, "outcome_code": 5}
    ok, failures = evaluate_controls(infra, controls)
    assert not ok and any("infrastructure" in f and "tamper:t" in f for f in failures)  # a collection failure is not a defeated attack
    bad = json.loads(json.dumps(good)); bad["plausible-wrong:k"]["reward"] = 0.3; bad["tamper:benign"]["success"] = 0; bad["alt-correct:alt"]["success"] = 0
    ok, failures = evaluate_controls(bad, controls)
    assert not ok and len(failures) == 3
    nonnum = json.loads(json.dumps(good)); nonnum["oracle"]["reward"] = "1"
    ok, failures = evaluate_controls(nonnum, controls)
    assert not ok and any("numeric" in f for f in failures)
    partial_alt = json.loads(json.dumps(good)); partial_alt["alt-correct:alt"]["reward"] = 0.5
    ok, failures = evaluate_controls(partial_alt, controls)
    assert not ok and failures == ["alt-correct:alt: reward 0.5 != 1"]  # an alternative correct fix must earn full credit
    declared = json.loads(json.dumps(controls)); declared["tamper"][1]["expected_reward"] = 1.0; declared["plausible_wrong"][0]["expected_reward"] = 0.0
    assert evaluate_controls(good, declared) == (True, [])
    off = json.loads(json.dumps(good)); off["tamper:benign"]["reward"] = 0.5; off["plausible-wrong:k"]["reward"] = 0.0
    ok, failures = evaluate_controls(off, declared)
    assert not ok and failures == ["tamper:benign: reward 0.5 != expected 1.0"]


def test_gold_lines_picks_long_added_lines():
    diff = "--- a/x.py\n+++ b/x.py\n@@\n-old\n+short\n+    def new_helper(value, dim):\n+        raise ValueError('dimension mismatch')\n+    return value\n+    another_long_added_line = compute(value)\n+    yet_another_long_added_line = 1\n"
    lines = gold_lines(diff)
    assert lines == ["def new_helper(value, dim):", "raise ValueError('dimension mismatch')", "another_long_added_line = compute(value)"]
    assert gold_lines("") == []


# ---- validate_scenario end to end with fake compile + fake harbor ------------------------------

def _spec_ws(tmp_path):
    ws = Workspace(tmp_path / "ws"); d = minimal_spec_dict(); spec = ScenarioSpec.model_validate(d)
    ws.scenario_dir(spec.id).mkdir(parents=True); return ws, spec


def _fake_compile(tmp_path, spec):
    task = tmp_path / "task-full"; (task / "controls").mkdir(parents=True); (task / "solution").mkdir()
    for c in spec.controls.plausible_wrong + spec.controls.alternative_correct + spec.controls.tamper: (task / "controls" / f"{c.name}.diff").write_text("diff")
    (task / "solution" / "oracle.diff").write_text("+++ b/x.py\n+    def new_helper(value, dim):\n")
    cj = tmp_path / "compile.json"; cj.write_text(json.dumps({"compiled_hash": "c" * 64}))
    return lambda ws, s, rc, url: CompileResult({"full": task, "hidden": task, "interactive": task}, "sha256:" + "d" * 64, cj)


def _fake_harbor(jobs_root: Path, rewards: dict, probe_txt: str | None, exception_for=()):
    launched = []

    def run_task(task_dir, agent, jobs_dir, job_name, model=None, agent_kwargs=None, env_provider="modal", timeout_sec=10800):
        name = job_name.split("--", 1)[1]; launched.append((name, agent, agent_kwargs))
        td = jobs_dir / job_name / "trial"; (td / "verifier").mkdir(parents=True); (td / "agent").mkdir()
        (td / "result.json").write_text(json.dumps({"exception_info": {"exception_type": "X"} if name in exception_for else None}))
        if name == "probe":
            if probe_txt is not None: (td / "agent" / "probe.txt").write_text(probe_txt)
            return td
        key = {"nop": "nop", "oracle": "oracle"}.get(name, name)
        r = rewards[key]; (td / "verifier" / "reward.json").write_text(json.dumps({k: v for k, v in r.items() if k != "per_test"}))
        (td / "verifier" / "report.json").write_text(json.dumps({"per_test": r.get("per_test", [])}))
        return td
    return run_task, launched


GOOD = {"nop": {"reward": 0, "success": 0, "outcome_code": 3, "per_test": [{"id": "t1", "observed": "failed"}, {"id": "t0", "observed": "passed"}]},
        "oracle": {"reward": 1.0, "success": 1, "outcome_code": 1, "per_test": [{"id": "t1", "observed": "passed"}, {"id": "t0", "observed": "passed"}]},
        "plausible-wrong-keyerror": {"reward": 0.0, "success": 0, "outcome_code": 2},
        "tamper-root-conftest": {"reward": 0.0, "success": 0, "outcome_code": 2}}
CLEAN_PROBE = "REFS=1\nREFLOG=1\nGOLD_SHA_CHECKED=none\nHITS_BEGIN\nHITS_END\n"


def test_validate_scenario_valid(tmp_path, monkeypatch):
    ws, spec = _spec_ws(tmp_path)
    monkeypatch.setattr(validate, "compile_scenario", _fake_compile(tmp_path, spec))
    run_task, launched = _fake_harbor(ws.jobs_dir, GOOD, CLEAN_PROBE)
    monkeypatch.setattr(harbor_cli, "run_task", run_task)
    out = validate.validate_scenario(ws, spec, RunConfig.default("full"), "https://x.modal.run")
    assert out["status"] == "valid" and out["failures"] == []
    assert out["expected_base"] == {"t1": "failed", "t0": "passed"} and out["expected_gold"] == {"t1": "passed", "t0": "passed"}
    ctl = out["controls"]["tamper:root-conftest"]
    assert ctl["reward"] == 0.0 and ctl["success"] == 0 and ctl["outcome"] == "valid-incorrect" and ctl["expected"]["cls"] == "incorrect-exploit" and "report" not in ctl
    assert out["controls"]["nop"]["outcome"] == "agent-invalid-submission"
    assert out["probe"]["gold_hits"] == [] and out["probe"]["all_refs"] == 1
    assert set(out["trial_dirs"]) == {"nop", "oracle", "plausible-wrong:keyerror", "tamper:root-conftest", "probe"}
    assert out["compiled_hash"] == "c" * 64 and out["image_digest"] == "sha256:" + "d" * 64 and out["spec_hash"] == spec.spec_hash
    assert json.loads((ws.scenario_dir(spec.id) / "validation.json").read_text())["status"] == "valid"
    names = [n for n, _, _ in launched]
    assert names == ["nop", "oracle", "plausible-wrong-keyerror", "tamper-root-conftest", "probe"]
    assert launched[0][1] == "nop" and launched[1][1] == "oracle"
    assert launched[2][1] == validate.PATCH_AGENT and launched[2][2]["patch"].endswith("controls/keyerror.diff")
    assert launched[4][2]["probe"] == "true" and json.loads(launched[4][2]["gold_lines"]) == ["def new_helper(value, dim):"]
    assert all(jd.parent.parent.name == "c" * 16 for jd in [Path(p) for p in out["trial_dirs"].values()])  # jobs keyed by compiled hash


def test_validate_scenario_invalid_reasons(tmp_path, monkeypatch):
    ws, spec = _spec_ws(tmp_path)
    monkeypatch.setattr(validate, "compile_scenario", _fake_compile(tmp_path, spec))
    bad = json.loads(json.dumps(GOOD)); bad["nop"]["per_test"] = [{"id": "t1", "observed": "passed"}, {"id": "t0", "observed": "passed"}]
    run_task, _ = _fake_harbor(ws.jobs_dir, bad, "REFS=1\nREFLOG=1\nGOLD_SHA_CHECKED=abc\nHITS_BEGIN\nGOLD_OBJECT_REACHABLE:abc\n./pkg/mod.py\nHITS_END\n", exception_for={"tamper-root-conftest"})
    monkeypatch.setattr(harbor_cli, "run_task", run_task)
    out = validate.validate_scenario(ws, spec, RunConfig.default("full"), "https://x.modal.run")
    assert out["status"] == "invalid"
    assert any("base must execute" in f for f in out["failures"])
    assert any("gold patch lines reachable" in f and "./pkg/mod.py" in f and "GOLD_OBJECT" not in f for f in out["failures"])
    assert any("gold commit object reachable" in f for f in out["failures"]) and out["probe"]["gold_object_reachable"] is True
    assert any(f.startswith("tamper:root-conftest") and "control agent failed" in f for f in out["failures"])
    assert json.loads((ws.scenario_dir(spec.id) / "validation.json").read_text())["status"] == "invalid"


def test_validate_scenario_missing_probe_and_launch_failure(tmp_path, monkeypatch):
    ws, spec = _spec_ws(tmp_path)
    monkeypatch.setattr(validate, "compile_scenario", _fake_compile(tmp_path, spec))
    run_task, _ = _fake_harbor(ws.jobs_dir, GOOD, None)
    monkeypatch.setattr(harbor_cli, "run_task", run_task)
    out = validate.validate_scenario(ws, spec, RunConfig.default("full"), "https://x.modal.run")
    assert out["status"] == "invalid" and any("reachability evidence" in f for f in out["failures"])

    ws2, spec2 = _spec_ws(tmp_path / "b")
    monkeypatch.setattr(validate, "compile_scenario", _fake_compile(tmp_path / "b", spec2))
    calls = []
    def failing(task_dir, agent, jobs_dir, job_name, **kw):
        calls.append(job_name); raise harbor_cli.HarborRunError("no trial dir")
    monkeypatch.setattr(harbor_cli, "run_task", failing)
    out = validate.validate_scenario(ws2, spec2, RunConfig.default("full"), "https://x.modal.run")
    assert out["status"] == "invalid" and any("nop" in f and "no trial dir" in f for f in out["failures"])
    assert len(calls) == 1  # a launch failure stops the sweep instead of burning five more launches
    assert (ws2.scenario_dir(spec2.id) / "validation.json").exists()


# ---- harbor_cli -------------------------------------------------------------------------------

def _fake_run_factory(jobs_dir: Path, trials=1, rc=0, result=None):
    seen = {}
    def fake_run(cmd, capture_output, text, timeout, env):
        seen.update(cmd=cmd, env=env, timeout=timeout)
        job = jobs_dir / cmd[cmd.index("--job-name") + 1]
        for i in range(trials):
            (job / f"trial{i}").mkdir(parents=True); (job / f"trial{i}" / "result.json").write_text(json.dumps(result if result is not None else {"id": i}))
        return subprocess.CompletedProcess(cmd, rc, stdout="out", stderr="err")
    return fake_run, seen


def test_run_task_builds_command_and_returns_trial(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"; fake_run, seen = _fake_run_factory(jobs)
    monkeypatch.setattr(harbor_cli.subprocess, "run", fake_run)
    fake_bin = tmp_path / "bin"; fake_bin.mkdir(); (fake_bin / "harbor").write_text("#!/bin/sh\n"); (fake_bin / "harbor").chmod(0o755)
    monkeypatch.setattr(harbor_cli.sys, "executable", str(fake_bin / "python"))
    monkeypatch.setenv("GH_TOKEN", "gh-secret"); monkeypatch.setenv("SWESIM_WORKER_SECRET", "w"); monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    td = harbor_cli.run_task(tmp_path / "task", validate.PATCH_AGENT, jobs, "job-1", model="anthropic/m", agent_kwargs={"patch": "/p.diff", "probe": "true"}, timeout_sec=123)
    assert td == jobs / "job-1" / "trial0"
    cmd = seen["cmd"]
    assert cmd[0] == str(fake_bin / "harbor") and cmd[1:3] == ["run", "--max-retries"] and cmd[3] == "0"
    for flag, val in (("-p", str(tmp_path / "task")), ("--agent", validate.PATCH_AGENT), ("-e", "modal"), ("--jobs-dir", str(jobs)), ("--job-name", "job-1"), ("-m", "anthropic/m")):
        assert cmd[cmd.index(flag) + 1] == val, flag
    assert "--yes" in cmd and cmd.count("--ak") == 2 and "patch=/p.diff" in cmd and "probe=true" in cmd
    assert seen["timeout"] == 123
    assert "GH_TOKEN" not in seen["env"] and "SWESIM_WORKER_SECRET" not in seen["env"] and seen["env"]["ANTHROPIC_API_KEY"] == "k"
    assert seen["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(Path(harbor_cli.__file__).resolve().parent.parent)


def test_run_task_resolves_harbor_from_path_or_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(harbor_cli.sys, "executable", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setattr(harbor_cli.shutil, "which", lambda name: "/opt/bin/harbor" if name == "harbor" else None)
    assert harbor_cli.harbor_executable() == "/opt/bin/harbor"
    monkeypatch.setattr(harbor_cli.shutil, "which", lambda name: None)
    with pytest.raises(harbor_cli.HarborRunError, match="harbor executable"):
        harbor_cli.run_task(tmp_path / "task", "nop", tmp_path / "jobs", "j")
    real = Path(sys.executable).with_name("harbor")
    if real.exists():
        monkeypatch.setattr(harbor_cli.sys, "executable", sys.executable)
        assert harbor_cli.harbor_executable() == str(real)


def test_run_task_reconciles_and_refuses_ambiguity(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"; monkeypatch.setattr(harbor_cli, "harbor_executable", lambda: "/bin/harbor")
    launched = []
    monkeypatch.setattr(harbor_cli.subprocess, "run", lambda *a, **k: launched.append(1))
    existing = jobs / "done" / "trialA"; existing.mkdir(parents=True); (existing / "result.json").write_text("{}")
    assert harbor_cli.run_task(tmp_path / "task", "nop", jobs, "done") == existing and not launched
    (jobs / "pending").mkdir()
    with pytest.raises(harbor_cli.HarborRunError, match="reconcile"):
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "pending")
    assert not launched
    fake_run, _ = _fake_run_factory(jobs, trials=2); monkeypatch.setattr(harbor_cli.subprocess, "run", fake_run)
    with pytest.raises(harbor_cli.HarborRunError, match="ambiguous"):
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "two")
    fake_run, _ = _fake_run_factory(jobs, trials=0, rc=1); monkeypatch.setattr(harbor_cli.subprocess, "run", fake_run)
    with pytest.raises(harbor_cli.HarborRunError, match="no trial dir"):
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "none")
    fake_run, _ = _fake_run_factory(jobs, trials=1, rc=1, result={"exception_info": {"exception_type": "AgentError"}}); monkeypatch.setattr(harbor_cli.subprocess, "run", fake_run)
    assert harbor_cli.run_task(tmp_path / "task", "nop", jobs, "agent-error").name == "trial0"  # agent-error trials are returned for attribution

    def timeout(*a, **k): raise subprocess.TimeoutExpired(cmd="harbor", timeout=1)
    monkeypatch.setattr(harbor_cli.subprocess, "run", timeout)
    with pytest.raises(harbor_cli.HarborRunError, match="deadline"):
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "slow")


def test_run_task_malformed_result_is_a_harbor_error(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"; monkeypatch.setattr(harbor_cli, "harbor_executable", lambda: "/bin/harbor")
    def fake_run(cmd, capture_output, text, timeout, env):
        job = jobs / cmd[cmd.index("--job-name") + 1]; (job / "t").mkdir(parents=True); (job / "t" / "result.json").write_text("{not json")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(harbor_cli.subprocess, "run", fake_run)
    with pytest.raises(harbor_cli.HarborRunError, match="malformed"):
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "bad")
    with pytest.raises(harbor_cli.HarborRunError, match="malformed"):  # reconciling an existing job reads the same file
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "bad")


def test_read_reward_and_report(tmp_path):
    td = tmp_path / "trial"; (td / "verifier").mkdir(parents=True)
    assert harbor_cli.read_reward(td) == {} and harbor_cli.read_report(td) == {}
    (td / "verifier" / "reward.json").write_text('{"reward": 1, "success": 1}'); (td / "verifier" / "report.json").write_text('{"per_test": []}')
    assert harbor_cli.read_reward(td) == {"reward": 1, "success": 1} and harbor_cli.read_report(td) == {"per_test": []}
