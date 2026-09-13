"""Trial launcher protocol: start a trial's runtime exactly once, read its evidence, decide without side effects.

Contract (interfaces §3)
- `TrialEvidence` is everything a decision needs: the record files under trials/<tid>/ plus the runtime's state, one
  of absent | running | succeeded | failed | unknown, with a launcher-specific detail string (systemd states, the Cloud
  Run execution name and condition, a pid).
- `decide_launch(ev)` is pure. Only "launch" may start a solver; every other branch reads existing state. Any launch
  evidence (launch.json in any phase, a runtime that exists or existed, a status.json) means: never relaunch.
- `launch_once(layout, launcher, p)` writes request.json and launch.json (phase intent) before it calls the launcher,
  so a lost acknowledgement is reconciled, never relaunched; afterwards it records the acknowledgement (phase
  acknowledged with the launcher's handle, or failed). A non-zero acknowledgement raises `LaunchFailed`; callers never
  retry blindly.
- A launcher that cannot report the runtime's state raises `RuntimeStateUnavailable` (retryable); it never guesses
  "absent". launch.json written by a different launcher (a VM-era systemd record read by a Cloud Run worker) reads as
  runtime "unknown": reconcile by hand, never relaunch.
- launch.json carries `launcher`, `handle`, `image_digest` and `origin`: a record names the runtime and the code that
  produced it.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from swesim.paths import atomic_json
from swesim.settings import Settings

RuntimeState = Literal["absent", "running", "succeeded", "failed", "unknown"]
LauncherName = Literal["systemd", "cloudrun", "subprocess"]
Decision = Literal["ingest", "runner-error", "reconcile", "unresolved", "launch-failed", "launch"]
# A runtime that exists, existed, or cannot be seen: inspect it, never start another.
LIVE_STATES: frozenset[str] = frozenset({"running", "succeeded", "failed", "unknown"})


class TrialBindingConflict(ValueError):
    """request.json for this trial id already binds a different request."""


class LaunchFailed(RuntimeError):
    """The launcher did not start the runtime. Never retried automatically; reconcile first."""


class RuntimeStateUnavailable(RuntimeError):
    """The launcher could not report the runtime's state (no bus, API unreachable). Retryable; never read as 'absent'."""


class LauncherConfigError(ValueError):
    """The launcher cannot be built from these settings (missing image, service account or project)."""


@dataclass(frozen=True)
class LaunchAck:
    returncode: int
    stdout: str
    stderr: str
    handle: str  # systemd unit | Cloud Run execution name | pid


@dataclass(frozen=True)
class TrialEvidence:
    has_record: bool
    has_runner_error: bool
    status: str | None            # status.json written by swesim.stages.run (running | completed | launch-failed)
    launch_phase: str | None      # launch.json written here: intent | acknowledged | failed
    launch_returncode: int | None
    runtime_state: RuntimeState
    runtime_detail: str


class TrialLayout(Protocol):
    def trial_dir(self, trial_id: str) -> Path: ...


@dataclass(frozen=True)
class WorkspaceLayout:
    """`trial_dir` over a workspace root; `swesim.storage.layout.Layout` satisfies the same protocol once merged."""

    root: Path

    def trial_dir(self, trial_id: str) -> Path:
        return Path(self.root) / "trials" / trial_id


class Launcher(Protocol):
    name: LauncherName

    def runtime_state(self, trial_id: str, launch: dict) -> tuple[RuntimeState, str]: ...

    def start(self, trial_id: str, request_path: Path, intent: dict) -> LaunchAck: ...

    def cancel(self, trial_id: str, launch: dict) -> None: ...

    def cleanup(self, trial_id: str, launch: dict) -> None: ...


def unit_name(trial_id: str) -> str:
    return "swesim-trial-" + trial_id


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def build_trial_command(request_path: Path, python: str | None = None) -> list[str]:
    """The per-trial process: the runner module with the request path; no inline code, no secrets."""
    return [python or sys.executable, "-m", "swesim.workflows.activities", "run-trial", str(request_path)]


def decide_launch(ev: TrialEvidence) -> Decision:
    """Pure decision. Only "launch" may start a solver; every other branch reads existing state."""
    if ev.has_record or (ev.status and ev.status != "running"):
        return "ingest"
    if ev.has_runner_error:
        return "runner-error"
    if ev.runtime_state in LIVE_STATES:
        return "reconcile"  # the runtime exists (running, finished or unseeable): inspect it, never start another
    if ev.launch_phase == "acknowledged" and ev.launch_returncode not in (None, 0):
        return "launch-failed"  # the launcher refused; nothing ran
    if ev.launch_phase in ("intent", "acknowledged") or ev.status == "running":
        return "unresolved"  # a launch was attempted and the runtime is gone (VM loss, reboot, manual cleanup): reconcile by hand
    if ev.launch_phase == "failed":
        return "launch-failed"
    return "launch"


def runtime_state_for(launcher: Launcher, trial_id: str, launch: dict) -> tuple[RuntimeState, str]:
    """The active launcher's view, unless launch.json names a different launcher: then "unknown" (never a guess)."""
    recorded = launch.get("launcher")
    if recorded and recorded != launcher.name:
        return "unknown", f"launched by {recorded}; this worker runs {launcher.name}"
    return launcher.runtime_state(trial_id, launch)


def read_evidence(layout: TrialLayout, launcher: Launcher, trial_id: str) -> TrialEvidence:
    d = layout.trial_dir(trial_id)
    launch = read_json(d / "launch.json")
    state, detail = runtime_state_for(launcher, trial_id, launch)
    return TrialEvidence(has_record=(d / "record.json").exists(), has_runner_error=(d / "runner-error.json").exists(),
                         status=read_json(d / "status.json").get("status"), launch_phase=launch.get("phase"),
                         launch_returncode=launch.get("returncode"), runtime_state=state, runtime_detail=detail)


def _boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def launch_once(layout: TrialLayout, launcher: Launcher, p: dict, *, image_digest: str | None = None, origin: str | None = None) -> dict:
    """Launch the trial's runtime exactly once; otherwise report the reconciliation decision without side effects."""
    tid = p["trial_id"]; unit = unit_name(tid); d = layout.trial_dir(tid); d.mkdir(parents=True, exist_ok=True)
    request = d / "request.json"
    if request.exists() and json.loads(request.read_text()) != p:
        raise TrialBindingConflict(f"trial {tid} already bound to a different request")
    ev = read_evidence(layout, launcher, tid); decision = decide_launch(ev)
    recorded = read_json(d / "launch.json")
    out = {"trial_id": tid, "unit": unit, "decision": decision, "launched": False, "launcher": launcher.name,
           "handle": recorded.get("handle"), "runtime": ev.runtime_state, "worker_image_digest": image_digest, "origin": origin}
    if decision != "launch":
        return out
    atomic_json(request, p)
    intent = {"trial_id": tid, "unit": unit, "phase": "intent", "launcher": launcher.name, "started_at": time.time(), "boot_id": _boot_id(),
              "image_digest": image_digest, "origin": origin}
    atomic_json(d / "launch.json", intent)  # before the launcher runs: a lost acknowledgement is reconciled, never relaunched
    try:
        ack = launcher.start(tid, request, intent)
    except Exception as e:  # noqa: BLE001 - every launcher failure is recorded before it propagates as LaunchFailed
        atomic_json(d / "launch.json", {**intent, "phase": "failed", "error": repr(e)})
        raise LaunchFailed(f"launcher {launcher.name} failed for {unit}: {e}") from e
    atomic_json(d / "launch.json", {**intent, "phase": "acknowledged", "returncode": ack.returncode, "stdout": ack.stdout[-500:],
                                    "stderr": ack.stderr[-1000:], "handle": ack.handle})
    if ack.returncode:
        raise LaunchFailed(f"launch of {unit} returned {ack.returncode}; reconcile before any new attempt")
    return {**out, "launched": True, "handle": ack.handle}


def get_launcher(s: Settings) -> Launcher:
    """The launcher `Settings.trial_launcher` names, built from the settings; `LauncherConfigError` when it cannot be."""
    if s.trial_launcher == "systemd":
        from swesim.workflows.launchers.systemd import SystemdLauncher
        return SystemdLauncher(workspace=s.workspace, registry_path=s.registry_path, env_file=s.env_file, origin=s.origin,
                               image_digest=s.worker_image_digest)
    if s.trial_launcher == "subprocess":
        from swesim.workflows.launchers.subprocess import SubprocessLauncher
        return SubprocessLauncher(workspace=s.workspace, registry_path=s.registry_path, env_file=s.env_file, origin=s.origin,
                                  image_digest=s.worker_image_digest)
    if s.trial_launcher == "cloudrun":
        from swesim.workflows.launchers.cloudrun import CloudRunLauncher
        return CloudRunLauncher.from_settings(s)
    raise LauncherConfigError(f"unknown trial launcher {s.trial_launcher!r}")
