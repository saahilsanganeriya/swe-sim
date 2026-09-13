"""Offline tests for the trial launcher protocol (interfaces §3): the pure decision, launch-once-then-reconcile through
a fake launcher, the systemd launcher over a fake systemctl/systemd-run, and the subprocess launcher end to end.

Moved from tests/test_workflows.py when today's systemd code went behind the protocol; the behaviour asserted is the
same, the paths are `swesim.workflows.launchers.*`.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pytest

from swesim.models import RunConfig, ScenarioSpec, minimal_spec_dict, trial_id
from swesim.paths import atomic_json
from swesim.settings import Settings
from swesim.workflows.launchers import base as B
from swesim.workflows.launchers import subprocess as SP
from swesim.workflows.launchers import systemd as SYS

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
        if argv[0] == "systemctl" and argv[2] == "show":
            if self.systemctl_failure: return subprocess.CompletedProcess(argv, 1, "", self.systemctl_failure)
            unit = next(a for a in argv[3:] if not a.startswith("--"))
            st = self.units.get(unit, {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead", "Result": "success"})
            stdout = self.systemctl_stdout_override if self.systemctl_stdout_override is not None else "".join(f"{k}={v}\n" for k, v in st.items())
            return subprocess.CompletedProcess(argv, self.systemctl_rc, stdout, "")
        if argv[0] == "systemctl" and argv[2] == "stop":
            self.units[argv[3]] = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "Result": "success"}
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "systemd-run":
            unit = next(a for a in argv if a.startswith("--unit=")).split("=", 1)[1]
            if self.run_returncode: return subprocess.CompletedProcess(argv, self.run_returncode, "", "Failed to start transient service unit")
            self.units[unit] = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success"}
            return subprocess.CompletedProcess(argv, 0, "", f"Running as unit: {unit}\n")
        raise AssertionError(f"unexpected command {argv}")


class FakeLauncher:
    """A launcher whose runtime is a dict; records every call so the base algorithm's side effects are observable."""

    name = "systemd"

    def __init__(self, state=("absent", "nothing")):
        self.state = state; self.starts: list[str] = []; self.cancelled: list[str] = []; self.cleaned: list[str] = []; self.fail = None

    def runtime_state(self, trial_id, launch): return self.state
    def start(self, trial_id, request_path, intent):
        self.starts.append(trial_id)
        if self.fail: raise self.fail
        self.state = ("running", "fake"); return B.LaunchAck(0, "", "started", f"handle-{trial_id}")
    def cancel(self, trial_id, launch): self.cancelled.append(trial_id)
    def cleanup(self, trial_id, launch): self.cleaned.append(trial_id)


@pytest.fixture
def layout(tmp_path):
    return B.WorkspaceLayout(tmp_path / "workspace")


@pytest.fixture
def systemd(monkeypatch):
    fake = FakeSystemd(); monkeypatch.setattr(SYS, "RUN", fake); return fake


@pytest.fixture
def launcher(tmp_path):
    return SYS.SystemdLauncher(workspace=tmp_path / "workspace", registry_path=tmp_path / "registry.json", env_file=tmp_path / "env", repo_root=tmp_path)


def request(attempt=0, condition="interactive"):
    spec = ScenarioSpec.model_validate(minimal_spec_dict())
    rc = RunConfig(condition=condition, model="anthropic/claude-haiku-4-5-20251001", compiled_hash="c" * 64, brief_profile="natural")
    tid = trial_id(spec.spec_hash, condition, rc.run_config_hash, attempt)
    return dict(trial_id=tid, scenario_id=spec.id, spec_hash=spec.spec_hash, condition=condition, model=rc.model,
                run_config=rc.model_dump(mode="json"), run_config_hash=rc.run_config_hash, attempt_index=attempt, stakeholder_url="https://stake.example")


# ---------------------------------------------------------------- pure decision function

def ev(**kw):
    base = dict(has_record=False, has_runner_error=False, status=None, launch_phase=None, launch_returncode=None, runtime_state="absent", runtime_detail="")
    base.update(kw); return B.TrialEvidence(**base)


@pytest.mark.parametrize("evidence,decision", [
    (ev(), "launch"),
    (ev(has_record=True), "ingest"),
    (ev(has_record=True, runtime_state="running"), "ingest"),
    (ev(status="completed"), "ingest"),
    (ev(has_runner_error=True), "runner-error"),
    (ev(runtime_state="running"), "reconcile"),
    (ev(runtime_state="failed"), "reconcile"),
    (ev(runtime_state="succeeded"), "reconcile"),
    (ev(runtime_state="unknown"), "reconcile"),
    (ev(launch_phase="intent"), "unresolved"),
    (ev(launch_phase="acknowledged", launch_returncode=0), "unresolved"),
    (ev(status="running"), "unresolved"),
    (ev(launch_phase="acknowledged", launch_returncode=1), "launch-failed"),
    (ev(launch_phase="failed"), "launch-failed"),
])
def test_decide_launch(evidence, decision):
    assert B.decide_launch(evidence) == decision


def test_decision_never_launches_when_any_launch_evidence_exists():
    for phase in ("intent", "acknowledged", "failed"):
        for state in ("absent", "running", "succeeded", "failed", "unknown"):
            assert B.decide_launch(ev(launch_phase=phase, runtime_state=state)) != "launch"


def test_systemd_state_mapping_is_the_old_triple():
    st = lambda load, active, sub: {"load_state": load, "active_state": active, "sub_state": sub, "result": ""}
    assert SYS.runtime_state_of(st("not-found", "inactive", "dead")) == ("absent", "not-found/inactive/dead")
    assert SYS.runtime_state_of(st("loaded", "active", "running"))[0] == "running"
    assert SYS.runtime_state_of(st("loaded", "activating", "start"))[0] == "running"
    assert SYS.runtime_state_of(st("loaded", "failed", "failed"))[0] == "failed"
    assert SYS.runtime_state_of(st("loaded", "active", "exited"))[0] == "succeeded"     # RemainAfterExit: finished, rc 0
    assert SYS.runtime_state_of(st("loaded", "inactive", "dead"))[0] == "succeeded"     # stopped without a failure flag


# ---------------------------------------------------------------- the base algorithm over a fake launcher

def test_launch_once_records_intent_then_acknowledgement_and_reconciles_afterwards(layout):
    fake = FakeLauncher(); p = request()
    first = B.launch_once(layout, fake, p, image_digest="sha256:abc", origin="cloudrun")
    assert first["decision"] == "launch" and first["launched"] is True and first["handle"] == f"handle-{p['trial_id']}"
    assert first["launcher"] == "systemd" and first["worker_image_digest"] == "sha256:abc" and first["origin"] == "cloudrun"
    d = layout.trial_dir(p["trial_id"]); launch = json.loads((d / "launch.json").read_text())
    assert launch["phase"] == "acknowledged" and launch["returncode"] == 0 and launch["handle"] == f"handle-{p['trial_id']}"
    assert launch["launcher"] == "systemd" and launch["image_digest"] == "sha256:abc" and launch["origin"] == "cloudrun"
    assert json.loads((d / "request.json").read_text()) == p
    second = B.launch_once(layout, fake, p, image_digest="sha256:abc", origin="cloudrun")
    assert second["decision"] == "reconcile" and second["launched"] is False and second["handle"] == f"handle-{p['trial_id']}" and fake.starts == [p["trial_id"]]


def test_launch_failure_is_recorded_before_it_propagates(layout):
    fake = FakeLauncher(); fake.fail = RuntimeError("api down"); p = request()
    with pytest.raises(B.LaunchFailed, match="api down"): B.launch_once(layout, fake, p)
    launch = json.loads((layout.trial_dir(p["trial_id"]) / "launch.json").read_text())
    assert launch["phase"] == "failed" and "api down" in launch["error"]
    fake.fail = None
    assert B.launch_once(layout, fake, p)["decision"] == "launch-failed" and fake.starts == [p["trial_id"]]   # never a second start


def test_intent_from_another_launcher_reads_as_unknown_and_is_never_relaunched(layout):
    fake = FakeLauncher(); fake.name = "cloudrun"; p = request(); d = layout.trial_dir(p["trial_id"])
    atomic_json(d / "request.json", p)
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "phase": "acknowledged", "returncode": 0, "launcher": "systemd", "handle": "swesim-trial-x"})
    evidence = B.read_evidence(layout, fake, p["trial_id"])
    assert evidence.runtime_state == "unknown" and "launched by systemd" in evidence.runtime_detail
    assert B.launch_once(layout, fake, p)["decision"] == "reconcile" and fake.starts == []


def test_binding_conflict_and_existing_record(layout):
    fake = FakeLauncher(); p = request(); d = layout.trial_dir(p["trial_id"])
    atomic_json(d / "request.json", {**p, "model": "someone-else"})
    with pytest.raises(B.TrialBindingConflict): B.launch_once(layout, fake, p)
    atomic_json(d / "request.json", p); atomic_json(d / "record.json", {"trial_id": p["trial_id"], "status": "completed", "reward": {}})
    assert B.launch_once(layout, fake, p)["decision"] == "ingest" and fake.starts == []
    assert issubclass(B.TrialBindingConflict, ValueError) and issubclass(B.LaunchFailed, RuntimeError) and issubclass(B.RuntimeStateUnavailable, RuntimeError)


def test_get_launcher_follows_settings(tmp_path):
    s = Settings.from_env({"SWESIM_WORKSPACE": str(tmp_path / "w"), "SWESIM_TRIAL_LAUNCHER": "systemd", "SWESIM_ORIGIN": "vm-systemd",
                           "SWESIM_WORKER_IMAGE_DIGEST": "sha256:1", "SWESIM_ENV_FILE": str(tmp_path / "none.env")})
    l = B.get_launcher(s)
    assert isinstance(l, SYS.SystemdLauncher) and l.name == "systemd" and l.workspace == s.workspace and l.origin == "vm-systemd" and l.image_digest == "sha256:1"
    assert isinstance(B.get_launcher(s.model_copy(update={"trial_launcher": "subprocess"})), SP.SubprocessLauncher)


# ---------------------------------------------------------------- systemd launcher over a fake systemctl / systemd-run

def test_systemd_launch_once_then_reconcile_never_runs_systemd_run_twice(layout, launcher, systemd):
    p = request(); unit = "swesim-trial-" + p["trial_id"]
    first = B.launch_once(layout, launcher, p)
    assert first["decision"] == "launch" and first["launched"] is True and first["unit"] == unit and first["handle"] == unit
    runs = [c for c in systemd.calls if c[0] == "systemd-run"]
    assert len(runs) == 1
    argv = runs[0]
    assert argv[:2] == ["systemd-run", "--user"] and f"--unit={unit}" in argv and "--property=RemainAfterExit=yes" in argv
    assert "--collect" not in argv  # a failed unit must stay visible for reconciliation
    launch = json.loads((layout.trial_dir(p["trial_id"]) / "launch.json").read_text())
    assert launch["phase"] == "acknowledged" and launch["returncode"] == 0 and launch["launcher"] == "systemd" and launch["handle"] == unit
    second = B.launch_once(layout, launcher, p)
    assert second["decision"] == "reconcile" and second["launched"] is False and second["runtime"] == "running"
    assert len([c for c in systemd.calls if c[0] == "systemd-run"]) == 1


def test_systemd_argv_carries_paths_only_never_secret_values(layout, launcher, systemd, monkeypatch):
    # Fake values deliberately do not match real credential prefixes (verify.sh scans tracked files for those).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-value-123456"); monkeypatch.setenv("SWESIM_WORKER_SECRET", "fake-worker-secret-xyz")
    monkeypatch.setenv("GH_TOKEN", "fake-github-token-value")
    p = request(); B.launch_once(layout, launcher, p)
    argv = next(c for c in systemd.calls if c[0] == "systemd-run"); joined = " ".join(argv)
    for secret in ("fake-anthropic-value-123456", "fake-worker-secret-xyz", "fake-github-token-value"):
        assert secret not in joined
    assert f"--setenv=SWESIM_ENV_FILE={launcher.env_file}" in argv and f"--setenv=SWESIM_WORKSPACE={launcher.workspace}" in argv
    assert f"--setenv=SWESIM_ORIGIN={launcher.origin}" in argv and "--setenv=PYTHONUNBUFFERED=1" in argv
    # the command inside the unit is the trial runner module with the request path, no inline code
    assert "-m" in argv and "swesim.workflows.activities" in argv and argv[-1].endswith("request.json") and "-c" not in argv
    monkeypatch.setenv("SOME_TOKEN", str(launcher.workspace))  # a secret-looking value equal to an argv part is refused
    with pytest.raises(RuntimeError, match="refusing to launch"):
        SYS.build_systemd_run_argv("u", ["python"], launcher.repo_root, launcher.unit_env())


def test_systemd_launch_failure_is_recorded_and_raised_not_retried(layout, launcher, monkeypatch):
    fake = FakeSystemd(run_returncode=1); monkeypatch.setattr(SYS, "RUN", fake); p = request()
    with pytest.raises(B.LaunchFailed): B.launch_once(layout, launcher, p)
    launch = json.loads((layout.trial_dir(p["trial_id"]) / "launch.json").read_text())
    assert launch["phase"] == "acknowledged" and launch["returncode"] == 1 and "Failed" in launch["stderr"]
    assert B.launch_once(layout, launcher, p)["decision"] == "launch-failed" and len([c for c in fake.calls if c[0] == "systemd-run"]) == 1


def test_systemd_intent_without_unit_is_unresolved(layout, launcher, systemd):
    p = request(); d = layout.trial_dir(p["trial_id"])
    atomic_json(d / "request.json", p); atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "phase": "intent", "launcher": "systemd"})
    assert B.launch_once(layout, launcher, p)["decision"] == "unresolved" and not [c for c in systemd.calls if c[0] == "systemd-run"]


def test_systemctl_failure_is_retryable_error_not_unit_gone(layout, launcher, systemd):
    p = request(); unit = "swesim-trial-" + p["trial_id"]
    atomic_json(layout.trial_dir(p["trial_id"]) / "launch.json", {"trial_id": p["trial_id"], "phase": "acknowledged", "returncode": 0, "launcher": "systemd"})
    systemd.systemctl_failure = "Failed to connect to bus: No medium found"
    with pytest.raises(SYS.UnitStateUnavailable, match="Failed to connect to bus"): B.read_evidence(layout, launcher, p["trial_id"])
    with pytest.raises(SYS.UnitStateUnavailable): B.launch_once(layout, launcher, p)   # launch never guesses either
    assert not [c for c in systemd.calls if c[0] == "systemd-run"]
    systemd.systemctl_failure = None
    systemd.systemctl_rc = 1                                                             # nonzero exit but an explicit not-found is trusted
    assert SYS.unit_state(unit)["load_state"] == "not-found"
    systemd.systemctl_rc = 0; systemd.systemctl_stdout_override = "Result=success\n"
    with pytest.raises(SYS.UnitStateUnavailable): SYS.unit_state(unit)                  # LoadState missing from the output


def test_systemd_cancel_stops_the_unit_and_cleanup_keeps_it(layout, launcher, systemd):
    p = request(); unit = "swesim-trial-" + p["trial_id"]; B.launch_once(layout, launcher, p)
    launcher.cancel(p["trial_id"], {}); launcher.cleanup(p["trial_id"], {})
    assert ["systemctl", "--user", "stop", unit] in systemd.calls and launcher.runtime_state(p["trial_id"], {})[0] == "succeeded"


# ---------------------------------------------------------------- subprocess launcher (non-production local smoke)

def test_subprocess_launcher_end_to_end_and_reconcile(layout, tmp_path):
    l = SP.SubprocessLauncher(workspace=layout.root, registry_path=tmp_path / "r.json", env_file=tmp_path / "env", repo_root=tmp_path,
                              command=lambda req: [sys.executable, "-c", SCRIPT_WRITE_RECORD, str(req)])
    p = request()
    first = B.launch_once(layout, l, p)
    launch = json.loads((layout.trial_dir(p["trial_id"]) / "launch.json").read_text())
    assert first["launched"] is True and launch["launcher"] == "subprocess" and launch["pid"] and launch["handle"] == str(launch["pid"])
    async def wait_done():
        for _ in range(100):
            state, _detail = l.runtime_state(p["trial_id"], launch)
            if state != "running": return state
            await asyncio.sleep(0.05)
        return "running"
    assert asyncio.run(wait_done()) == "succeeded"
    assert (layout.trial_dir(p["trial_id"]) / "record.json").exists() and B.launch_once(layout, l, p)["decision"] == "ingest"
    assert SP.runtime_state_of_pid(None)[0] == "absent" and SP.runtime_state_of_pid(2**22 - 1)[0] == "unknown"
