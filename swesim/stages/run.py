"""Run stage: one trial end to end (spec section 7, plan Task 9).

Materializes a per-trial copy of the compiled condition task with the session token and trial id in
``[environment.env]``, launches Harbor through ``swesim.harbor_cli``, then fetches the frozen stakeholder
transcript and binds its hash to the collector receipt recorded in the grader report. Writes
``workspace/trials/<trial_id>/{run_config,spec,reward,report,transcript,record,status}.json``.
"""
from __future__ import annotations

import json, shutil, tomllib, urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import tomli_w

from swesim import harbor_cli
from swesim.models import RunConfig, ScenarioSpec
from swesim.models import trial_id as make_trial_id
from swesim.paths import Workspace
from swesim.stakeholder.view import visible_view

PROVIDER_HOSTS = {"anthropic": "api.anthropic.com", "openai": "api.openai.com"}
OUTCOME_NAMES = {3: "agent-invalid-submission", 4: "agent-runtime-failure", 7: "evaluator-failure"}
HTTP_TIMEOUT = 120


@dataclass
class TrialRecord:
    trial_id: str; scenario_id: str; condition: str; attempt_index: int; run_config_hash: str; task_dir: str; trial_dir: str | None
    reward: dict; report: dict; transcript: dict; status: str
    spec_hash: str = ""; model: str = ""; purpose: str = "demonstration"; compiled_hash: str = ""; brief_profile: str = "natural"
    started_at: str = ""; finished_at: str | None = None      # ISO-8601 UTC; appended last so positional construction is unchanged


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def materialize_trial_task(ws: Workspace, compiled_task_dir: Path, tid: str, token: str) -> Path:
    """Copy the compiled condition task to workspace/trials/<tid>/task and bind the session to it."""
    dst = ws.root / "trials" / tid / "task"
    if dst.exists(): raise ValueError("trial task already exists; reconcile before materialization")
    shutil.copytree(compiled_task_dir, dst); dst.chmod(0o700)
    p = dst / "task.toml"; cfg = tomllib.loads(p.read_text())
    env = cfg.setdefault("environment", {}).setdefault("env", {})
    env["SWESIM_SESSION_TOKEN"] = token; env["SWESIM_TRIAL_ID"] = tid
    cfg.setdefault("metadata", {})["trial_id"] = tid
    p.write_text(tomli_w.dumps(cfg)); p.chmod(0o600)
    return dst


def _post(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r: return json.load(r)


def _get(url: str, headers: dict) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=HTTP_TIMEOUT) as r: return json.load(r)


def render_instruction(spec: ScenarioSpec, rc: RunConfig) -> str:
    """Re-render instruction.md for the effective brief profile with the compile stream's template."""
    from jinja2 import Environment, FileSystemLoader, StrictUndefined
    from swesim.stages.compile import TEMPLATES, visible_facts   # owned by the compile stream; imported lazily
    natural = next(b for b in spec.briefs if b.profile == "natural")
    selected = next((b for b in spec.briefs if b.profile == rc.brief_profile), natural)
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), keep_trailing_newline=True, undefined=StrictUndefined)
    return env.get_template("instruction.md.j2").render(
        spec=spec, run_config=rc, condition=rc.condition, task_name=f"{spec.id}--{rc.condition}",
        brief=natural.text if rc.condition == "full" else selected.text, visible_facts=visible_facts(spec),
        repo_dir=spec.environment.repo_dir, solver_minutes=rc.solver_timeout_sec // 60)


def run_trial(ws: Workspace, spec: ScenarioSpec, rc: RunConfig, attempt_index: int, stakeholder_url: str, worker_secret: str) -> TrialRecord:
    started_at = _now()
    sdir = ws.scenario_dir(spec.id)
    comp = json.loads((sdir / "compile.json").read_text()); val = json.loads((sdir / "validation.json").read_text())
    if val["status"] != "valid" or val["spec_hash"] != spec.spec_hash or val["compiled_hash"] != comp["compiled_hash"]:
        raise ValueError("scenario/build not validated")
    profile = "natural" if rc.condition == "full" or not any(b.profile == rc.brief_profile for b in spec.briefs) else rc.brief_profile
    rc = RunConfig.model_validate({**rc.model_dump(), "compiled_hash": comp["compiled_hash"], "brief_profile": profile})
    tid = make_trial_id(spec.spec_hash, rc.condition, rc.run_config_hash, attempt_index)
    tdir = ws.root / "trials" / tid; prior = tdir / "record.json"
    if prior.exists(): return TrialRecord(**json.loads(prior.read_text()))

    compiled = ws.task_dir(spec.id, rc.condition); token = ""
    tc = tomllib.loads((compiled / "task.toml").read_text())
    expected_host = urlparse(stakeholder_url).hostname if stakeholder_url else None
    if rc.condition == "interactive" and (not expected_host or expected_host not in tc["environment"]["allowed_hosts"]):
        raise ValueError("recompile and validate for actual stakeholder endpoint")
    if int(tc["agent"]["timeout_sec"]) != rc.solver_timeout_sec: raise ValueError("compiled timeout differs from run config")
    provider = rc.model.split("/")[0]
    if provider not in PROVIDER_HOSTS: raise ValueError(f"unknown model provider {provider!r}")
    provider_host = PROVIDER_HOSTS[provider]

    if rc.condition == "interactive":
        token = _post(f"{stakeholder_url}/sessions",
                      {"trial_id": tid, "spec_hash": spec.spec_hash, "condition": rc.condition, "run_config_hash": rc.run_config_hash,
                       "visible_view": visible_view(spec), "stakeholder_config": rc.stakeholder.model_dump(), "image": spec.environment.image},
                      {"X-Worker-Secret": worker_secret})["token"]
    task = materialize_trial_task(ws, compiled, tid, token)
    tc = tomllib.loads((task / "task.toml").read_text())
    tc["metadata"].update(run_config_hash=rc.run_config_hash, compiled_hash=rc.compiled_hash, condition=rc.condition, brief_profile=rc.brief_profile)
    (task / "instruction.md").write_text(render_instruction(spec, rc))
    for phase in ("environment", "agent"):
        tc[phase]["allowed_hosts"] = sorted(set(tc[phase].get("allowed_hosts", [])) | {provider_host})
    (task / "task.toml").write_text(tomli_w.dumps(tc))
    (tdir / "status.json").write_text(json.dumps({"status": "running", "trial_id": tid, "started_at": started_at, "finished_at": None}))

    status = "completed"; trial_dir: Path | None = None; reward: dict = {}; report: dict = {}; transcript: dict = {}
    try:
        trial_dir = harbor_cli.run_task(task, rc.agent, ws.jobs_dir / "runs", tid, model=rc.model, timeout_sec=rc.solver_timeout_sec + 7200)
        reward = harbor_cli.read_reward(trial_dir); report = harbor_cli.read_report(trial_dir)
        raw = json.loads((trial_dir / "result.json").read_text()); report["harbor_exception"] = raw.get("exception_info")
        if raw.get("exception_info"):
            name = str(raw["exception_info"].get("exception_type", ""))
            code = 3 if "AgentTimeout" in name else 4 if "Agent" in name else 7
            reward.update(reward=0.0, success=0, outcome_code=code); report["outcome"] = OUTCOME_NAMES[code]
        if not reward: reward = {"reward": 0.0, "success": 0, "outcome_code": 7}
    except harbor_cli.HarborRunError as e:
        status = "launch-failed"; report = {"error": str(e), "outcome": "evaluator-failure"}; reward = {"reward": 0.0, "success": 0, "outcome_code": 7}

    if rc.condition == "interactive":
        try: _post(f"{stakeholder_url}/freeze", {}, {"Authorization": f"Bearer {token}"})   # safety net; collect.py froze already
        except Exception: pass
        try: transcript = _get(f"{stakeholder_url}/transcript/{tid}", {"X-Worker-Secret": worker_secret})
        except Exception as e: transcript = {"fetch_error": type(e).__name__}
        bound = report.get("transcript_hash")
        if not bound or bound != transcript.get("transcript_hash") or not transcript.get("frozen"):
            report["transcript_binding_valid"] = False; report["training_eligible"] = False
            # Keep correctness scores; a missing transcript does not make a code failure disappear.
        else:
            report["transcript_binding_valid"] = True
        if transcript.get("service_failure"):
            report["service_failure_evidence"] = transcript["service_failure"]; report["training_eligible"] = False
        if bound and transcript.get("transcript_hash") and bound != transcript["transcript_hash"]:
            report["transcript_hash_mismatch"] = True
    report["reviewed_equals_submitted"] = None
    report["image_digest"] = comp.get("image_digest")
    report["simulator_validity"] = "unadjudicated" if rc.condition == "interactive" else "n/a"

    finished_at = _now()
    rec = TrialRecord(tid, spec.id, rc.condition, attempt_index, rc.run_config_hash, str(task), str(trial_dir) if trial_dir else None,
                      reward, report, transcript, status, spec.spec_hash, rc.model, rc.purpose, rc.compiled_hash, rc.brief_profile,
                      started_at, finished_at)
    for name, obj in (("run_config.json", rc.model_dump(mode="json")), ("spec.json", spec.model_dump(mode="json")), ("reward.json", reward),
                      ("report.json", report), ("transcript.json", transcript), ("record.json", asdict(rec))):
        (tdir / name).write_text(json.dumps(obj, indent=2))
    (tdir / "status.json").write_text(json.dumps({"status": status, "trial_id": tid, "started_at": started_at, "finished_at": finished_at}))
    return rec
