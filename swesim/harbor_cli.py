"""Thin wrapper over the `harbor run` CLI (Harbor 0.23, Modal backend). One job per control/trial, never retried
here: a job directory that exists is reconciled from its result.json, not relaunched."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


class HarborRunError(RuntimeError):
    pass


SECRET_ENV_MARKERS = ("GH_TOKEN", "GITHUB_TOKEN", "SWESIM_WORKER_SECRET")


def harbor_executable() -> str:
    """Prefer the `harbor` next to the running interpreter (the uv project env), then PATH."""
    sibling = Path(sys.executable).with_name("harbor")
    if sibling.is_file(): return str(sibling)
    found = shutil.which("harbor")
    if found: return found
    raise HarborRunError("harbor executable not found next to the interpreter or on PATH; run inside the project environment (uv run)")


def _terminal_trials(job_dir: Path) -> list[Path]:
    return sorted(p.parent for p in job_dir.glob("*/result.json"))


def run_task(task_dir: Path, agent: str, jobs_dir: Path, job_name: str, model: str | None = None, agent_kwargs: dict | None = None,
             env_provider: str = "modal", timeout_sec: int = 10800) -> Path:
    """Launch one Harbor job and return its trial directory. Agent-error trials are returned too (attribution needs
    the recorded result); missing or ambiguous results raise HarborRunError."""
    jobs_dir = Path(jobs_dir); job_dir = jobs_dir / job_name
    if job_dir.exists():
        matches = _terminal_trials(job_dir)
        if len(matches) == 1: return matches[0]
        raise HarborRunError(f"job {job_dir} exists without a unique terminal result; reconcile, do not relaunch")
    cmd = [harbor_executable(), "run", "--max-retries", "0", "-p", str(task_dir), "--agent", agent, "-e", env_provider,
           "--jobs-dir", str(jobs_dir), "--job-name", job_name, "--yes"]
    if model: cmd += ["-m", model]
    for k, v in (agent_kwargs or {}).items(): cmd += ["--ak", f"{k}={v}"]
    env = {k: v for k, v in os.environ.items() if not any(marker in k for marker in SECRET_ENV_MARKERS)}
    root = str(Path(__file__).resolve().parent.parent)  # so custom agent import paths resolve in harbor's process
    env["PYTHONPATH"] = root + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    jobs_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, env=env)
    except subprocess.TimeoutExpired as e:
        raise HarborRunError("Harbor wrapper deadline; inspect remote/job state before retry") from e
    trials = _terminal_trials(job_dir)
    if len(trials) > 1: raise HarborRunError("ambiguous job; never pick a random last trial")
    if not trials: raise HarborRunError(f"no trial dir; harbor rc={proc.returncode} stdout tail: {proc.stdout[-1500:]} stderr: {proc.stderr[-1500:]}")
    result = json.loads((trials[0] / "result.json").read_text() or "null")
    if proc.returncode and not result: raise HarborRunError("Harbor failed without result")
    return trials[0]


def read_reward(trial_dir: Path) -> dict:
    p = Path(trial_dir) / "verifier" / "reward.json"; return json.loads(p.read_text()) if p.exists() else {}


def read_report(trial_dir: Path) -> dict:
    p = Path(trial_dir) / "verifier" / "report.json"; return json.loads(p.read_text()) if p.exists() else {}
