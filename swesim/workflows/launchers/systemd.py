"""systemd launcher: one transient user unit per trial (`systemd-run --user --unit swesim-trial-<id>`), the VM path.

Contract
- `start` runs systemd-run with `RemainAfterExit=yes` (a finished unit stays visible for reconciliation) and passes
  only paths, the origin label and PATH through `--setenv`; secrets reach the trial through the env file
  (python-dotenv), never the command line: `build_systemd_run_argv` refuses an argv that contains any secret-looking
  environment value.
- `runtime_state` maps `systemctl --user show` onto the protocol: LoadState=not-found -> absent; ActiveState in
  RUNNING_STATES with SubState != exited -> running; ActiveState=failed -> failed; any other loaded unit ->
  succeeded (finished; the record files say whether it produced evidence). A failing systemctl raises
  `UnitStateUnavailable`; only an explicit `LoadState=not-found` means the unit is gone.
- `cancel` stops the unit. `cleanup` is a no-op: a transient unit's state is the evidence and must stay visible.
- `RUN` is the injectable process runner (tests substitute a fake); the launcher resolves it at call time.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from swesim.workflows.launchers.base import LaunchAck, RuntimeState, RuntimeStateUnavailable, build_trial_command, unit_name

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNING_STATES = frozenset({"active", "activating", "reloading", "deactivating"})
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


class UnitStateUnavailable(RuntimeStateUnavailable):
    """systemctl could not report the unit (no bus, missing XDG_RUNTIME_DIR, killed). Retryable; never read as 'unit gone'."""


def RUN(argv: list[str]) -> subprocess.CompletedProcess:  # injectable process runner
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)


def unit_state(unit: str, run: Callable[[list[str]], subprocess.CompletedProcess] | None = None) -> dict:
    """LoadState/ActiveState/SubState/Result of a unit. Only an explicit `LoadState=not-found` means the unit is gone."""
    cp = (run or RUN)(["systemctl", "--user", "show", unit, "--property=LoadState,ActiveState,SubState,Result"])
    props = dict(line.split("=", 1) for line in (cp.stdout or "").splitlines() if "=" in line)
    load = props.get("LoadState")
    if load is None or (cp.returncode != 0 and load != "not-found"):
        raise UnitStateUnavailable(f"systemctl --user show {unit} failed (rc={cp.returncode}): {((cp.stderr or '') + (cp.stdout or ''))[-300:]}")
    return {"load_state": load, "active_state": props.get("ActiveState", "inactive"), "sub_state": props.get("SubState", "dead"),
            "result": props.get("Result", "")}


def runtime_state_of(props: dict) -> tuple[RuntimeState, str]:
    """systemd states -> protocol state; the detail keeps the raw triple so nothing is lost."""
    detail = f"{props['load_state']}/{props['active_state']}/{props['sub_state']}"
    if props["load_state"] == "not-found":
        return "absent", detail
    if props["active_state"] in RUNNING_STATES and props["sub_state"] != "exited":
        return "running", detail
    if props["active_state"] == "failed":
        return "failed", detail
    return "succeeded", detail


def secret_values(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Values of secret-looking variables in the process environment (for redaction and the argv guard)."""
    env = os.environ if environ is None else environ
    return {k: v for k, v in env.items() if len(v) >= 8 and any(m in k.upper() for m in SECRET_MARKERS)}


def assert_no_secret_values(parts: list[str]) -> None:
    joined = "\x00".join(parts)
    for k, v in secret_values().items():
        if v in joined:
            raise RuntimeError(f"refusing to launch: the value of {k} would appear in the unit command line")


def build_systemd_run_argv(unit: str, command: list[str], cwd: Path, env: dict[str, str]) -> list[str]:
    argv = ["systemd-run", "--user", f"--unit={unit}", f"--description=swesim trial {unit}", "--property=RemainAfterExit=yes",
            f"--working-directory={cwd}"]
    argv += [f"--setenv={k}={v}" for k, v in sorted(env.items())]
    argv += list(command)
    assert_no_secret_values(argv)
    return argv


@dataclass(frozen=True)
class SystemdLauncher:
    workspace: Path
    registry_path: Path
    env_file: Path
    origin: str = "vm-systemd"
    image_digest: str | None = None
    repo_root: Path = REPO_ROOT
    command: Callable[[Path], list[str]] = build_trial_command
    run: Callable[[list[str]], subprocess.CompletedProcess] | None = field(default=None, compare=False)
    name: str = "systemd"

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        return (self.run or RUN)(argv)

    def unit_env(self) -> dict[str, str]:
        """The only variables a trial unit receives: paths, the origin label, PATH. Secrets come from the env file."""
        env = {"SWESIM_WORKSPACE": str(self.workspace), "SWESIM_REGISTRY": str(self.registry_path), "SWESIM_ENV_FILE": str(self.env_file),
               "SWESIM_ORIGIN": self.origin, "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "PYTHONUNBUFFERED": "1"}
        if self.image_digest:
            env["SWESIM_WORKER_IMAGE_DIGEST"] = self.image_digest
        return env

    def runtime_state(self, trial_id: str, launch: dict) -> tuple[RuntimeState, str]:
        return runtime_state_of(unit_state(unit_name(trial_id), self._run))

    def start(self, trial_id: str, request_path: Path, intent: dict) -> LaunchAck:
        unit = unit_name(trial_id)
        cp = self._run(build_systemd_run_argv(unit, self.command(request_path), self.repo_root, self.unit_env()))
        return LaunchAck(cp.returncode, cp.stdout or "", cp.stderr or "", unit)

    def cancel(self, trial_id: str, launch: dict) -> None:
        self._run(["systemctl", "--user", "stop", unit_name(trial_id)])

    def cleanup(self, trial_id: str, launch: dict) -> None:
        return None  # the unit's state is the evidence; it stays visible
