"""Run stage: one trial end to end (spec section 7, plan Task 9).

Materializes a per-trial copy of the compiled condition task with the session token and trial id in
``[environment.env]``, launches Harbor through ``swesim.harbor_cli``, then fetches the frozen stakeholder
transcript and binds its hash to the collector receipt recorded in the grader report. Writes
``workspace/trials/<trial_id>/{run_config,spec,reward,report,transcript,record,status}.json``.

Providers: the solver id follows `swesim.roles.parse_model_id` (``anthropic/…``, ``openai/…``, ``openrouter/<vendor>/<model>``);
the provider's API host is added to both allowlists of the materialized task (`PROVIDER_HOSTS`) and the id is passed to
Harbor verbatim (mini-swe-agent/litellm form). Harbor's mini-swe-agent adapter reads the provider key named by
`agent_env_names` from the launcher's process environment and forwards it into the agent container under the same
name, so `run_trial` refuses to start (`ProviderKeyMissing`, before any session or file) when that key is unavailable.

Cost: after Harbor returns, the solver's usage is read from the trial directory (`costs.usage_from_trial_dir`) and
recorded to ``trials/<trial_id>/costs.json``; ``record.json`` carries ``cost_usd/input_tokens/output_tokens/price_source``
(all None when the trial left no usage evidence: unknown is never written as zero).
"""
from __future__ import annotations

import hashlib
import json, shutil, tomllib, urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import tomli_w

from swesim import costs, harbor_cli
from swesim.models import RunConfig, ScenarioSpec
from swesim.models import trial_id as make_trial_id
from swesim.paths import Workspace
from swesim.roles import PROVIDERS, parse_model_id
from swesim.settings import Settings
from swesim.settings import has_secret as _has_secret
from swesim.stakeholder.view import forbidden_terms, visible_view
from swesim.stages.compile import verify_compiled_artifacts

PROVIDER_HOSTS = {name: p.host for name, p in PROVIDERS.items()}   # anthropic, openai, openrouter -> API host for the sandbox allowlist
OUTCOME_NAMES = {3: "agent-invalid-submission", 4: "agent-runtime-failure", 7: "evaluator-failure"}
HTTP_TIMEOUT = 120


class ProviderKeyMissing(ValueError):
    """The solver's provider credential is neither in the launcher's environment nor in its env file. Configuration, not a
    transient failure: nothing is launched and no trial state is created."""


@dataclass
class TrialRecord:
    trial_id: str; scenario_id: str; condition: str; attempt_index: int; run_config_hash: str; task_dir: str; trial_dir: str | None
    reward: dict; report: dict; transcript: dict; status: str
    spec_hash: str = ""; model: str = ""; purpose: str = "demonstration"; compiled_hash: str = ""; brief_profile: str = "natural"
    started_at: str = ""; finished_at: str | None = None      # ISO-8601 UTC; appended last so positional construction is unchanged
    # Solver cost (interfaces section 6), appended after the timestamps; None = no usage evidence or no verified price.
    cost_usd: float | None = None; input_tokens: int | None = None; output_tokens: int | None = None; price_source: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def has_secret(name: str, settings: Settings | None = None) -> bool:
    """Whether the launcher holds this credential (process environment, then the env file). Tests stub this seam."""
    return _has_secret(name, settings)


def agent_env_names(model: str) -> tuple[str, ...]:
    """The environment variable names Harbor must forward into the agent container for this solver.

    Harbor 0.23's mini-swe-agent adapter takes the provider from the id's first segment, looks the key up in its own
    process environment under the provider table's name (ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY) and
    forwards it under the same name (`ModelConnectionSpec.passthrough`); `harbor_cli.run_task` hands the launcher's
    environment to Harbor, so the launcher must hold it."""
    return (PROVIDERS[parse_model_id(model).provider].key_env,)


def require_provider_key(model: str, settings: Settings | None = None) -> str:
    """The provider key name for `model`, after checking the launcher holds it; `ProviderKeyMissing` otherwise."""
    name = agent_env_names(model)[0]
    if not has_secret(name, settings):
        raise ProviderKeyMissing(f"{name} is not available to the launcher (environment or env file); the solver {model!r} cannot run")
    return name


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


def scaffold_identity(compiled_task_dir: Path, rc: RunConfig) -> str:
    """Agent-scaffold part of the run identity. A compiled interactive mini-swe-agent task carries agent_config.yaml (bound by
    compiled_hash); its digest enters RunConfig.agent_config so scaffold and default-agent trials never share a group. The
    Temporal planner and run_trial MUST agree on this, or the planned trial id differs from the recorded one and every
    interactive trial ends "unresolved" (seaborn-3190 v2, 2026-09-13)."""
    cfg = compiled_task_dir / "agent_config.yaml"
    if rc.agent == "mini-swe-agent" and rc.condition == "interactive" and cfg.exists():
        return "mini-swe-agent.interactive:" + hashlib.sha256(cfg.read_bytes()).hexdigest()[:16]
    return ""


def render_instruction(spec: ScenarioSpec, rc: RunConfig, compiled_task_dir: Path | None = None) -> str:
    """Instruction for the effective brief profile, read from the COMPILED task (bound by compiled_hash), never re-rendered
    from live templates. Falls back to the condition's default instruction.md when the profile file is absent."""
    if compiled_task_dir is None:
        raise ValueError("compiled task dir required: instructions are rendered at compile time")
    natural = next(b for b in spec.briefs if b.profile == "natural")
    selected = next((b for b in spec.briefs if b.profile == rc.brief_profile), natural)
    profile = "natural" if rc.condition == "full" else selected.profile
    for name in (f"instruction.{profile}.md", "instruction.md"):
        p = compiled_task_dir / name
        if p.exists():
            return p.read_text()
    raise FileNotFoundError(f"no compiled instruction in {compiled_task_dir}")


def _solver_cost(ws: Workspace, tid: str, model: str, trial_dir: Path | None, report: dict) -> costs.CostRecord | None:
    """Record the solver's cost from the usage evidence Harbor left; None (unknown) when there is none. A corrupt
    trajectory is noted in the report and never loses the graded result."""
    if trial_dir is None:
        return None
    try:
        usage = costs.usage_from_trial_dir(trial_dir)
    except ValueError as e:  # unreadable agent trajectory: the reward stands, the cost is unknown and the reason is recorded
        report["usage_error"] = str(e)[:300]
        return None
    if usage is None:
        return None
    return costs.record_cost(ws, tid, None, "solver", model, usage)


def run_trial(ws: Workspace, spec: ScenarioSpec, rc: RunConfig, attempt_index: int, stakeholder_url: str, worker_secret: str) -> TrialRecord:
    started_at = _now()
    sdir = ws.scenario_dir(spec.id)
    comp = json.loads((sdir / "compile.json").read_text()); val = json.loads((sdir / "validation.json").read_text())
    if val["status"] != "valid" or val["spec_hash"] != spec.spec_hash or val["compiled_hash"] != comp["compiled_hash"]:
        raise ValueError("scenario/build not validated")
    profile = "natural" if rc.condition == "full" or not any(b.profile == rc.brief_profile for b in spec.briefs) else rc.brief_profile
    compiled = ws.task_dir(spec.id, rc.condition)
    verify_compiled_artifacts(compiled, comp, rc.condition)
    # The agent scaffold is part of the run identity: a compiled interactive task carries agent_config.yaml (bound by
    # compiled_hash) and its identity enters RunConfig.agent_config, so scaffold and default-agent trials never share a group.
    agent_config = scaffold_identity(compiled, rc)
    rc = RunConfig.model_validate({**rc.model_dump(), "compiled_hash": comp["compiled_hash"], "brief_profile": profile, "agent_config": agent_config})
    tid = make_trial_id(spec.spec_hash, rc.condition, rc.run_config_hash, attempt_index)
    tdir = ws.root / "trials" / tid; prior = tdir / "record.json"
    if prior.exists(): return TrialRecord(**json.loads(prior.read_text()))

    token = ""
    tc = tomllib.loads((compiled / "task.toml").read_text())
    expected_host = urlparse(stakeholder_url).hostname if stakeholder_url else None
    if rc.condition == "interactive" and (not expected_host or expected_host not in tc["environment"]["allowed_hosts"]):
        raise ValueError("recompile and validate for actual stakeholder endpoint")
    if int(tc["agent"]["timeout_sec"]) != rc.solver_timeout_sec: raise ValueError("compiled timeout differs from run config")
    provider_host = PROVIDER_HOSTS[parse_model_id(rc.model).provider]   # ModelIdError (a ValueError) for an id outside the grammar
    require_provider_key(rc.model)                                        # fail fast: no session, no files, nothing to reconcile

    if rc.condition == "interactive":
        view = visible_view(spec)
        view["forbidden_terms"] = forbidden_terms(spec, sdir)     # deny-list of tokens from the hidden tests; never content
        token = _post(f"{stakeholder_url}/sessions",
                      {"trial_id": tid, "spec_hash": spec.spec_hash, "condition": rc.condition, "run_config_hash": rc.run_config_hash,
                       "visible_view": view, "stakeholder_config": rc.stakeholder.model_dump(), "image": spec.environment.image},
                      {"X-Worker-Secret": worker_secret})["token"]
    task = materialize_trial_task(ws, compiled, tid, token)
    tc = tomllib.loads((task / "task.toml").read_text())
    tc["metadata"].update(run_config_hash=rc.run_config_hash, compiled_hash=rc.compiled_hash, condition=rc.condition, brief_profile=rc.brief_profile)
    (task / "instruction.md").write_text(render_instruction(spec, rc, compiled))
    for phase in ("environment", "agent"):
        tc[phase]["allowed_hosts"] = sorted(set(tc[phase].get("allowed_hosts", [])) | {provider_host})
    (task / "task.toml").write_text(tomli_w.dumps(tc))
    (tdir / "status.json").write_text(json.dumps({"status": "running", "trial_id": tid, "started_at": started_at, "finished_at": None}))

    status = "completed"; trial_dir: Path | None = None; reward: dict = {}; report: dict = {}; transcript: dict = {}
    try:
        # Pin the pre-baked agent version so Harbor's offline `uv tool install` request matches the baked receipt.
        agent_kwargs = {"version": rc.agent_version} if rc.agent == "mini-swe-agent" and rc.agent_version else None
        # Interactive condition: layer the swesim scaffold config (the packaged mini-swe-agent workflow never lists asking the
        # stakeholder as a step, and 10 of 10 pilot trials under it asked nothing). Harbor reads the host path and ships it.
        agent_config_sha = None
        if rc.agent == "mini-swe-agent" and rc.condition == "interactive" and (task / "agent_config.yaml").exists():
            agent_config_sha = hashlib.sha256((task / "agent_config.yaml").read_bytes()).hexdigest()  # copied at compile time
            agent_kwargs = {**(agent_kwargs or {}), "config_file": str(task / "agent_config.yaml")}
        trial_dir = harbor_cli.run_task(task, rc.agent, ws.jobs_dir / "runs", tid, model=rc.model, agent_kwargs=agent_kwargs,
                                        timeout_sec=rc.solver_timeout_sec + 7200)
        reward = harbor_cli.read_reward(trial_dir); report = harbor_cli.read_report(trial_dir)
        raw = json.loads((trial_dir / "result.json").read_text()); report["harbor_exception"] = raw.get("exception_info")
        if raw.get("exception_info"):
            name = str(raw["exception_info"].get("exception_type", ""))
            code = 3 if "AgentTimeout" in name else 4 if "Agent" in name else 7
            reward.update(reward=0.0, success=0, outcome_code=code); report["outcome"] = OUTCOME_NAMES[code]
        if not reward: reward = {"reward": 0.0, "success": 0, "outcome_code": 7}
    except harbor_cli.HarborRunError as e:
        status = "launch-failed"; report = {"error": str(e), "outcome": "evaluator-failure"}; reward = {"reward": 0.0, "success": 0, "outcome_code": 7}
    except Exception as e:
        # Unknown launch state: leave no record.json (so the trial is reconciled, not silently retried) but make the dir self-describing.
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "status.json").write_text(json.dumps({"status": "error", "trial_id": tid, "started_at": started_at, "finished_at": _now(), "error": type(e).__name__}))
        raise

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
        if transcript.get("service_failure") or transcript.get("pending"):
            # A recorded model failure or a crash-left pending marker (every later request 503) is simulator evidence.
            if transcript.get("service_failure"): report["service_failure_evidence"] = transcript["service_failure"]
            if transcript.get("pending"): report["interrupted_request"] = transcript["pending"]
            report["training_eligible"] = False
        if bound and transcript.get("transcript_hash") and bound != transcript["transcript_hash"]:
            report["transcript_hash_mismatch"] = True
    report["reviewed_equals_submitted"] = None
    report["image_digest"] = comp.get("image_digest")
    report["agent_config_sha256"] = agent_config_sha  # interactive scaffold config, None for other conditions/agents
    report["simulator_validity"] = "unadjudicated" if rc.condition == "interactive" else "n/a"
    cost = _solver_cost(ws, tid, rc.model, trial_dir, report)

    finished_at = _now()
    rec = TrialRecord(tid, spec.id, rc.condition, attempt_index, rc.run_config_hash, str(task), str(trial_dir) if trial_dir else None,
                      reward, report, transcript, status, spec.spec_hash, rc.model, rc.purpose, rc.compiled_hash, rc.brief_profile,
                      started_at, finished_at,
                      cost_usd=None if cost is None or cost.usd is None else float(cost.usd),
                      input_tokens=None if cost is None else cost.input_tokens, output_tokens=None if cost is None else cost.output_tokens,
                      price_source=None if cost is None else cost.price_source)
    for name, obj in (("run_config.json", rc.model_dump(mode="json")), ("spec.json", spec.model_dump(mode="json")), ("reward.json", reward),
                      ("report.json", report), ("transcript.json", transcript), ("record.json", asdict(rec))):
        (tdir / name).write_text(json.dumps(obj, indent=2))
    (tdir / "status.json").write_text(json.dumps({"status": status, "trial_id": tid, "started_at": started_at, "finished_at": finished_at}))
    return rec
