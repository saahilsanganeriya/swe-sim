"""Trial launchers: one protocol (`base`), three runtimes.

- `systemd`   : one transient user unit per trial on the VM (production before the Cloud Run cutover).
- `cloudrun`  : one Cloud Run Job per trial (production on GCP), launch-once guarded by the Job name.
- `subprocess`: a detached child process for local smoke only; NOT production.

Callers use `base.launch_once`, `base.read_evidence`, `base.decide_launch` and `base.get_launcher(settings)`.
"""
from __future__ import annotations

from swesim.workflows.launchers.base import (LaunchAck, Launcher, LauncherConfigError, LaunchFailed, RuntimeStateUnavailable,
                                             TrialBindingConflict, TrialEvidence, decide_launch, get_launcher, launch_once,
                                             read_evidence, unit_name)

__all__ = ["LaunchAck", "Launcher", "LauncherConfigError", "LaunchFailed", "RuntimeStateUnavailable", "TrialBindingConflict",
           "TrialEvidence", "decide_launch", "get_launcher", "launch_once", "read_evidence", "unit_name"]
