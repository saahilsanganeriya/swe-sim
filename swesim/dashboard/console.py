"""Operator console: form validation, the Temporal client adapter, jobs and the review queue.

The dashboard never talks to Temporal directly; it calls ``swesim.workflows.client`` lazily. The orchestration
stream is adding ``start_create``, ``start_run``, ``list_runs``, ``progress``, ``cancel`` and ``approve_candidate``;
until they exist this module falls back to the existing ``start_pipeline`` / ``start_matrix`` / ``approve`` and,
when nothing usable is there or Temporal is unreachable, raises ``ConsoleUnavailable`` carrying the equivalent CLI
command so the page can show it instead of failing silently. The worker secret and the stakeholder URL are never
form fields: the server reads them from its own environment and never echoes them.
"""
from __future__ import annotations

import contextlib
import inspect
import os
import re
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path

from swesim.dashboard.data import ID_RE, read_json
from swesim.paths import Workspace, atomic_json

GITHUB_REPO_RE = re.compile(r"^(?:https://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
MODEL_RE = re.compile(r"^[a-z0-9-]+/[A-Za-z0-9._:-]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INSTANCE_RE = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+$")
WORKFLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CONDITIONS = ("full", "hidden", "interactive")
PURPOSES = ("calibration", "comparison", "demonstration")
PROFILES = ("withheld", "natural")
APPROVAL_MODES = (("auto", "Auto-approve with morning review (default)"), ("signal", "Hold each task for my approval"))
SOLVER_TIERS = ("anthropic/claude-haiku-4-5-20251001", "anthropic/claude-sonnet-5", "anthropic/claude-opus-5", "openai/gpt-5.5")
STAKEHOLDER_DEFAULT = "openai/gpt-5.5"
MAX_PARALLEL = 8


class ConsoleUnavailable(RuntimeError):
    """Temporal or the client function is not available; ``cli`` is the equivalent command.
    ``kind`` is ``unreachable`` (connection/RPC failure), ``missing`` (function absent in this client version) or
    ``no-query`` (the workflow type exposes no progress query, e.g. RunMatrixWorkflow on main)."""

    def __init__(self, message: str, cli: str, kind: str = "unreachable"):
        super().__init__(message)
        self.cli = cli
        self.kind = kind


def redact(text: str) -> str:
    """Error text shown to the browser: the worker's redaction when available, else mask URLs and secret-like values."""
    text = str(text)
    with contextlib.suppress(Exception):  # optional dependency on the orchestration module
        from swesim.workflows.activities import _redact
        text = _redact(text)
    text = re.sub(r"https?://\S+", "<url>", text)
    text = re.sub(r"(?i)(secret|token|authorization|api[_-]?key)(['\"]?\s*[:=]\s*)\S+", r"\1\2<redacted>", text)
    return text[:300]


def defaults() -> dict:
    try:
        from swesim import cli
        judge, draft, solver = cli.JUDGE_MODEL_DEFAULT, cli.DRAFT_MODEL_DEFAULT, cli.SOLVER_MODEL_DEFAULT
    except Exception:  # noqa: BLE001 - the CLI module is optional for the dashboard
        judge, draft, solver = "openai/gpt-5.5", "anthropic/claude-sonnet-5", "anthropic/claude-haiku-4-5-20251001"
    return {"judge_model": judge, "draft_model": draft, "solver_model": solver, "stakeholder_model": STAKEHOLDER_DEFAULT,
            "since": "2026-06-01", "max_candidates": 3, "limit": 5, "max_parallel": 1, "k": 3}


def stakeholder_url() -> str:
    """Server-side only; loaded from SWESIM_ENV_FILE, never rendered or echoed."""
    from dotenv import load_dotenv
    load_dotenv(os.environ.get("SWESIM_ENV_FILE", str(Path.home() / "Documents" / ".env")), override=False)
    return os.environ.get("SWESIM_STAKEHOLDER_URL", "")


def _lines(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [ln.strip() for ln in str(value or "").replace(",", "\n").splitlines() if ln.strip()]


def _int(value, name: str, lo: int, hi: int, errors: list[str], default: int) -> int:
    if value in (None, ""):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        errors.append(f"{name} must be an integer")
        return default
    if not lo <= n <= hi:
        errors.append(f"{name} must be between {lo} and {hi}")
    return n


def _model(value, name: str, errors: list[str], default: str) -> str:
    m = str(value or default).strip()
    if not MODEL_RE.fullmatch(m):
        errors.append(f"{name} must look like provider/model-id (got {m!r})")
    return m


# ----------------------------------------------------------------------------- validation

def validate_create(body: dict) -> tuple[dict, list[str]]:
    d = defaults()
    errors: list[str] = []
    body = body if isinstance(body, dict) else {}
    source = str(body.get("source") or "github-repo")
    if source not in ("github-repo", "swebench-annotated"):
        errors.append("source must be github-repo or swebench-annotated")
    repos: list[str] = []
    instance_ids: list[str] = []
    if source == "github-repo":
        for raw in _lines(body.get("repos")):
            m = GITHUB_REPO_RE.fullmatch(raw)
            if not m or m.group(1) in (".", "..") or m.group(2) in (".", ".."):
                errors.append(f"not a GitHub repository URL or owner/repo: {raw!r}")
                continue
            repos.append(f"https://github.com/{m.group(1)}/{m.group(2)}")
        if not repos and not any(e.startswith("not a GitHub") for e in errors):
            errors.append("at least one GitHub repository URL is required")
    else:
        for raw in _lines(body.get("instance_ids")):
            if not INSTANCE_RE.fullmatch(raw):
                errors.append(f"not a SWE-bench instance id (owner__repo-NNNN): {raw!r}")
            else:
                instance_ids.append(raw)
    since = str(body.get("since") or d["since"])
    if source == "github-repo" and not DATE_RE.fullmatch(since):
        errors.append("since must be YYYY-MM-DD")
    judge = _model(body.get("judge_model"), "judge model", errors, d["judge_model"])
    draft = _model(body.get("draft_model"), "draft model", errors, d["draft_model"])
    if judge == draft:
        errors.append("judge and draft models must differ (the judge must not grade its own drafts)")
    approval_mode = str(body.get("approval_mode") or "auto")
    if approval_mode not in dict(APPROVAL_MODES):
        errors.append("approval_mode must be auto or signal")
    params = {
        "source": source, "repos": repos, "since": since,
        "max_candidates_per_repo": _int(body.get("max_candidates"), "max candidates per repo", 1, 50, errors, d["max_candidates"]),
        "instance_ids": instance_ids, "limit": _int(body.get("limit"), "limit", 1, 200, errors, d["limit"]),
        "judge_model": judge, "draft_model": draft, "model": d["solver_model"],
        "approval_mode": approval_mode, "max_parallel": _int(body.get("max_parallel"), "max parallel", 1, MAX_PARALLEL, errors, d["max_parallel"]),
        "conditions": ["interactive"], "k": 1,
    }
    return params, errors


def valid_registered(registry_path: Path) -> list[dict]:
    reg = read_json(registry_path).get("scenarios") or {} if registry_path.exists() else {}
    if not isinstance(reg, dict):
        return []
    return [{"id": sid, "version": e.get("version"), "source_ref": e.get("source_ref"), "spec_hash": e.get("spec_hash")}
            for sid, e in sorted(reg.items()) if isinstance(e, dict) and e.get("validation_status") == "valid"]


def validate_run(body: dict, registry_path: Path) -> tuple[dict, list[str]]:
    d = defaults()
    errors: list[str] = []
    body = body if isinstance(body, dict) else {}
    valid_ids = {s["id"] for s in valid_registered(registry_path)}
    scenario_ids = _lines(body.get("scenario_ids"))
    if not scenario_ids:
        errors.append("select at least one registered, valid scenario")
    for sid in scenario_ids:
        if sid not in valid_ids:
            errors.append(f"not a registered valid scenario: {sid!r}")
    models = _lines(body.get("models")) + _lines(body.get("models_extra"))
    models = list(dict.fromkeys(models))
    if not models:
        errors.append("select or enter at least one solver model id")
    for m in models:
        if not MODEL_RE.fullmatch(m):
            errors.append(f"model id must look like provider/model-id (got {m!r})")
    conditions = [c for c in _lines(body.get("conditions"))]
    if not conditions:
        errors.append("select at least one condition")
    for c in conditions:
        if c not in CONDITIONS:
            errors.append(f"unknown condition {c!r}")
    purpose = str(body.get("purpose") or "comparison")
    if purpose not in PURPOSES:
        errors.append("purpose must be calibration, comparison or demonstration")
    profile = str(body.get("brief_profile") or "withheld")
    if profile not in PROFILES:
        errors.append("brief profile must be withheld or natural")
    params = {
        "scenario_ids": scenario_ids, "models": models, "conditions": conditions,
        "k": _int(body.get("k"), "k", 1, 10, errors, d["k"]), "purpose": purpose, "brief_profile": profile,
        "stakeholder_model": _model(body.get("stakeholder_model"), "stakeholder model", errors, d["stakeholder_model"]),
        "max_parallel": _int(body.get("max_parallel"), "max parallel trials", 1, MAX_PARALLEL, errors, d["max_parallel"]),
    }
    return params, errors


# ----------------------------------------------------------------------------- CLI equivalents

def _q(value) -> str:
    """Shell-quote a value for the CLI-equivalent string (values may come straight from a rejected form)."""
    return shlex.quote(str(value))


def cli_for_create(p: dict) -> str:
    base = (f"--judge-model {_q(p['judge_model'])} --draft-model {_q(p['draft_model'])} "
            f"--approval-mode {_q(p['approval_mode'])} --max-candidates-tried {_q(p['max_candidates_per_repo'])}")
    if p["source"] == "github-repo":
        return "\n".join(f"uv run swesim pipeline --source github-repo --repo {_q(r)} {base}" for r in p["repos"]) or \
            f"uv run swesim pipeline --source github-repo --repo <url> {base}"
    ids = "".join(f" --instance {_q(i)}" for i in p["instance_ids"])
    return f"uv run swesim mine --source swebench-annotated{ids}\nuv run swesim pipeline --source swebench-annotated {base}"


def cli_for_run(p: dict) -> str:
    return "\n".join(f"uv run swesim matrix {_q(sid)} --models {_q(','.join(p['models']))} "
                     f"--conditions {_q(','.join(p['conditions']))} --k {_q(p['k'])} --purpose {_q(p['purpose'])} --brief-profile {_q(p['brief_profile'])}"
                     for sid in p["scenario_ids"]) or "uv run swesim matrix <scenario-id> --models <ids>"


# ----------------------------------------------------------------------------- client adapter

def client_fn(name: str):
    try:
        from swesim.workflows import client
    except Exception:  # noqa: BLE001 - temporalio or the module may be missing on a dashboard-only host
        return None
    fn = getattr(client, name, None)
    return fn if callable(fn) else None


async def call(fn, *args):
    result = fn(*args)
    if inspect.isawaitable(result):
        result = await result
    return result


def _unreachable(e: Exception, cli: str) -> ConsoleUnavailable:
    return ConsoleUnavailable(f"Temporal call failed: {type(e).__name__}: {redact(e)}", cli)


async def start_create(params: dict) -> dict:
    cli = cli_for_create(params)
    full = {**params, "stakeholder_url": stakeholder_url()}
    fn = client_fn("start_create")
    try:
        if fn is not None:
            wid = await call(fn, full)
            ids = list(wid) if isinstance(wid, (list, tuple)) else [str(wid)]
            return {"workflow_ids": ids, "via": "start_create"}
        legacy = client_fn("start_pipeline")
        if legacy is None:
            raise ConsoleUnavailable("swesim.workflows.client has neither start_create nor start_pipeline", cli, "missing")
        ids = []
        targets = params["repos"] if params["source"] == "github-repo" else [""]
        for repo in targets:
            legacy_params = {"source": params["source"], "repo": repo, "judge_model": params["judge_model"], "draft_model": params["draft_model"],
                             "models": [params["model"]], "conditions": params["conditions"], "k": params["k"],
                             "approval_mode": params["approval_mode"], "max_scenarios": 1, "since": params["since"], "limit": params["limit"],
                             "max_candidates_tried": params["max_candidates_per_repo"], "stakeholder_url": full["stakeholder_url"]}
            if params["instance_ids"]:
                legacy_params["instance_ids"] = params["instance_ids"]
            ids.append(str(await call(legacy, legacy_params)))
        return {"workflow_ids": ids, "via": "start_pipeline (one workflow per repository)"}
    except ConsoleUnavailable:
        raise
    except Exception as e:
        raise _unreachable(e, cli) from e


def plan_matrix(root: Path, params: dict, sid: str, stakeholder_url_value: str) -> dict:
    """Trial requests for one scenario, mirroring activities.plan_trials but against the dashboard's workspace and with the
    operator's stakeholder model in the frozen RunConfig (the legacy planner reads its own workspace and RunConfig defaults)."""
    from swesim.models import RunConfig, ScenarioSpec, StakeholderConfig, trial_id
    sdir = root / "scenarios" / sid
    spec = ScenarioSpec.model_validate_json((sdir / "scenario.json").read_text())
    comp = read_json(sdir / "compile.json")
    if not comp.get("compiled_hash"):
        raise ConsoleUnavailable(f"{sid} has no compile.json in this workspace; run swesim compile first", cli_for_run(params), "missing")
    trials = []
    for cond in params["conditions"]:
        for model in params["models"]:
            for i in range(params["k"]):
                profile = params["brief_profile"]
                if cond == "full" or not any(b.profile == profile for b in spec.briefs):
                    profile = "natural"
                rc = RunConfig(condition=cond, model=model, compiled_hash=comp["compiled_hash"], purpose=params["purpose"], brief_profile=profile,
                               stakeholder=StakeholderConfig(model_id=params["stakeholder_model"]), k=params["k"])
                tid = trial_id(spec.spec_hash, cond, rc.run_config_hash, i)
                trials.append({"trial_id": tid, "scenario_id": spec.id, "spec_hash": spec.spec_hash, "condition": cond, "model": model,
                               "run_config": rc.model_dump(mode="json"), "run_config_hash": rc.run_config_hash, "attempt_index": i,
                               "stakeholder_url": stakeholder_url_value})
    if len({t["trial_id"] for t in trials}) != len(trials):
        raise ValueError("duplicate matrix entries")
    return {"scenario_id": spec.id, "trials": trials, "max_parallel": params["max_parallel"]}


async def start_run(params: dict, root: Path | None = None) -> dict:
    cli = cli_for_run(params)
    full = {**params, "stakeholder_url": stakeholder_url()}
    fn = client_fn("start_run")
    try:
        if fn is not None:
            wid = await call(fn, full)
            ids = list(wid) if isinstance(wid, (list, tuple)) else [str(wid)]
            return {"workflow_ids": ids, "via": "start_run"}
        legacy = client_fn("start_matrix")
        if legacy is None:
            raise ConsoleUnavailable("swesim.workflows.client has neither start_run nor start_matrix", cli, "missing")
        if root is None:
            raise ConsoleUnavailable("start_matrix fallback needs the workspace root to plan trials", cli, "missing")
        ids = []
        for sid in params["scenario_ids"]:
            plan = plan_matrix(root, params, sid, full["stakeholder_url"])
            ids.append(str(await call(legacy, plan)))
        return {"workflow_ids": ids, "via": "start_matrix (one workflow per scenario; trials planned by the dashboard with the chosen stakeholder model)"}
    except ConsoleUnavailable:
        raise
    except Exception as e:
        raise _unreachable(e, cli) from e


def _job_type(wid: str, raw: dict) -> str:
    t = str(raw.get("type") or raw.get("workflow_type") or "")
    if t:
        return t
    return "pipeline" if wid.startswith(("pipeline-", "create-")) else "matrix" if wid.startswith(("matrix-", "run-")) else "workflow"


def normalize_job(raw: dict) -> dict:
    wid = str(raw.get("workflow_id") or raw.get("id") or "")
    summary = raw.get("summary")
    if isinstance(summary, dict):
        summary = ", ".join(f"{k}={v}" for k, v in summary.items())
    return {"workflow_id": wid, "type": _job_type(wid, raw), "status": str(raw.get("status") or "unknown").lower(),
            "started_at": raw.get("started_at") or raw.get("start_time"), "summary": summary or raw.get("stage") or "",
            "scenario_id": raw.get("scenario_id")}


async def list_jobs() -> list[dict]:
    fn = client_fn("list_runs")
    cli = "uv run swesim pipeline-progress <workflow-id>   # or open the Temporal UI at http://localhost:8233"
    if fn is None:
        raise ConsoleUnavailable("swesim.workflows.client.list_runs is not available in this worker version", cli, "missing")
    try:
        raw = await call(fn)
    except Exception as e:
        raise _unreachable(e, cli) from e
    jobs = [normalize_job(j) for j in (raw or []) if isinstance(j, dict)]
    return [j for j in jobs if j["workflow_id"]]


async def job_progress(wid: str) -> dict:
    fn = client_fn("progress")
    cli = f"uv run swesim pipeline-progress {shlex.quote(wid)}"
    if fn is None:
        raise ConsoleUnavailable("swesim.workflows.client.progress is not available", cli, "missing")
    try:
        p = await call(fn, wid)
    except Exception as e:
        if "Query" in type(e).__name__ or "query" in str(e).lower():
            raise ConsoleUnavailable("this workflow type exposes no progress query (RunMatrixWorkflow reports only on completion); "
                                     "Temporal itself answered", "open the Temporal UI at http://localhost:8233", "no-query") from e
        raise _unreachable(e, cli) from e
    return p if isinstance(p, dict) else {"raw": p}


async def cancel_job(wid: str) -> dict:
    fn = client_fn("cancel")
    cli = f"temporal workflow cancel --workflow-id {shlex.quote(wid)}"
    if fn is None:
        raise ConsoleUnavailable("swesim.workflows.client.cancel is not available in this worker version", cli, "missing")
    try:
        r = await call(fn, wid)
    except Exception as e:
        raise _unreachable(e, cli) from e
    return {"cancelled": wid, "result": r if isinstance(r, (dict, str, int, bool)) or r is None else str(r)}


# ----------------------------------------------------------------------------- review queue

def draft_hash(sdir: Path) -> str | None:
    """Spec hash of the draft (or frozen spec) currently on disk, None when unreadable."""
    for name in ("scenario.json", "scenario.draft.json"):
        p = sdir / name
        if p.exists():
            try:
                from swesim.models import ScenarioSpec
                return ScenarioSpec.model_validate_json(p.read_text()).spec_hash
            except Exception:  # noqa: BLE001 - malformed draft: no hash, the page says so
                return None
    return None


def local_drafts(root: Path) -> list[dict]:
    """Drafts with scenario.draft.json and no approval.json (and no local reject decision)."""
    out = []
    sdirs = root / "scenarios"
    if not sdirs.is_dir():
        return out
    for sdir in sorted(p for p in sdirs.iterdir() if p.is_dir() and ID_RE.fullmatch(p.name)):
        if not (sdir / "scenario.draft.json").exists() or (sdir / "approval.json").exists():
            continue
        review = read_json(sdir / "review.json")
        if review.get("decision") == "reject":
            continue
        meta = read_json(sdir / "draft.meta.json")
        judge = read_json(sdir / "judge.json")
        out.append({"scenario_id": sdir.name, "spec_hash": draft_hash(sdir), "source": "local-draft", "workflow_id": None,
                    "drafted_at": meta.get("created_at"), "judge_keep": judge.get("keep"), "rubric": judge.get("verified_rubric_score")})
    return out


def _awaiting_from_progress(wid: str, p: dict) -> list[dict]:
    items = []
    cands = p.get("candidates") if isinstance(p.get("candidates"), list) else None
    if cands is not None:
        for c in cands:
            if isinstance(c, dict) and c.get("stage") == "awaiting-approval" and c.get("scenario_id"):
                items.append({"scenario_id": c["scenario_id"], "spec_hash": c.get("spec_hash"), "source": "creation-job", "workflow_id": wid,
                              "instance_id": c.get("instance_id"), "judge_keep": (c.get("judge") or {}).get("keep") if isinstance(c.get("judge"), dict) else None,
                              "rubric": (c.get("judge") or {}).get("verified_rubric_score") if isinstance(c.get("judge"), dict) else None})
    elif p.get("stage") == "awaiting-approval" and p.get("scenario_id"):
        items.append({"scenario_id": p["scenario_id"], "spec_hash": p.get("spec_hash"), "source": "creation-job", "workflow_id": wid,
                      "instance_id": p.get("instance_id"), "judge_keep": None, "rubric": None})
    return items


async def review_queue(root: Path) -> dict:
    """Candidates awaiting approval from running creation jobs plus local drafts without approval.json."""
    items: list[dict] = []
    errors: list[str] = []
    try:
        jobs = await list_jobs()
    except ConsoleUnavailable as e:
        jobs = []
        errors.append(f"{e} (creation jobs not listed; local drafts only)")
    for job in jobs:
        if job["type"] not in ("pipeline", "create", "PipelineWorkflow", "CreateWorkflow") or job["status"] not in ("running", "open", "unknown"):
            continue
        try:
            items.extend(_awaiting_from_progress(job["workflow_id"], await job_progress(job["workflow_id"])))
        except ConsoleUnavailable as e:
            errors.append(str(e))
    seen = {i["scenario_id"] for i in items}
    items.extend(d for d in local_drafts(root) if d["scenario_id"] not in seen)
    for it in items:
        sdir = root / "scenarios" / it["scenario_id"]
        it["on_disk"] = sdir.is_dir()
        it["disk_hash"] = draft_hash(sdir) if sdir.is_dir() else None
    return {"items": items, "errors": errors}


def local_approve(ws: Workspace, scenario_id: str) -> dict:
    from swesim.stages.approve import approve_scenario
    return approve_scenario(ws, scenario_id, auto=False)


async def decide(root: Path, registry_path: Path, sid: str, body: dict) -> tuple[int, dict]:
    """Approve or reject a draft. Refuses when the spec hash on disk differs from the one the page rendered."""
    body = body if isinstance(body, dict) else {}
    decision = str(body.get("decision") or "")
    note = str(body.get("note") or "")[:2000]
    if decision not in ("approve", "reject"):
        return 400, {"ok": False, "error": "decision must be approve or reject"}
    sdir = root / "scenarios" / sid
    if not sdir.is_dir() or not ((sdir / "scenario.draft.json").exists() or (sdir / "scenario.json").exists()):
        return 404, {"ok": False, "error": "no draft on disk for this scenario"}
    current = draft_hash(sdir)
    rendered = str(body.get("spec_hash") or "")
    if not current:
        return 409, {"ok": False, "error": "the draft on disk does not validate; nothing to approve"}
    if rendered != current:
        return 409, {"ok": False, "error": "the draft changed since the page was rendered; reload and review again",
                     "current_spec_hash": current, "rendered_spec_hash": rendered}
    if (sdir / "approval.json").exists():
        return 409, {"ok": False, "error": "already approved; the review page is read-only", "approval": read_json(sdir / "approval.json")}
    wid = str(body.get("workflow_id") or "")
    record = {"scenario_id": sid, "spec_hash": current, "decision": decision, "note": note, "workflow_id": wid or None,
              "decided_at": datetime.now(UTC).isoformat(timespec="seconds"), "decided_by": "dashboard"}
    if wid:
        if not WORKFLOW_ID_RE.fullmatch(wid):
            return 400, {"ok": False, "error": "invalid workflow id"}
        fn = client_fn("approve_candidate")
        cli = f"uv run swesim approve-signal {shlex.quote(wid)} {shlex.quote(sid)} {current}"
        try:
            if fn is not None:
                await call(fn, wid, sid, current, decision, note)
            elif decision == "approve" and client_fn("approve") is not None:
                await call(client_fn("approve"), wid, sid, current)
            else:
                return 502, {"ok": False, "error": "this worker's client cannot signal a rejection; cancel the job or approve via CLI", "cli": cli}
        except Exception as e:  # noqa: BLE001
            return 502, {"ok": False, "error": f"Temporal call failed: {type(e).__name__}: {redact(e)}", "cli": cli}
        atomic_json(sdir / "review.json", record)
        return 200, {"ok": True, "decision": decision, "scenario_id": sid, "spec_hash": current, "workflow_id": wid, "via": "signal"}
    if decision == "reject":
        atomic_json(sdir / "review.json", record)
        return 200, {"ok": True, "decision": "reject", "scenario_id": sid, "spec_hash": current, "via": "local review.json"}
    try:
        result = local_approve(Workspace(root, registry_path), sid)
    except Exception as e:  # noqa: BLE001
        return 500, {"ok": False, "error": f"approve_scenario failed: {type(e).__name__}: {redact(e)}"}
    if not result.get("approved"):
        return 400, {"ok": False, "error": "the code-owned gates rejected this draft", "reasons": result.get("reasons") or []}
    atomic_json(sdir / "review.json", record)
    return 200, {"ok": True, "decision": "approve", "scenario_id": sid, "spec_hash": result.get("spec_hash") or current,
                 "via": "approve_scenario(auto=False)", "approval": read_json(sdir / "approval.json")}


def now_ts() -> float:
    return time.time()
