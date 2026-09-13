"""Operator console: form validation, the Temporal client adapter, jobs and the review queue.

The dashboard never talks to Temporal directly; it calls ``swesim.workflows.client`` lazily and prefers the
two-workflow functions (``start_create`` -> CreateTasksWorkflow, ``start_run`` -> RunTasksWorkflow, ``list_runs``,
``progress``, ``cancel``, ``approve_candidate``). Only when one of them is missing does it fall back to the legacy
``start_pipeline`` (one PipelineWorkflow per repository) / ``start_matrix`` (one serial RunMatrixWorkflow per
scenario, planned by the worker's own ``plan_trials``) / ``approve``. When nothing usable is there or Temporal is
unreachable it raises ``ConsoleUnavailable`` carrying the equivalent CLI command so the page shows it instead of
failing silently. The worker secret and the stakeholder URL are never form fields: the server reads them from its own
environment and never echoes them.
"""
from __future__ import annotations

import json
import contextlib
import inspect
import re
import shlex
import time
from pathlib import Path

from swesim.dashboard.data import ID_RE, read_json
from swesim.paths import Workspace
from swesim.settings import Settings, get_settings

GITHUB_REPO_RE = re.compile(r"^(?:https://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
MODEL_RE = re.compile(r"^[a-z0-9-]+/[A-Za-z0-9._:-]+(?:/[A-Za-z0-9._:-]+)?$")  # <provider>/<vendor-or-model>[/<model>] (interfaces §6)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INSTANCE_RE = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+$")
WORKFLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")  # `@` for rebuilt candidates: create:<sid>@v<n>
CONDITIONS = ("full", "hidden", "interactive")
PURPOSES = ("calibration", "comparison", "demonstration", "screen")  # screen = the difficulty screen run (brief §5)
PROFILES = ("withheld", "natural")
# Form values are the CreateTasksWorkflow's modes; the legacy PipelineWorkflow spells "hold" as "signal".
APPROVAL_MODES = (("auto", "Auto-approve with morning review (default)"), ("hold", "Hold each task for my approval"))
LEGACY_APPROVAL = {"auto": "auto", "hold": "signal"}
SOLVER_TIERS = ("anthropic/claude-haiku-4-5-20251001", "anthropic/claude-sonnet-5", "anthropic/claude-opus-5", "openai/gpt-5.5")  # legacy picker; the run page uses render.TIERS


def _stakeholder_default() -> str:
    """The frozen default of ``StakeholderConfig.model_id`` (interfaces §7: anthropic/claude-opus-5 once the integrator lands it)."""
    try:
        from swesim.models import StakeholderConfig
        return str(StakeholderConfig().model_id)
    except Exception:  # noqa: BLE001 - models.py is the integrator's; a construction error must not break the console
        return "anthropic/claude-opus-5"


STAKEHOLDER_DEFAULT = _stakeholder_default()
MAX_PARALLEL = 8
CREATION_TYPES = ("CreateTasksWorkflow", "CandidateWorkflow", "PipelineWorkflow", "create", "candidate", "pipeline")
OPEN_STATUSES = ("running", "open", "unknown", "continued_as_new")


class ConsoleUnavailable(RuntimeError):
    """Temporal or the client function is not available; ``cli`` is the equivalent command.
    ``kind`` is ``unreachable`` (connection/RPC failure), ``missing`` (function absent in this client version) or
    ``no-query`` (the workflow type exposes no progress query, e.g. RunMatrixWorkflow)."""

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


def stakeholder_url(settings: Settings | None = None) -> str:
    """Server-side only (``Settings.stakeholder_url``); passed to the workflow, never rendered or echoed."""
    return (settings or get_settings()).stakeholder_url or ""


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

PR_KINDS = ("bugfix", "feature")  # github_repo.discover bands; bugfix is the one that produced a valid scenario


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
    pr_kind = str(body.get("pr_kind") or "bugfix")
    if pr_kind not in PR_KINDS:
        errors.append("pr_kind must be bugfix or feature")
    approval_mode = str(body.get("approval_mode") or "auto")
    if approval_mode not in dict(APPROVAL_MODES):
        errors.append("approval_mode must be auto or hold")
    params = {
        "source": source, "repos": repos, "since": since, "pr_kind": pr_kind,
        "max_candidates_per_repo": _int(body.get("max_candidates"), "max candidates per repo", 1, 50, errors, d["max_candidates"]),
        "instance_ids": instance_ids, "limit": _int(body.get("limit"), "limit", 1, 200, errors, d["limit"]),
        "judge_model": judge, "draft_model": draft, "model": d["solver_model"],
        "approval_mode": approval_mode, "max_parallel": _int(body.get("max_parallel"), "max parallel", 1, MAX_PARALLEL, errors, d["max_parallel"]),
        "conditions": ["interactive"], "k": 1,
        "rebuild": _flag(body.get("rebuild")),
    }
    if body.get("pr") is not None:
        params["pr"] = _int(body["pr"], "PR number", 1, 2**31 - 1, errors, 1)
        if source != "github-repo" or len(repos) != 1:
            errors.append("an exact PR requires one repository")
    return params, errors


def _flag(value) -> bool:
    """Checkbox semantics: the console JS posts checked boxes as a list of values and unchecked ones as an empty list."""
    if isinstance(value, (list, tuple)):
        return any(_flag(v) for v in value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)


def create_sources(p: dict) -> list[dict]:
    """The ``sources`` list CreateTasksWorkflow expects (same shape as ``swesim create --source``)."""
    if p["source"] == "github-repo":
        return [{"kind": "github-repo", "repo": r, "since": p["since"], "max_candidates": p["max_candidates_per_repo"], "pr_kind": p.get("pr_kind", "bugfix"), **({"pr": p["pr"]} if p.get("pr") else {})} for r in p["repos"]]
    src = {"kind": "swebench-annotated", "pool": "A", "limit": p["limit"]}
    if p["instance_ids"]:
        src["instance_ids"] = list(p["instance_ids"])
    return [src]


def valid_registered(registry_path: Path) -> list[dict]:
    reg = read_json(registry_path).get("scenarios") or {} if registry_path.exists() else {}
    if not isinstance(reg, dict):
        return []
    return [{"id": sid, "version": e.get("version"), "source_ref": e.get("source_ref"), "spec_hash": e.get("spec_hash")}
            for sid, e in sorted(reg.items()) if isinstance(e, dict) and e.get("validation_status") == "valid"]


def validate_run(body: dict, valid_ids: set[str] | Path) -> tuple[dict, list[str]]:
    """``valid_ids``: the registered valid scenario ids (from the reader); a registry path is accepted for the CLI fallback."""
    d = defaults()
    errors: list[str] = []
    body = body if isinstance(body, dict) else {}
    if isinstance(valid_ids, Path):
        valid_ids = {s["id"] for s in valid_registered(valid_ids)}
    scenario_ids = _lines(body.get("scenario_ids"))
    if not scenario_ids:
        errors.append("select at least one registered, valid scenario")
    for sid in scenario_ids:
        if sid not in valid_ids:
            errors.append(f"not a registered valid scenario: {sid!r}")
    models = _lines(body.get("models")) + _lines(body.get("models_extra"))
    models = list(dict.fromkeys(models))
    purpose = str(body.get("purpose") or "comparison")
    if purpose == "screen" and not models:
        from swesim.roles import ROLE_CHAINS
        models = [ROLE_CHAINS["screen_model"][0]]
    if not models:
        errors.append("select or enter at least one solver model id")
    for m in models:
        if not MODEL_RE.fullmatch(m):
            errors.append(f"model id must look like provider/model-id (got {m!r})")
    conditions = list(_lines(body.get("conditions")))
    if purpose == "screen":
        conditions = ["hidden", "full"]
    if not conditions:
        errors.append("select at least one condition")
    for c in conditions:
        if c not in CONDITIONS:
            errors.append(f"unknown condition {c!r}")
    if purpose not in PURPOSES:
        errors.append("purpose must be calibration, comparison, demonstration or screen")
    profile = str(body.get("brief_profile") or "withheld")
    if profile not in PROFILES:
        errors.append("brief profile must be withheld or natural")
    params = {
        "scenario_ids": scenario_ids, "models": models, "conditions": conditions,
        "k": 2 if purpose == "screen" else _int(body.get("k"), "k", 1, 10, errors, d["k"]), "purpose": purpose, "brief_profile": profile,
        "stakeholder_model": _model(body.get("stakeholder_model"), "stakeholder model", errors, d["stakeholder_model"]),
        "max_parallel": _int(body.get("max_parallel"), "max parallel trials", 1, MAX_PARALLEL, errors, d["max_parallel"]),
        "attempt_offset": _int(body.get("attempt_offset"), "attempt offset", 0, 1000, errors, 0),
    }
    return params, errors


# ----------------------------------------------------------------------------- CLI equivalents

def _q(value) -> str:
    """Shell-quote a value for the CLI-equivalent string (values may come straight from a rejected form)."""
    return shlex.quote(str(value))


def cli_for_create(p: dict) -> str:
    base = (f"--judge-model {_q(p['judge_model'])} --draft-model {_q(p['draft_model'])} --approval-mode {_q(p['approval_mode'])} "
            f"--max-parallel {_q(p['max_parallel'])}" + (f" --pr {p['pr']}" if p.get("pr") else "") + (" --rebuild" if p.get("rebuild") else ""))
    if p["source"] == "github-repo":
        srcs = "".join(f" --source {_q(r)}" for r in p["repos"]) or " --source <repo-url>"
        return f"uv run swesim create{srcs} --since {_q(p['since'])} --max-candidates {_q(p['max_candidates_per_repo'])} --pr-kind {_q(p.get('pr_kind', 'bugfix'))} {base}"
    pool = "swebench:A" + (":" + ",".join(p["instance_ids"]) if p["instance_ids"] else "")
    return f"uv run swesim create --source {_q(pool)} --max-candidates {_q(p['limit'])} {base}"


def cli_for_run(p: dict) -> str:
    return (f"uv run swesim run-tasks --scenario-ids {_q(','.join(p['scenario_ids']) or '<ids>')} --models {_q(','.join(p['models']) or '<ids>')} "
            f"--conditions {_q(','.join(p['conditions']) or 'interactive')} --k {_q(p['k'])} --purpose {_q(p['purpose'])} "
            f"--brief-profile {_q(p['brief_profile'])} --stakeholder-model {_q(p['stakeholder_model'])} --max-parallel-trials {_q(p['max_parallel'])}")


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


def _ids(result) -> list[str]:
    return [str(x) for x in result] if isinstance(result, (list, tuple)) else [str(result)]


async def start_create(params: dict, settings: Settings | None = None) -> dict:
    """Prefer CreateTasksWorkflow (one workflow for every source); fall back to one PipelineWorkflow per repository."""
    cli = cli_for_create(params)
    url = stakeholder_url(settings)
    fn = client_fn("start_create")
    try:
        if fn is not None:
            wf_params = {"sources": create_sources(params), "judge_model": params["judge_model"], "draft_model": params["draft_model"],
                         "max_parallel": params["max_parallel"], "approval_mode": params["approval_mode"], "stakeholder_url": url}
            if params.get("rebuild"):
                wf_params["rebuild"] = True  # existing scenarios are re-created as version n+1 (create:<sid>@v<n+1>)
            return {"workflow_ids": _ids(await call(fn, wf_params)), "via": "start_create (CreateTasksWorkflow)"}
        legacy = client_fn("start_pipeline")
        if legacy is None:
            raise ConsoleUnavailable("swesim.workflows.client has neither start_create nor start_pipeline", cli, "missing")
        ids = []
        targets = params["repos"] if params["source"] == "github-repo" else [""]
        for repo in targets:
            legacy_params = {"source": params["source"], "repo": repo, "judge_model": params["judge_model"], "draft_model": params["draft_model"],
                             "models": [params["model"]], "conditions": params["conditions"], "k": params["k"],
                             "approval_mode": LEGACY_APPROVAL[params["approval_mode"]], "max_scenarios": 1, "since": params["since"],
                             "limit": params["limit"], "max_candidates_tried": params["max_candidates_per_repo"], "stakeholder_url": url}
            if params["instance_ids"]:
                legacy_params["instance_ids"] = params["instance_ids"]
            ids.append(str(await call(legacy, legacy_params)))
        return {"workflow_ids": ids, "via": "start_pipeline (legacy: one PipelineWorkflow per repository; max_parallel is ignored)"}
    except ConsoleUnavailable:
        raise
    except Exception as e:
        raise _unreachable(e, cli) from e


@contextlib.contextmanager
def planner_workspace(root: Path | None):
    """Point the worker's planner at the dashboard's workspace for the duration of the call when the two differ
    (they are the same directory in production: both read SWESIM_WORKSPACE)."""
    from swesim.workflows import activities
    previous = activities.WS
    if root is not None and Path(previous.root).resolve() != Path(root).resolve():
        activities.WS = Workspace(root, previous.registry_path)
    try:
        yield activities
    finally:
        activities.WS = previous


async def start_run(params: dict, settings: Settings | None = None, root: Path | None = None) -> dict:
    """Prefer RunTasksWorkflow (one workflow, bounded parallelism); fall back to one serial RunMatrixWorkflow per scenario,
    planned by the worker's own ``plan_trials`` so trial ids and score groups agree with the CLI."""
    cli = cli_for_run(params)
    url = stakeholder_url(settings)
    fn = client_fn("start_run")
    try:
        if fn is not None:
            wf_params = {"scenario_ids": params["scenario_ids"], "models": params["models"], "conditions": params["conditions"], "k": params["k"],
                         "purpose": params["purpose"], "brief_profile": params["brief_profile"], "stakeholder_model": params["stakeholder_model"],
                         "attempt_offset": params["attempt_offset"], "max_parallel_trials": params["max_parallel"], "stakeholder_url": url}
            return {"workflow_ids": _ids(await call(fn, wf_params)), "via": "start_run (RunTasksWorkflow; max_parallel_trials honored)"}
        legacy = client_fn("start_matrix")
        if legacy is None:
            raise ConsoleUnavailable("swesim.workflows.client has neither start_run nor start_matrix", cli, "missing")
        ids = []
        with planner_workspace(root) as activities:
            for sid in params["scenario_ids"]:
                plan = activities.plan_trials({"scenario_id": sid, "models": params["models"], "conditions": params["conditions"], "k": params["k"],
                                               "purpose": params["purpose"], "brief_profile": params["brief_profile"],
                                               "stakeholder_model": params["stakeholder_model"], "attempt_offset": params["attempt_offset"],
                                               "stakeholder_url": url})
                ids.append(str(await call(legacy, plan)))
        return {"workflow_ids": ids, "via": "start_matrix (legacy: one RunMatrixWorkflow per scenario, serial on this worker - max_parallel is "
                                            "ignored and honored only by RunTasksWorkflow; trials planned by the worker's plan_trials)"}
    except ConsoleUnavailable:
        raise
    except Exception as e:
        raise _unreachable(e, cli) from e


def _job_type(wid: str, raw: dict) -> str:
    t = str(raw.get("type") or raw.get("workflow_type") or "")
    if t:
        return t
    return "PipelineWorkflow" if wid.startswith("pipeline-") else "RunMatrixWorkflow" if wid.startswith("matrix-") else \
        "CreateTasksWorkflow" if wid.startswith("create-") else "RunTasksWorkflow" if wid.startswith("run-") else "workflow"


def normalize_job(raw: dict) -> dict:
    wid = str(raw.get("workflow_id") or raw.get("id") or "")
    summary = raw.get("summary")
    if isinstance(summary, dict):
        summary = ", ".join(f"{k}={','.join(map(str, v)) if isinstance(v, (list, tuple)) else v}" for k, v in summary.items())
    return {"workflow_id": wid, "run_id": raw.get("run_id"), "type": _job_type(wid, raw), "status": str(raw.get("status") or "unknown").lower(),
            "started_at": raw.get("started_at") or raw.get("start_time"), "closed_at": raw.get("close_time"),
            "summary": summary or raw.get("stage") or "", "params_summary": raw.get("summary") if isinstance(raw.get("summary"), dict) else {},
            "scenario_id": raw.get("scenario_id"), "failure": raw.get("failure")}


def validate_retry(body: dict, candidate: dict | None) -> tuple[dict, list[str]]:
    """Retry a failed/rejected candidate: a creation job over that candidate's source only (SWE-bench: the instance id;
    GitHub: its repository with max 1 candidate and the same PR band). Discovery re-runs; nothing is hand-edited."""
    errors: list[str] = []
    if not candidate:
        return {}, ["candidate not found in the index"]
    if candidate.get("source") == "swebench-annotated":
        params, errs = validate_create({"source": "swebench-annotated", "instance_ids": [candidate["instance_id"]], "limit": 1,
                                        "approval_mode": (body or {}).get("approval_mode") or "auto"})
    else:
        repo = candidate.get("repo") or ""
        params, errs = validate_create({"source": "github-repo", "repos": [repo], "max_candidates": 1, "pr_kind": candidate.get("pr_kind") or "bugfix",
                                        "since": (body or {}).get("since") or defaults()["since"], "approval_mode": (body or {}).get("approval_mode") or "auto",
                                        "rebuild": bool(candidate.get("scenario_id")), "pr": candidate.get("pr_number")})
    if candidate.get("source") != "swebench-annotated" and not candidate.get("pr_number"):
        errors.append("candidate has no recorded PR number; retry cannot identify its source")
    errors.extend(errs)
    params["retry_of"] = {"instance_id": candidate["instance_id"], "source_key": candidate["source_key"]}
    return params, errors


async def list_jobs() -> list[dict]:
    fn = client_fn("list_runs")
    cli = "uv run swesim workflows list   # or open the Temporal UI at http://localhost:8233"
    if fn is None:
        raise ConsoleUnavailable("swesim.workflows.client.list_runs is not available in this worker version", cli, "missing")
    try:
        raw = await call(fn)
    except Exception as e:
        raise _unreachable(e, cli) from e
    jobs = [normalize_job(j) for j in (raw or []) if isinstance(j, dict)]
    return [j for j in jobs if j["workflow_id"]]


def _is_query_failure(e: Exception) -> bool:
    try:
        from temporalio.client import WorkflowQueryFailedError, WorkflowQueryRejectedError
        return isinstance(e, (WorkflowQueryFailedError, WorkflowQueryRejectedError))
    except Exception:  # noqa: BLE001 - temporalio absent: fall back to the class name only
        return type(e).__name__ in ("WorkflowQueryFailedError", "WorkflowQueryRejectedError")


async def job_progress(wid: str) -> dict:
    fn = client_fn("progress")
    cli = f"uv run swesim workflows progress {shlex.quote(wid)}"
    if fn is None:
        raise ConsoleUnavailable("swesim.workflows.client.progress is not available", cli, "missing")
    try:
        p = await call(fn, wid)
    except Exception as e:
        if _is_query_failure(e):
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


def _candidate_items(p: dict):
    """Candidates from a progress dict: CreateTasksWorkflow keeps a dict keyed by scenario id, older shapes a list."""
    cands = p.get("candidates")
    if isinstance(cands, dict):
        return [(sid, c) for sid, c in cands.items() if isinstance(c, dict)]
    if isinstance(cands, list):
        return [(c.get("scenario_id"), c) for c in cands if isinstance(c, dict)]
    return []


def _awaiting_from_progress(wid: str, p: dict) -> list[dict]:
    """Held candidates from one creation job's progress: CreateTasksWorkflow (``awaiting_approval`` list / ``candidates``
    dict with ``status``), CandidateWorkflow (its own ``stage``) and the legacy PipelineWorkflow (``stage``)."""
    items: list[dict] = []
    seen: set[str] = set()

    def add(sid, spec_hash, target_wid, instance_id=None, judge=None):
        if not sid or sid in seen:
            return
        seen.add(sid)
        items.append({"scenario_id": sid, "spec_hash": spec_hash, "source": "creation-job", "workflow_id": target_wid or wid,
                      "parent_workflow_id": wid, "instance_id": instance_id,
                      "judge_keep": (judge or {}).get("keep") if isinstance(judge, dict) else None,
                      "rubric": (judge or {}).get("verified_rubric_score") if isinstance(judge, dict) else None})

    for a in p.get("awaiting_approval") or []:
        if isinstance(a, dict):
            add(a.get("scenario_id"), a.get("spec_hash"), a.get("candidate_workflow_id"))
    for sid, c in _candidate_items(p):
        if c.get("status") == "awaiting-approval" or c.get("stage") == "awaiting-approval":
            add(sid or c.get("scenario_id"), c.get("spec_hash"), c.get("candidate_workflow_id"), c.get("instance_id"), c.get("judge"))
    if p.get("stage") == "awaiting-approval" and p.get("scenario_id"):
        add(p["scenario_id"], p.get("spec_hash"), None, p.get("instance_id"))
    return items


async def review_queue(root: Path, reader=None) -> dict:
    """Candidates awaiting approval from running creation jobs plus local drafts without approval.json."""
    if reader is not None:
        from swesim.dashboard.access import all_rows
        from swesim.dashboard.rows import ScenarioFilter, WorkflowFilter
        items = []
        for w in all_rows(reader.workflows, WorkflowFilter(type=list(CREATION_TYPES), status=["running"])):
            items.extend(_awaiting_from_progress(w.workflow_id, w.params_summary))
        seen = {i["scenario_id"] for i in items}
        for s in all_rows(reader.scenarios, ScenarioFilter(validation_status=["drafting"], latest_only=True)):
            if s.scenario_id not in seen:
                items.append({"scenario_id": s.scenario_id, "spec_hash": s.spec_hash, "source": s.source,
                              "workflow_id": s.workflow_id, "drafted_at": s.created_at.isoformat(), "judge_keep": None, "rubric": None})
        return {"items": items, "errors": []}
    items: list[dict] = []
    errors: list[str] = []
    try:
        jobs = await list_jobs()
    except ConsoleUnavailable as e:
        jobs = []
        errors.append(f"{e} (creation jobs not listed; local drafts only)")
    for job in jobs:
        if job["type"] not in CREATION_TYPES or job["status"] not in OPEN_STATUSES:
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
    if wid and not WORKFLOW_ID_RE.fullmatch(wid):
        return 400, {"ok": False, "error": "invalid workflow id"}
    result = await submit_record_action("review", {"scenario_id": sid, "spec_hash": current, "decision": decision,
                                                    "note": note, "workflow_id": wid},
                                        body.get("actor") or "anonymous", body["request_id"])
    return 200, result


async def submit_record_action(action: str, params: dict, actor: str, request_id: str) -> dict:
    """All record mutations run on a writable control worker, including hosted hold decisions."""
    fn = client_fn("operator_action")
    cli = "uv run swesim workflows action " + action + " --params-json " + shlex.quote(json.dumps(params)) + " --request-id " + shlex.quote(request_id)
    if fn is None:
        raise ConsoleUnavailable("operator action worker is unavailable", cli, "missing")
    try:
        return await call(fn, {"action": action, "params": params, "actor": actor}, request_id)
    except Exception as error:
        raise _unreachable(error, cli) from error


def now_ts() -> float:
    return time.time()
