from __future__ import annotations
import json, os, subprocess
from pathlib import Path
class HarborRunError(RuntimeError): pass

def run_task(task_dir: Path, agent: str, jobs_dir: Path, job_name: str, model: str | None = None, agent_kwargs: dict | None = None,
             env_provider: str = "modal", timeout_sec: int = 10800) -> Path:
    cmd = ["harbor", "run", "--max-retries", "0", "-p", str(task_dir), "--agent", agent, "-e", env_provider, "--jobs-dir", str(jobs_dir), "--job-name", job_name, "--yes"]
    if model: cmd += ["-m", model]
    for k, v in (agent_kwargs or {}).items(): cmd += ["--ak", f"{k}={v}"]
    env = {k:v for k,v in os.environ.items() if not any(x in k for x in ('GH_TOKEN','GITHUB_TOKEN','SWESIM_WORKER_SECRET'))}
    env.update(PYTHONPATH=str(Path(__file__).resolve().parent.parent) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    if (jobs_dir/job_name).exists():
        matches=list((jobs_dir/job_name).glob("*/result.json"))
        if len(matches)==1: return matches[0].parent
        raise HarborRunError("job exists without a unique terminal result; reconcile, do not relaunch")
    try: proc = subprocess.run(cmd,capture_output=True,text=True,timeout=timeout_sec,env=env)
    except subprocess.TimeoutExpired as e: raise HarborRunError("Harbor wrapper deadline; inspect remote/job state before retry") from e
    trials = [p.parent for p in (jobs_dir/job_name).glob("*/result.json")]
    if len(trials)>1: raise HarborRunError("ambiguous job; never pick a random last trial")
    if not trials: raise HarborRunError(f"no trial dir; harbor stdout tail: {proc.stdout[-1500:]} stderr: {proc.stderr[-1500:]}")
    result = json.loads((trials[-1] / "result.json").read_text())
    # Return agent-error trials too; attribution and denominators need the recorded result.
    if proc.returncode and not result: raise HarborRunError("Harbor failed without result")
    return trials[-1]

def read_reward(trial_dir: Path) -> dict:
    p = trial_dir / "verifier" / "reward.json"; return json.loads(p.read_text()) if p.exists() else {}
def read_report(trial_dir: Path) -> dict:
    p = trial_dir / "verifier" / "report.json"; return json.loads(p.read_text()) if p.exists() else {}
