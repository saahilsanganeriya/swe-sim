"""Thin wrapper over the `harbor run` CLI (Harbor 0.23, Modal backend). One job per control/trial: a job directory that
holds a unique terminal result is reconciled from its result.json, never relaunched.

Two callers, two reuse rules (review P2-17):
- graded trials (`stages.run`, launch-once semantics): a job dir without a unique terminal result is unknown launch state
  and only reconciles (`max_fresh_attempts=0`, the default);
- probes and validation controls opt into bounded fresh attempts: when the job dir exists without a terminal result and no
  Harbor process in this worker runs it, the launch was abandoned (a worker-pool roll or a wrapper deadline killed it; Cloud
  Run Jobs and pools share no processes, so after a restart "exists without a result" means abandoned). The next attempt runs
  in `<jobs_dir>/<job_name>--attempt-<n>` (n <= MAX_FRESH_ATTEMPTS) and its lineage (previous dir, reason, timestamp, worker
  image digest) is appended to `<jobs_dir>/<job_name>.attempts.json`; the abandoned directory is never deleted or written.
  The occurrence after the last allowed attempt raises HarborRunError with the lineage in the message.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


class HarborRunError(RuntimeError):
    pass


SECRET_ENV_MARKERS = ("GH_TOKEN", "GITHUB_TOKEN", "SWESIM_WORKER_SECRET")
MAX_FRESH_ATTEMPTS = 3  # attempt dirs an abandoned probe/control may spawn; the 4th abandonment is a hard reconcile error
ABANDONED_REASON = "abandoned: job dir exists without a terminal result and no Harbor process in this worker runs it"
_LIVE: set[str] = set()  # job dirs with a Harbor subprocess running in this process (a live launch is never "abandoned")


def harbor_executable() -> str:
    """Prefer the `harbor` next to the running interpreter (the uv project env), then PATH."""
    sibling = Path(sys.executable).with_name("harbor")
    if sibling.is_file(): return str(sibling)
    found = shutil.which("harbor")
    if found: return found
    raise HarborRunError("harbor executable not found next to the interpreter or on PATH; run inside the project environment (uv run)")


def _terminal_trials(job_dir: Path) -> list[Path]:
    return sorted(p.parent for p in job_dir.glob("*/result.json"))


def _read_result(trial_dir: Path):
    try:
        return json.loads((trial_dir / "result.json").read_text() or "null")
    except (OSError, ValueError) as e:
        raise HarborRunError(f"malformed result.json in {trial_dir}: {e}") from e


def attempt_job_dir(jobs_dir: Path, job_name: str, n: int) -> Path:
    """The job dir of attempt `n`: the original for 0, `<job_name>--attempt-<n>` beside it otherwise."""
    return Path(jobs_dir) / (job_name if n == 0 else f"{job_name}--attempt-{n}")


def job_dirs(jobs_dir: Path, job_name: str, max_fresh_attempts: int = MAX_FRESH_ATTEMPTS) -> list[Path]:
    """Every directory a job's evidence may live in (original first), for ownership checks by callers."""
    return [attempt_job_dir(jobs_dir, job_name, n) for n in range(max_fresh_attempts + 1)]


def lineage_path(jobs_dir: Path, job_name: str) -> Path:
    return Path(jobs_dir) / f"{job_name}.attempts.json"


def read_lineage(jobs_dir: Path, job_name: str) -> dict:
    p = lineage_path(jobs_dir, job_name)
    if not p.exists():
        return {"job_name": job_name, "attempts": []}
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise HarborRunError(f"malformed lineage {p}: {e}") from e


def _worker_image_digest() -> str | None:
    from swesim.settings import get_settings  # settings.py owns the environment read

    return get_settings().worker_image_digest


def _record_attempt(jobs_dir: Path, job_name: str, n: int, job_dir: Path, previous: Path, image_digest: str | None) -> None:
    from swesim.paths import atomic_json

    lineage = read_lineage(jobs_dir, job_name)
    lineage["attempts"] = [*lineage.get("attempts", []),
                           {"attempt": n, "job_dir": str(job_dir), "previous_dir": str(previous), "reason": ABANDONED_REASON,
                            "at": datetime.now(timezone.utc).isoformat(), "worker_image_digest": image_digest if image_digest is not None else _worker_image_digest()}]
    atomic_json(lineage_path(jobs_dir, job_name), lineage)


def _lineage_text(jobs_dir: Path, job_name: str, abandoned: list[Path]) -> str:
    entries = read_lineage(jobs_dir, job_name).get("attempts", [])
    recorded = "; ".join(f"attempt {e.get('attempt')} at {e.get('at')} after {e.get('previous_dir')} (worker image {e.get('worker_image_digest')})" for e in entries) or "none"
    return f"abandoned dirs: {', '.join(map(str, abandoned))}; lineage {lineage_path(jobs_dir, job_name)}: {recorded}"


def run_task(task_dir: Path, agent: str, jobs_dir: Path, job_name: str, model: str | None = None, agent_kwargs: dict | None = None,
             env_provider: str = "modal", timeout_sec: int = 10800, max_fresh_attempts: int = 0, image_digest: str | None = None) -> Path:
    """Launch one Harbor job and return its trial directory. Agent-error trials are returned too (attribution needs
    the recorded result); missing or ambiguous results raise HarborRunError.

    `max_fresh_attempts` (0 for graded trials) bounds the `<job_name>--attempt-<n>` dirs an abandoned job may spawn; see the
    module docstring. `image_digest` is the worker image recorded in the lineage (defaults to the settings' digest)."""
    jobs_dir = Path(jobs_dir); abandoned: list[Path] = []
    for n in range(max(0, max_fresh_attempts) + 1):
        job_dir = attempt_job_dir(jobs_dir, job_name, n)
        if not job_dir.exists():
            exe = harbor_executable()  # resolved before the lineage is written: an entry means a launch was started
            if n: _record_attempt(jobs_dir, job_name, n, job_dir, abandoned[-1], image_digest)
            return _launch(exe, task_dir, agent, jobs_dir, job_dir, model, agent_kwargs, env_provider, timeout_sec)
        matches = _terminal_trials(job_dir)
        if len(matches) == 1:
            _read_result(matches[0]); return matches[0]
        if matches or not max_fresh_attempts:  # several results, or a launch-once caller: unknown launch state only reconciles
            raise HarborRunError(f"job {job_dir} exists without a unique terminal result; reconcile, do not relaunch")
        if str(job_dir.resolve()) in _LIVE:
            raise HarborRunError(f"job {job_dir} is running in this worker without a terminal result yet; reconcile, do not relaunch")
        abandoned.append(job_dir)
    raise HarborRunError(f"job {job_name} in {jobs_dir}: {len(abandoned)} abandoned launches exhaust the {max_fresh_attempts} fresh attempts; "
                         f"{_lineage_text(jobs_dir, job_name, abandoned)}; reconcile, do not relaunch")


def _launch(exe: str, task_dir: Path, agent: str, jobs_dir: Path, job_dir: Path, model: str | None, agent_kwargs: dict | None,
            env_provider: str, timeout_sec: int) -> Path:
    cmd = [exe, "run", "--max-retries", "0", "-p", str(task_dir), "--agent", agent, "-e", env_provider,
           "--jobs-dir", str(jobs_dir), "--job-name", job_dir.name, "--yes"]
    if model: cmd += ["-m", model]
    for k, v in (agent_kwargs or {}).items(): cmd += ["--ak", f"{k}={v}"]
    env = {k: v for k, v in os.environ.items() if not any(marker in k for marker in SECRET_ENV_MARKERS)}
    root = str(Path(__file__).resolve().parent.parent)  # so custom agent import paths resolve in harbor's process
    env["PYTHONPATH"] = root + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    jobs_dir.mkdir(parents=True, exist_ok=True)
    live = str(job_dir.resolve()); _LIVE.add(live)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, env=env)
    except subprocess.TimeoutExpired as e:
        raise HarborRunError("Harbor wrapper deadline; inspect remote/job state before retry") from e
    finally:
        _LIVE.discard(live)
    trials = _terminal_trials(job_dir)
    if len(trials) > 1: raise HarborRunError("ambiguous job; never pick a random last trial")
    if not trials: raise HarborRunError(f"no trial dir; harbor rc={proc.returncode} stdout tail: {proc.stdout[-1500:]} stderr: {proc.stderr[-1500:]}")
    result = _read_result(trials[0])
    if proc.returncode and not result: raise HarborRunError("Harbor failed without result")
    return trials[0]


def read_reward(trial_dir: Path) -> dict:
    p = Path(trial_dir) / "verifier" / "reward.json"; return json.loads(p.read_text()) if p.exists() else {}


def read_report(trial_dir: Path) -> dict:
    p = Path(trial_dir) / "verifier" / "report.json"; return json.loads(p.read_text()) if p.exists() else {}
