"""subprocess launcher: a detached child process per trial for local smoke only. NOT production.

It loses the runtime-state evidence across worker restarts (a pid is all it records, and pids are reused), so
`runtime_state` answers "unknown" for a pid this process did not start and can no longer see. Everything else follows
the protocol: the intent is recorded before the child starts, the pid lands in launch.json, cancel sends SIGTERM.
"""
from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from swesim.workflows.launchers.base import LaunchAck, RuntimeState, build_trial_command
from swesim.workflows.launchers.systemd import REPO_ROOT

_CHILDREN: dict[int, subprocess.Popen] = {}


def runtime_state_of_pid(pid: int | None) -> tuple[RuntimeState, str]:
    if not pid:
        return "absent", "no pid recorded"
    proc = _CHILDREN.get(pid)
    if proc is not None:
        rc = proc.poll()
        if rc is None:
            return "running", f"pid {pid}"
        return ("succeeded" if rc == 0 else "failed"), f"pid {pid} exited {rc}"
    try:
        os.kill(pid, 0)
        return "running", f"pid {pid} (not a child of this process)"
    except ProcessLookupError:
        return "unknown", f"pid {pid} gone; exit status unknown to this process"
    except PermissionError:
        return "running", f"pid {pid} (another user's process)"


@dataclass(frozen=True)
class SubprocessLauncher:
    workspace: Path
    registry_path: Path
    env_file: Path
    origin: str = "laptop"
    image_digest: str | None = None
    repo_root: Path = REPO_ROOT
    command: Callable[[Path], list[str]] = build_trial_command
    name: str = "subprocess"

    def child_env(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in ("HOME", "USER", "LANG", "TMPDIR") if k in os.environ}  # same shape as the systemd path
        env.update({"SWESIM_WORKSPACE": str(self.workspace), "SWESIM_REGISTRY": str(self.registry_path), "SWESIM_ENV_FILE": str(self.env_file),
                    "SWESIM_ORIGIN": self.origin, "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "PYTHONUNBUFFERED": "1"})
        if self.image_digest:
            env["SWESIM_WORKER_IMAGE_DIGEST"] = self.image_digest
        return env

    def runtime_state(self, trial_id: str, launch: dict) -> tuple[RuntimeState, str]:
        return runtime_state_of_pid(launch.get("pid"))

    def start(self, trial_id: str, request_path: Path, intent: dict) -> LaunchAck:
        log = open(request_path.parent / "launcher.log", "ab")  # noqa: SIM115 - inherited by the detached child
        proc = subprocess.Popen(self.command(request_path), cwd=str(self.repo_root), stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True, env=self.child_env())
        log.close(); _CHILDREN[proc.pid] = proc; intent["pid"] = proc.pid
        return LaunchAck(0, "", f"detached pid {proc.pid} via subprocess launcher (NOT production)", str(proc.pid))

    def cancel(self, trial_id: str, launch: dict) -> None:
        pid = launch.get("pid")
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                return None

    def cleanup(self, trial_id: str, launch: dict) -> None:
        _CHILDREN.pop(launch.get("pid") or -1, None)
