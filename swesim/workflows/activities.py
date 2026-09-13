"""Temporal activities. Payloads carry ids, paths, hashes and model ids; never secrets.

Stage modules owned by other streams are imported lazily inside activity bodies so this module imports without them
(tests inject fakes into sys.modules).

Every activity is declared with `@swesim_activity`, which adds two rules to `@activity.defn`: a `ValueError` (invalid
input, business rejection, schema failure) becomes a non-retryable ApplicationError typed by the exception class, so
the retry classes in `swesim.workflows.retries` retry only transient failures; and every result dict carries
`worker_image_digest` and `origin` from `Settings`, so a result names the code that produced it.

Configuration enters through `SETTINGS` (one `Settings` built at import, rebound by `configure(settings)` in the
worker); `WS` is the workspace and `LAUNCHER` the trial launcher derived from it. No `os.environ` reads for
configuration; the environment is only scanned to redact secret-looking values from error text.

Trial lifecycle (exactly-once in effect, not merely by workflow id) lives in `swesim.workflows.launchers`:
  launch_once   -> read evidence (trial dir + runtime state), decide, and only on "launch" write request.json, a launch
                   intent, start the runtime (systemd unit | Cloud Run Job | subprocess) and record the acknowledgement.
                   Any prior launch evidence means: never relaunch.
  await_trial   -> heartbeating poll of record.json / runner-error.json / runtime state; returns a terminal status dict
                   (completed, runner-error, launch-failed, unit-failed, unresolved). Retryable and idempotent: it only reads.
Run as a program (`python -m swesim.workflows.activities run-trial <request.json>`) this module is the per-trial
process inside the unit or Job; it survives Temporal worker restarts because the runtime, not the worker, owns it.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import importlib
import inspect
import json
import logging
import os
import sys
import time
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from swesim import roles
from swesim.models import ID_RE, RunConfig, ScenarioSpec, trial_id
from swesim.paths import Workspace, atomic_json
from swesim.settings import Settings, get_settings, has_secret, secret
from swesim.workflows import archive, batching, retries, validation
from swesim.workflows.launchers.base import (  # noqa: F401 - re-exported
    Launcher,
    LaunchFailed,
    TrialBindingConflict,
    TrialEvidence,
    TrialLayout,
    WorkspaceLayout,
    decide_launch,
    get_launcher,
    unit_name,
)
from swesim.workflows.launchers.base import launch_once as _launch_once
from swesim.workflows.launchers.base import read_evidence as _read_evidence
from swesim.workflows.launchers.systemd import (  # noqa: F401 - re-exported
    UnitStateUnavailable,
    secret_values,
)
from swesim.workflows.payloads import check_payload

log = logging.getLogger("swesim.workflows.trials")  # launch decisions; the worker configures stdout logging at INFO
SETTINGS: Settings = get_settings()
WS = Workspace(SETTINGS.workspace, SETTINGS.registry_path)
LAUNCHER: Launcher | None = None  # built from SETTINGS on first use (`launcher()`); tests assign one directly
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = REPO_ROOT / "data" / "annotations" / "ensembled_annotations_public.csv"
HEARTBEAT_SECONDS = 30.0
POLL_SECONDS = 10.0
AWAIT_DEADLINE_SEC = 3 * 3600
LAUNCH_GRACE_POLLS = 6  # polls without any launch evidence before await reports launch-failed
# Per-role fallback chains (interfaces §7): the configured id is probed first, then the chain in order; judge and draft must
# resolve to different ids (draft_spec.draft refuses correlated errors); the solver is never substituted.
FALLBACK_CHAINS = roles.ROLE_CHAINS
LAUNCH_SLOTS = asyncio.Semaphore(1)  # pilot trials launch serially per worker


def configure(settings: Settings) -> None:
    """Rebind SETTINGS and WS to one `Settings` (worker start, CLI overrides) and drop the launcher so it is rebuilt lazily."""
    global SETTINGS, WS, LAUNCHER
    SETTINGS = settings
    WS = Workspace(settings.workspace, settings.registry_path)
    LAUNCHER = None


def stamp(result):
    """`worker_image_digest` and `origin` on every dict result (never overriding a value the record supplied)."""
    if isinstance(result, dict):
        result.setdefault("worker_image_digest", SETTINGS.worker_image_digest)
        result.setdefault("origin", SETTINGS.origin)
    return result


def swesim_activity(fn):
    """`@activity.defn` plus the module's two rules: ValueError -> non-retryable ApplicationError; results are stamped."""
    @functools.wraps(fn)
    async def wrapper(p: dict) -> dict:
        try:
            check_payload(p, f"{fn.__name__} input")
            result = await fn(p)
            result = stamp(result)
            check_payload(result, f"{fn.__name__} result")
        except ValueError as e:
            raise retries.non_retryable(f"{type(e).__name__}: {str(e)[:1500]}", kind=type(e).__name__) from e
        return result
    return activity.defn(name=fn.__name__)(wrapper)


# ------------------------------------------------------------------ helpers

def _heartbeat(*details) -> None:
    if activity.in_activity():
        activity.heartbeat(*details)


def _cancelled() -> bool:
    return activity.in_activity() and activity.is_cancelled()


async def blocking(fn, every: float | None = None):
    """Run a blocking stage function in a thread while heartbeating every `every` seconds. Between heartbeats the activity's
    cancellation is observed: the activity ends as cancelled (the thread cannot be interrupted and runs to completion; a launcher's
    runtime is stopped by `cancel_trial_activity`, not here)."""
    if _cancelled():
        raise asyncio.CancelledError("activity cancelled before the next stage")
    task = asyncio.ensure_future(asyncio.to_thread(fn))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=every or HEARTBEAT_SECONDS)
            if _cancelled():
                raise asyncio.CancelledError("activity cancelled between stages")
            if done:
                return task.result()
            _heartbeat()
    except asyncio.CancelledError:
        # A Python thread cannot be killed. Drain this stage before acknowledging cancellation so that
        # launch_trial cannot create a Job after the workflow's cancellation cleanup has already run.
        try:
            await asyncio.shield(task)
        except Exception as error:  # noqa: BLE001 - cancellation still wins after the thread drains
            log.debug("cancelled stage ended with %s", type(error).__name__)
        raise


def _spec(sid: str) -> ScenarioSpec:
    return ScenarioSpec.model_validate_json((WS.scenario_dir(sid) / "scenario.json").read_text())


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _redact(text: str) -> str:
    """Replace the values of secret-looking environment variables inside error text before it is stored."""
    for k, v in secret_values().items():
        if v in text:
            text = text.replace(v, f"[redacted:{k}]")
    return text


def _accepted_kwargs(fn, candidates: dict) -> dict:
    """Keep only keyword arguments `fn` accepts (all of them if it takes **kwargs)."""
    params = inspect.signature(fn).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(candidates)
    return {k: v for k, v in candidates.items() if k in params}


def scenario_id_for(instance_id: str) -> str:
    """Same rule as swesim.stages.draft_spec.draft: lower-case, '__' -> '--'."""
    sid = instance_id.lower().replace("__", "--")
    if not ID_RE.fullmatch(sid):
        raise ApplicationError(f"instance id {instance_id!r} does not map to a valid scenario id", non_retryable=True)
    return sid


# ------------------------------------------------------------------ pipeline activities

def probe_models(p: dict) -> dict:
    """One tiny request per model id through the provider's own SDK (`roles.probe_id`; never raises, never carries a key).
    Every role in `roles.ROLE_CHAINS` resolves along its chain, configured id first (`roles.resolve_roles`): judge and draft must
    differ (advance the draft chain, then the judge chain, and record why); the solver is never substituted. Evidence:
    workspace/evidence/model-probe.json with configured/resolved ids and the reasoning."""
    results: dict[str, dict] = {}

    def probe(model_id: str) -> bool:
        if model_id not in results:
            r = roles.probe_id(model_id)
            results[model_id] = {"ok": r.reachable, "error": None if r.reachable else _redact(f"{r.error_class}: {r.error}"),
                                 "latency_ms": r.latency_ms, "served_provider": r.served_provider}
        return results[model_id]["ok"]

    res = roles.resolve_roles(probe, configured={role: p[role] for role in roles.ROLE_CHAINS if p.get(role)})
    needs_human = list(res.needs_human)
    solver_unavailable = [m for m in (p.get("models") or []) if not probe(m)]
    needs_human += [f"solver model {m} unavailable; no silent substitution" for m in solver_unavailable]
    out = {"models": results, "configured": dict(res.configured), "resolved": dict(res.roles), "substitutions": dict(res.substitutions),
           "notes": list(res.reasoning), "needs_human": needs_human, "solver_unavailable": solver_unavailable, "chains": roles.ROLE_CHAINS,
           "probed_at": time.time()}
    atomic_json(WS.root / "evidence" / "model-probe.json", out)
    return out


@swesim_activity
async def probe_models_activity(p: dict) -> dict:
    return await blocking(lambda: probe_models(p))


@swesim_activity
async def discover_activity(p: dict) -> dict:
    """GitHub discovery without probe builds (AUTHORING): `github_repo.discover` -> candidate rows (candidate_to_row + build_recipe)
    into candidates/github-repo--<owner>__<repo>--<band>.jsonl plus the reject histogram (`.rejects.json`) and a `.status` marker when
    nothing qualified. Probing happens per candidate in `probe_candidate_activity`. Like mine, a rerun rewrites the file: discovery
    is the freshest PR list the operator asked for. Returned paths reference immutable per-workflow snapshots."""
    gr = importlib.import_module("swesim.sources.github_repo")
    repo = gr.normalize_repo(p["repo"])
    band = p.get("kind") or "feature"
    if band not in ("bugfix", "feature"):
        raise ValueError("discovery band must be bugfix or feature")
    if p.get("max_candidates") is not None and int(p["max_candidates"]) < 1:
        raise ValueError("max_candidates must be positive")
    kwargs = _accepted_kwargs(gr.discover, {k: p[k] for k in ("since", "pr", "max_prs", "kind") if p.get(k) is not None})
    kwargs.setdefault("since", "2026-06-01")
    cands, rejects = await blocking(lambda: gr.discover(repo, **kwargs))
    limit = int(p.get("max_candidates") or 0) or len(cands)
    rows = []
    for c in cands[:limit]:
        row = gr.candidate_to_row(c); row.update(gr.build_recipe(repo, c["base_commit"])); rows.append(row)
    source_key = f"github-repo--{repo.replace('/', '__')}--{band}"
    out = WS.candidates_dir / f"{source_key}.jsonl"; out.parent.mkdir(parents=True, exist_ok=True)
    candidate_text = "".join(json.dumps(r) + "\n" for r in rows)
    batching.atomic_text(out, candidate_text)
    rejects = {**dict(rejects), **({"max-candidates-cut": len(cands) - limit} if len(cands) > limit else {})}
    atomic_json(out.with_suffix(".rejects.json"), rejects)
    rejects_bytes = json.dumps(rejects, sort_keys=True, indent=2, allow_nan=False).encode()
    rejects_sha256 = hashlib.sha256(rejects_bytes).hexdigest()
    workflow_id = activity.info().workflow_id if activity.in_activity() else p.get("workflow_id", "direct")
    snapshot_id = hashlib.sha256(candidate_text.encode() + rejects_bytes).hexdigest()
    snapshot_dir = batching.directory(WS, workflow_id) / "discovery" / source_key
    snapshot = snapshot_dir / f"{snapshot_id}.jsonl"
    batching.atomic_text(snapshot, candidate_text)
    rejects_snapshot = snapshot.with_suffix(".rejects.json")
    atomic_json(rejects_snapshot, rejects)
    status = out.with_suffix(".status")
    if not rows:
        status.write_text("no-valid-candidate")
    elif status.exists():
        status.unlink()
    return {"candidates_path": str(snapshot), "canonical_candidates_path": str(out), "source_key": source_key, "band": band, "repo": repo,
            "discovered": len(cands), "written": len(rows), "rejects": rejects, "rejects_sha256": rejects_sha256, "rejects_path": str(rejects_snapshot)}


@swesim_activity
async def mine_activity(p: dict) -> dict:
    """Source mining: SWE-bench pool rows, or the GitHub path with up to `max_builds` probe builds inside the activity (legacy for
    GitHub; CreateTasksWorkflow uses discover_activity and probes per candidate)."""
    M = importlib.import_module("swesim.stages.mine")
    source = p.get("source", "swebench-annotated")
    if source == "github-repo":
        fn = M.mine_github_repo
        kwargs = _accepted_kwargs(fn, {k: p[k] for k in ("pr", "since", "max_candidates", "max_builds", "max_prs", "kind") if k in p})
        path = await blocking(lambda: fn(WS, p["repo"], **kwargs))
    else:
        fn = getattr(M, "mine_swebench_annotated", None) or M.mine_swebench
        pytest_only = p.get("pytest_repos_only", True)
        kwargs = _accepted_kwargs(fn, {"csv_path": Path(p.get("csv_path") or DEFAULT_CSV), "limit": p.get("limit"), "pytest_repos_only": pytest_only,
                                       "instance_ids": p.get("instance_ids"), "pytest_only": pytest_only, "pool": p.get("pool", "A")})
        path = await blocking(lambda: fn(WS, **kwargs))
    return {"candidates_path": str(path)}


def record_judge_cost(row: dict, model_id: str) -> dict | None:
    """One `judge` cost row per judged candidate (`swesim.costs.record_cost`, filed under the running workflow id; "direct" when
    a stage command runs the judge outside Temporal). Returns the outcome; a cost-ledger problem never fails the judge."""
    usage = (row.get("judge") or {}).get("usage")
    if not usage:
        return None
    try:
        costs = importlib.import_module("swesim.costs")
    except ImportError:
        return {"recorded": False, "reason": "swesim.costs is not available on this worker"}
    workflow_id = activity.info().workflow_id if activity.in_activity() else "direct"
    try:
        rec = costs.record_cost(WS.layout, None, workflow_id, "judge", model_id, costs.Usage.model_validate(usage))
    except (ValueError, OSError) as e:  # an unknown model id or an unwritable ledger is reported, not a judge failure
        return {"recorded": False, "reason": f"{type(e).__name__}: {str(e)[:200]}"}
    return {"recorded": True, "usd": float(rec.usd) if rec.usd is not None else None, "price_source": rec.price_source, "workflow_id": workflow_id}


@swesim_activity
async def judge_activity(p: dict) -> dict:
    """Legacy whole-file judge used only by PipelineWorkflow; CandidateWorkflow judges one row at a time (`judge_one_activity`)."""
    from swesim.stages.judge import judge_candidate

    src = Path(p["candidates_path"]); out = src.with_suffix(".judged.jsonl"); rows = []
    # Idempotent stage output is retained; an explicit new pipeline version is required to regenerate.
    if out.exists():
        rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    else:
        lines = [line for line in src.read_text().splitlines() if line.strip()][: p.get("max_candidates", 20)]
        for line in lines:
            row = json.loads(line)
            row["judge"] = await blocking(lambda row=row: judge_candidate(row, p["judge_model"]))
            record_judge_cost(row, p["judge_model"])
            rows.append(row)
        tmp = out.with_name(out.name + ".tmp")  # write-then-rename: a crash never leaves a truncated file that a rerun would trust
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
        os.replace(tmp, out)
    return {"judged_path": str(out), "instance_ids": [r["instance_id"] for r in rows if r.get("judge", {}).get("keep")]}


def is_leak_reason(reason) -> bool:
    """Same rule as workflows._is_leak_reason (kept separate: the workflow module imports only temporalio)."""
    r = str(reason).strip().lower()
    return r.startswith("withholding") or "leak" in r or "withheld" in r


@swesim_activity
async def list_candidates_activity(p: dict) -> dict:
    """Candidate identifiers from a candidates.jsonl (for CreateTasksWorkflow's fan-out); ids only, no row bodies."""
    lines = [line for line in Path(p["candidates_path"]).read_text().splitlines() if line.strip()]
    if p.get("max_candidates"):
        lines = lines[: int(p["max_candidates"])]
    out = []
    for line in lines:
        row = json.loads(line)
        out.append({"instance_id": row["instance_id"], "scenario_id": scenario_id_for(row["instance_id"])})
    return {"candidates": out}


ARCHIVE_FILES = ("scenario.draft.json", "scenario.json", "compile.json", "validation.json", "approval.json", "judge.json", "draft.meta.json", "difficulty.json", "audit.json")


def scenario_version(sid: str) -> dict:
    """Highest known version of a scenario: max over scenario.json, the registry entry, any draft on disk (scenario.draft.json
    and scenario.draft.rejected-*.json: a rebuild that was rejected or ended invalid consumed its `create:<sid>@v<n>` id) and
    archive/v<n> directories. `next` is version + 1, or 1 when nothing exists."""
    sdir = WS.scenario_dir(sid); versions: list[int] = []
    for name in ("scenario.json", "scenario.draft.json", *sorted(q.name for q in sdir.glob("scenario.draft.rejected-*.json"))):
        f = sdir / name
        if f.exists():
            versions.append(int((_read_json(f) or {}).get("version") or 1))
    for d in sdir.glob("archive/v*"):
        if d.is_dir() and d.name[1:].isdigit():
            versions.append(int(d.name[1:]))
    scenarios = (_read_json(WS.registry_path) or {}).get("scenarios") or {}
    for key, entry in scenarios.items():
        entry = entry if isinstance(entry, dict) else {}
        if key == sid or str(key).startswith(sid + "@") or entry.get("id") == sid:
            versions.append(int(entry.get("version") or 1))
    version = max(versions) if versions else None
    return {"scenario_id": sid, "exists": version is not None, "version": version, "next": (version or 0) + 1}


def archive_scenario(sid: str, previous_version: int) -> dict:
    """Freeze assets and all compiled variants, publish a verified snapshot, then remove old live metadata.
    Files are copied idempotently using file write-then-rename (flat GCS buckets cannot atomically rename directories).
    The archive's assets and tasks remain valid against their original spec/build after a new draft overwrites live files.
    An archived.json marker makes a retry a no-op, so a newer partial draft is never archived as its predecessor.
    """
    sdir = WS.scenario_dir(sid); target = sdir / "archive" / f"v{int(previous_version)}"; marker = target / "archived.json"
    if marker.exists():
        return {**_read_json(marker), "already_archived": True}
    target.mkdir(parents=True, exist_ok=True)
    archive.copy_tree(sdir / "assets", target / "assets")
    task_dirs = {}
    for condition in ("full", "hidden", "interactive"):
        old_task = WS.task_dir(sid, condition)
        if old_task.is_dir():
            archive.copy_tree(old_task, target / "tasks" / condition)
            task_dirs[condition] = str(target / "tasks" / condition)
    names = sorted(set(ARCHIVE_FILES) | {q.name for q in sdir.glob("scenario.draft.rejected-*.json")} |
                   {q.name for q in target.glob("scenario.draft.rejected-*.json")})
    for name in [*names, "scores.json", "approval.note.json"]:
        if (sdir / name).is_file():
            archive.copy_file(sdir / name, target / name)
    snapshot_path = target / "snapshot.json"
    snapshot = {"files": archive.manifest(target), "task_dirs": task_dirs}
    if snapshot_path.exists() and _read_json(snapshot_path) != snapshot:
        raise ValueError("completed archive snapshot differs from the preserved files")
    atomic_json(snapshot_path, snapshot)  # all archive files verified before any live metadata is removed
    moved = [name for name in names if (target / name).is_file()]
    for name in moved:
        (sdir / name).unlink(missing_ok=True)
    record = {"scenario_id": sid, "previous_version": int(previous_version), "archive_dir": str(target), "archived": moved, "at": time.time(),
              "snapshot_path": str(snapshot_path), "task_dirs": task_dirs,
              "kept": ["assets/", "scores.json", "workspace/tasks/<sid>__<condition> (compile rewrites them)"]}
    atomic_json(marker, record)
    return record


@swesim_activity
async def scenario_version_activity(p: dict) -> dict:
    return scenario_version(p["scenario_id"])


@swesim_activity
async def archive_scenario_activity(p: dict) -> dict:
    return await blocking(lambda: archive_scenario(p["scenario_id"], int(p["previous_version"])))


@swesim_activity
async def judge_one_activity(p: dict) -> dict:
    """Judge one candidate row into its own one-row `<candidates_dir>/judged/<scenario_id>.judged.jsonl`
    (parallel-safe; retained on rerun). draft_activity reads that file unchanged."""
    from swesim.stages.judge import judge_candidate

    src = Path(p["candidates_path"]); sid = scenario_id_for(p["instance_id"])
    out = WS.candidates_dir / "judged" / f"{sid}.judged.jsonl"
    if out.exists():
        row = json.loads(out.read_text().splitlines()[0]); cost = None  # retained output: judged (and paid for) earlier
    else:
        row = next((json.loads(line) for line in src.read_text().splitlines() if line.strip() and json.loads(line).get("instance_id") == p["instance_id"]), None)
        if row is None:
            raise ApplicationError(f"candidate {p['instance_id']} not in {src}", non_retryable=True)
        row["judge"] = await blocking(lambda: judge_candidate(row, p["judge_model"]))
        cost = record_judge_cost(row, p["judge_model"])
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp"); tmp.write_text(json.dumps(row) + "\n"); os.replace(tmp, out)
    judge = row.get("judge", {})
    return {"judged_path": str(out), "instance_id": p["instance_id"], "scenario_id": sid, "keep": bool(judge.get("keep")),
            "reasons": [] if judge.get("keep") else ["judge did not keep the candidate"], "judge_model": judge.get("model_id", p.get("judge_model")),
            "cost": cost}


@swesim_activity
async def probe_candidate_activity(p: dict) -> dict:
    """Measure FAIL_TO_PASS / PASS_TO_PASS on base and gold for one judged row (Harbor on Modal; one attempt, and
    harbor_cli reconciles an existing job dir instead of relaunching). The probed row replaces the row in the
    per-candidate judged file, so draft_activity reads the measured lists. Retained: a row with probe.measured is reused."""
    judged_path = Path(p["judged_path"]); iid = p["instance_id"]; sid = p.get("scenario_id") or scenario_id_for(iid)
    rows = [json.loads(line) for line in judged_path.read_text().splitlines() if line.strip()]
    idx = next((i for i, r in enumerate(rows) if r.get("instance_id") == iid), None)
    if idx is None:
        raise ApplicationError(f"candidate {iid} not in {judged_path}", non_retryable=True)
    row = rows[idx]
    if not _probe_measured(row):
        M = importlib.import_module("swesim.stages.mine")
        probe_candidates = getattr(M, "probe_candidates", None)
        dropped: dict = {}
        if probe_candidates is not None:
            # Contract (swesim.stages.mine.probe_candidates): writes <judged>.probed.jsonl (+ .probed.summary.json) with the
            # measured fail_to_pass / pass_to_pass replacing the source lists (originals under swebench_*), probe metadata
            # (`probe.statuses`, which draft requires), and reject_reason not-reproducible-offline | probe-failed.
            out = Path(str(await blocking(lambda: probe_candidates(WS, judged_path, [iid], int(p.get("max_builds", 1))))))
            probed = _read_row(out, iid)
            if probed is None:
                raise RuntimeError(f"probe_candidates wrote no row for {iid} to {out}")
            error = str((probed.get("probe") or {}).get("error") or "")
            if error and not error.startswith("ValueError"):
                # HarborRunError / build / digest failures are infrastructure: fail the child (rerunnable), no terminal status.
                raise RuntimeError(f"probe infrastructure failure for {iid}: {error}")
            summary = _read_json(out.with_suffix(".summary.json"))
            dropped = {k: v for k, v in summary.items() if k not in ("rows", "probed")}
            row = {**row, **probed}
        else:
            gr = importlib.import_module("swesim.sources.github_repo")
            probe_tests = getattr(gr, "probe_tests", None)
            if probe_tests is None:
                raise ApplicationError("no probe available: swesim.stages.mine.probe_candidates (sources branch) and "
                                       "swesim.sources.github_repo.probe_tests are both missing", non_retryable=True)
            row = {**row, "swebench_fail_to_pass": list(row.get("fail_to_pass") or []), "swebench_pass_to_pass": list(row.get("pass_to_pass") or [])}
            try:
                res = await blocking(lambda: probe_tests(row, WS))
            except ValueError as e:
                # Measured but unusable (nothing fails on base and passes with gold; unreliable base/gold statuses): terminal.
                row.update(fail_to_pass=[], probe={**(row.get("probe") or {}), "error": f"ValueError: {e}"}, reject_reason="not-reproducible-offline")
                dropped = {"probe-error": str(e)}
            else:
                row.update(fail_to_pass=list(res.get("fail_to_pass") or []), pass_to_pass=list(res.get("pass_to_pass") or []),
                           probe=res.get("probe") or {k: res.get(k) for k in ("statuses", "probe_dir", "dropped") if k in res})
                for k in ("test_python", "dockerfile"):
                    if res.get(k):
                        row[k] = res[k]
                dropped = dict(res.get("dropped") or {})
                if not row["fail_to_pass"]:
                    row["reject_reason"] = "not-reproducible-offline"
        row["probe_workflow"] = {"measured_at": time.time(), "dropped": dropped}
        rows[idx] = row
        tmp = judged_path.with_name(judged_path.name + ".tmp"); tmp.write_text("".join(json.dumps(r) + "\n" for r in rows)); os.replace(tmp, judged_path)
    probe = row.get("probe") or {}
    f2p = list(row.get("fail_to_pass") or []); p2p = list(row.get("pass_to_pass") or [])
    reproducible = bool(f2p) and not probe.get("error") and row.get("reject_reason") not in ("not-reproducible-offline", "probe-failed")
    reason = None if reproducible else (probe.get("error") or row.get("reject_reason") or
                                        "measured FAIL_TO_PASS is empty: no test fails on base and passes on gold in the offline probe")
    return {"judged_path": str(judged_path), "instance_id": iid, "scenario_id": sid, "fail_to_pass": f2p if reproducible else [], "pass_to_pass": p2p,
            "probe_dir": probe.get("probe_dir") or row.get("probe_dir"), "reproducible": reproducible, "reason": reason,
            "dropped": (row.get("probe_workflow") or {}).get("dropped") or probe.get("dropped") or {}}


def _probe_measured(row: dict) -> bool:
    """A row already carries offline probe evidence: probe metadata (`statuses`) or a recorded probe error from
    probe_candidates/probe_tests, or mine's own probe output (`probe_dir`), or this activity's marker."""
    probe = row.get("probe") or {}
    return bool(probe.get("statuses") or probe.get("error") or row.get("probe_dir") or row.get("probe_workflow"))


def _read_row(path: Path, iid: str) -> dict | None:
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if r.get("instance_id") == iid:
                return r
    return None


@swesim_activity
async def precheck_draft_activity(p: dict) -> dict:
    """Hold mode: schema, asset and withholding-leak checks on scenario.draft.json; writes nothing, approves nothing."""
    AP = importlib.import_module("swesim.stages.approve")
    sid = p["scenario_id"]; sdir = WS.scenario_dir(sid); draft = sdir / "scenario.draft.json"
    if not draft.is_file():
        return {"ok": False, "reasons": [f"missing {draft}"], "scenario_id": sid, "spec_hash": None}
    try:
        spec = ScenarioSpec.model_validate_json(draft.read_text())
    except Exception as e:  # noqa: BLE001 - schema failure is a result, not an infrastructure error
        return {"ok": False, "reasons": [f"schema: {str(e)[:300]}"], "scenario_id": sid, "spec_hash": None}
    reasons: list[str] = []
    check_assets = getattr(AP, "check_assets", None)
    if check_assets is not None:
        reasons += [str(r) for r in (await blocking(lambda: check_assets(sdir, spec)) or [])]
    auto_gates = getattr(AP, "auto_gates", None)
    if auto_gates is not None:
        reasons += [str(r) for r in (await blocking(lambda: auto_gates(spec, sdir)) or []) if is_leak_reason(r)]
    return {"ok": not reasons, "reasons": reasons, "scenario_id": spec.id, "spec_hash": spec.spec_hash}


@swesim_activity
async def human_approve_activity(p: dict) -> dict:
    """Hold mode approve: the signalled spec_hash must equal the draft on disk; then approve_scenario(auto=False)
    writes scenario.json + approval.json (approved_by human-command, review_needed false). Mismatch -> non-retryable."""
    from swesim.stages.approve import approve_scenario

    sid = p["scenario_id"]; draft = WS.scenario_dir(sid) / "scenario.draft.json"
    if not draft.is_file():
        raise ApplicationError(f"missing {draft}", non_retryable=True)
    spec = ScenarioSpec.model_validate_json(draft.read_text())
    if p.get("spec_hash") != spec.spec_hash:
        raise ApplicationError("signalled spec_hash differs from the draft on disk; refresh the review queue", non_retryable=True)
    r = await blocking(lambda: approve_scenario(WS, sid, auto=False))
    if not r.get("approved"):
        raise ApplicationError("human approval refused by the schema/asset checks: " + "; ".join(map(str, r.get("reasons", []))), non_retryable=True)
    if r.get("spec_hash") != spec.spec_hash:
        raise ApplicationError("approval hash differs from the reviewed draft", non_retryable=True)
    atomic_json(WS.scenario_dir(sid) / "approval.note.json", {"scenario_id": sid, "spec_hash": spec.spec_hash, "note": p.get("note"), "at": time.time()})
    return {"approved": True, "scenario_id": sid, "spec_hash": spec.spec_hash, "approved_by": "human-command"}


@swesim_activity
async def draft_activity(p: dict) -> dict:
    from swesim.stages.draft_spec import draft

    rows = [json.loads(line) for line in Path(p["judged_path"]).read_text().splitlines() if line.strip()]
    row = next(r for r in rows if r["instance_id"] == p["instance_id"])
    sid = scenario_id_for(p["instance_id"]); sdir = WS.scenario_dir(sid); path = sdir / "scenario.draft.json"
    if p.get("force") and path.exists():
        n = 1
        while (sdir / f"scenario.draft.rejected-{n}.json").exists():
            n += 1
        path.rename(sdir / f"scenario.draft.rejected-{n}.json")  # keep the rejected draft as evidence
    if not path.exists():
        wanted: dict = {}
        if p.get("reject_reasons"):
            wanted["reject_reasons"] = p["reject_reasons"]
        if p.get("version"):
            wanted["version"] = int(p["version"])  # rebuilds draft the next version; draft writes spec["version"] and draft.meta.json["version"]
        wanted["layout"] = WS.layout
        wanted["workflow_id"] = activity.info().workflow_id if activity.in_activity() else "direct"
        kwargs = _accepted_kwargs(draft, wanted)
        await blocking(lambda: draft(row, row.get("judge", {}), p["draft_model"], sdir, **kwargs))
    return {"scenario_id": sid, "draft_path": str(path), "version": int(p.get("version") or 1)}


@swesim_activity
async def auto_approve_activity(p: dict) -> dict:
    from swesim.stages.approve import approve_scenario

    # Code-owned gates decide; approval.json records approved_by: auto-validators, review_needed: true.
    return await blocking(lambda: approve_scenario(WS, p["scenario_id"], auto=True))


@swesim_activity
async def verify_approval_activity(p: dict) -> dict:
    if not (WS.scenario_dir(p["scenario_id"]) / "scenario.json").exists():
        raise ApplicationError(f"scenario.json missing for {p['scenario_id']}; run `swesim approve` before signalling", non_retryable=True)
    spec = _spec(p["scenario_id"]); approval_path = WS.scenario_dir(spec.id) / "approval.json"
    if not approval_path.exists():
        raise ApplicationError(f"approval.json missing for {spec.id}", non_retryable=True)
    approval = json.loads(approval_path.read_text())
    if p["spec_hash"] != spec.spec_hash or approval.get("spec_hash") != spec.spec_hash:
        raise ApplicationError("approval hash differs from frozen spec", non_retryable=True)
    return {"scenario_id": spec.id, "spec_hash": spec.spec_hash}


@swesim_activity
async def compile_activity(p: dict) -> dict:
    from swesim.stages.compile import compile_scenario

    spec = _spec(p["scenario_id"])
    r = await blocking(lambda: compile_scenario(WS, spec, RunConfig.default("interactive"), p["stakeholder_url"]))
    task_dirs = getattr(r, "task_dirs", None) or {}
    return {"compile_path": str(r.compile_json), "image_digest": getattr(r, "image_digest", None), "task_dirs": {k: str(v) for k, v in dict(task_dirs).items()},
            "compiled_hash": _read_json(Path(r.compile_json)).get("compiled_hash"), "controls": validation.control_names(spec)}


@swesim_activity
async def validate_activity(p: dict) -> dict:
    """Legacy whole-scenario validation (all control runs in one activity), used by PipelineWorkflow only; CandidateWorkflow runs
    validate_control_activity per control and evaluate_controls_activity for the verdict."""
    from swesim.stages.validate import validate_scenario

    r = await blocking(lambda: validate_scenario(WS, _spec(p["scenario_id"]), RunConfig.default("full"), p["stakeholder_url"]))
    return {"status": r["status"], "failures": r.get("failures", [])}


def _compile_info(sid: str) -> dict:
    comp = _read_json(WS.scenario_dir(sid) / "compile.json")
    if not comp.get("compiled_hash"):
        raise ApplicationError(f"compile.json missing for {sid}; compile before validating", non_retryable=True)
    return comp


@swesim_activity
async def validate_control_activity(p: dict) -> dict:
    """One control run of the validation (BUILD): Harbor on Modal through harbor_cli, which reconciles an existing job dir instead
    of relaunching. `HarborRunError` propagates and the BUILD class retries this one control; the result carries paths and the
    small reward summary, never report bodies."""
    spec = _spec(p["scenario_id"]); comp = _compile_info(spec.id)
    if p.get("compiled_hash") and p["compiled_hash"] != comp["compiled_hash"]:
        raise ValueError("compiled build changed after control planning")
    return await blocking(lambda: validation.run_control(WS, spec, p["control"], comp["compiled_hash"]))


@swesim_activity
async def evaluate_controls_activity(p: dict) -> dict:
    """The verdict over the recorded control runs (`trial_dirs`: control name -> Harbor trial dir): validate.py's rules over each
    dir's reward and report, then scenarios/<sid>/validation.json in validate_scenario's shape."""
    spec = _spec(p["scenario_id"]); comp = _compile_info(spec.id)
    if p.get("compiled_hash") and p["compiled_hash"] != comp["compiled_hash"]:
        raise ValueError("compiled build changed after control execution")
    r = await blocking(lambda: validation.assemble_validation(WS, spec, dict(p.get("trial_dirs") or {}), comp.get("compiled_hash"), comp.get("image_digest"),
                                                              comp.get("run_config_hash")))
    return {"status": r["status"], "failures": r.get("failures", []), "validation_path": str(WS.scenario_dir(spec.id) / "validation.json"),
            "controls": {k: v.get("outcome") for k, v in r["controls"].items()}}


@swesim_activity
async def register_activity(p: dict) -> dict:
    from swesim.stages.register import register_scenario

    r = await blocking(lambda: register_scenario(WS, _spec(p["scenario_id"])))
    # Coerce to plain JSON (Path, datetime, models -> str) so the activity result never fails to serialize after the registry write.
    return {"registry_entry": json.loads(json.dumps(r, default=str))}


def plan_trials(p: dict) -> dict:
    """Precompute trial requests with the actual per-model RunConfig; trial ids are the workflow ids."""
    spec = _spec(p["scenario_id"]); comp = json.loads((WS.scenario_dir(spec.id) / "compile.json").read_text()); trials = []
    offset = int(p.get("attempt_offset", 0))  # manual retries use fresh attempt indices, never the same trial id
    if p.get("purpose") == "screen":
        from swesim.stages.difficulty import SCREEN_COUNTS, SCREEN_MODEL
        models = p.get("models") or [SCREEN_MODEL]
        if len(models) != 1:
            raise ValueError("difficulty screen requires one model")
        p = {**p, "models": models, "conditions": list(SCREEN_COUNTS)}
        counts = SCREEN_COUNTS
    else:
        counts = {cond: p.get("k", 1) for cond in p.get("conditions", ["interactive"])}
    stakeholder_model = p.get("stakeholder_model")  # the probe's resolved stakeholder id; part of every trial's RunConfig and hash
    for cond in p.get("conditions", ["interactive"]):
        for model in p["models"]:
            for i in range(offset, offset + counts[cond]):
                profile = p.get("brief_profile", "withheld")
                if cond == "full" or not any(b.profile == profile for b in spec.briefs):
                    profile = "natural"
                kwargs = {"condition": cond, "model": model, "compiled_hash": comp["compiled_hash"], "k": int(p.get("metric_k") or (1 if p.get("purpose") == "screen" else p.get("k", 1))), "purpose": p.get("purpose", "demonstration"), "brief_profile": profile}
                if stakeholder_model:
                    kwargs["stakeholder"] = {"model_id": stakeholder_model}
                rc = RunConfig(**kwargs)
                # Same scaffold identity as run_trial, from the same compiled task, or the planned id never matches the record.
                from swesim.stages.run import scaffold_identity
                rc = RunConfig.model_validate({**rc.model_dump(), "agent_config": scaffold_identity(WS.task_dir(spec.id, cond), rc)})
                tid = trial_id(spec.spec_hash, cond, rc.run_config_hash, i)
                trials.append({"trial_id": tid, "scenario_id": spec.id, "version": spec.version, "spec_hash": spec.spec_hash, "condition": cond, "model": model,
                                   "stakeholder_model": rc.stakeholder.model_id, "run_config": rc.model_dump(mode="json"),
                                   "run_config_hash": rc.run_config_hash, "attempt_index": i, "stakeholder_url": p["stakeholder_url"]})
    if len({t["trial_id"] for t in trials}) != len(trials):
        raise ValueError("duplicate matrix entries")
    if p.get("audit_model"):
        for trial in trials:
            trial["audit_model"] = p["audit_model"]
    return {"scenario_id": spec.id, "trials": trials}


@swesim_activity
async def plan_trials_activity(p: dict) -> dict:
    return plan_trials(p)


@swesim_activity
async def prepare_trial_plan_activity(p: dict) -> dict:
    path = batching.plan_path(WS, p["workflow_id"], p["scenario_id"])
    if path.exists():
        return batching.write_plan(path, [])
    return await blocking(lambda: batching.trial_plan(WS, path, plan_trials(p)["trials"]))


@swesim_activity
async def prepare_candidate_plan_activity(p: dict) -> dict:
    path = batching.plan_path(WS, p["workflow_id"], "candidates")
    if path.exists():
        return batching.write_plan(path, [])
    items = []; seen = set(); duplicates = 0
    for source_index, source in enumerate(p["sources"]):
        # Freeze the full candidate rows outside history before children begin. A later discovery may rewrite
        # the canonical source file while this workflow is still judging/probing its original selection.
        snapshot = path.parent / (str(source_index) + '.candidates.jsonl')
        if not snapshot.exists():
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            temporary = snapshot.with_suffix('.tmp')
            temporary.write_text(Path(source["candidates_path"]).read_text())
            os.replace(temporary, snapshot)
        rows = [json.loads(line) for line in snapshot.read_text().splitlines() if line.strip()]
        for row in rows[:source.get("limit") or None]:
            iid = row["instance_id"]; sid = scenario_id_for(iid)
            if sid in seen:
                duplicates += 1
                continue
            seen.add(sid)
            items.append({"instance_id": iid, "scenario_id": sid, "source_key": source["source_key"], "candidates_path": str(snapshot)})
    return batching.write_plan(path, items, duplicates=duplicates)


@swesim_activity
async def read_plan_page_activity(p: dict) -> dict:
    return batching.read_page(WS, p["plan_path"], int(p.get("offset", 0)), int(p.get("limit", batching.CHILDREN_PER_RUN)))


@swesim_activity
async def checkpoint_workflow_activity(p: dict) -> dict:
    return {"checkpoint_path": batching.checkpoint(WS, p["workflow_id"], p["run_id"], p["state"])}


@swesim_activity
async def score_activity(p: dict) -> dict:
    from swesim.stages.score import score_scenario

    return {"scores_path": str(await blocking(lambda: score_scenario(WS, p["scenario_id"], k=p.get("k"))))}


@swesim_activity
async def difficulty_label_activity(p: dict) -> dict:
    from swesim.stages.difficulty import label
    from swesim.stages.score import normalize_record, summarize

    spec = _spec(p["scenario_id"]); comp = _compile_info(spec.id)
    if p.get("plan_path"):
        plan = json.loads(batching.checked_path(WS, p["plan_path"]).read_text())
        tids = [item["trial_id"] for item in plan["items"]]
    else:
        tids = list(p.get("trial_ids") or [])
    if len(tids) != 3 or len(set(tids)) != 3:
        raise ValueError("difficulty screen requires its three explicit trial IDs")
    records = []
    for tid in tids:
        directory = trial_dir(tid)
        record = normalize_record(json.loads((directory / "record.json").read_text()), directory)
        if record.get("trial_id") != tid or record.get("scenario_id") != spec.id or record.get("spec_hash") != spec.spec_hash or record.get("compiled_hash") != comp["compiled_hash"]:
            raise ValueError("difficulty evidence does not match the current scenario and compiled build")
        records.append(record)
    result = label(list(summarize(records, 1).values()), spec_hash=spec.spec_hash, compiled_hash=comp["compiled_hash"])
    path = WS.scenario_dir(spec.id) / "difficulty.json"
    # A retry uses the previously recorded timestamp when it names the identical measurement.
    previous = _read_json(path)
    out = result.model_dump(mode="json")
    if {k: v for k, v in previous.items() if k != "labelled_at"} == {k: v for k, v in out.items() if k != "labelled_at"}:
        out["labelled_at"] = previous["labelled_at"]
    atomic_json(path, out)
    return {"tier": out["tier"], "model": out["model"], "difficulty_path": str(path)}


@swesim_activity
async def export_activity(p: dict) -> dict:
    from swesim.stages.export import export_scenario

    return {"manifest_paths": [str(x) for x in await blocking(lambda: export_scenario(WS, p["scenario_id"]))]}


# ------------------------------------------------------------------ trial lifecycle: evidence, launch once, await

_LAYOUT_CLS = None


def _layout() -> TrialLayout:
    """`swesim.storage.layout.Layout` over the workspace root once the storage stream is merged; the same-shaped adapter until then."""
    global _LAYOUT_CLS
    if _LAYOUT_CLS is None:
        try:
            from swesim.storage import layout as storage_layout
            _LAYOUT_CLS = storage_layout.Layout
        except ImportError:
            _LAYOUT_CLS = WorkspaceLayout
    return _LAYOUT_CLS(root=WS.root)


def trial_dir(tid: str) -> Path:
    return _layout().trial_dir(tid)


def launcher() -> Launcher:
    """The configured launcher, built once from `SETTINGS` (tests assign `LAUNCHER` directly)."""
    global LAUNCHER
    if LAUNCHER is None:
        LAUNCHER = get_launcher(SETTINGS)
    return LAUNCHER


def read_evidence(tid: str) -> TrialEvidence:
    return _read_evidence(_layout(), launcher(), tid)


def launch_once(p: dict) -> dict:
    return _launch_once(_layout(), launcher(), p, image_digest=SETTINGS.worker_image_digest, origin=SETTINGS.origin)


@swesim_activity
async def launch_trial_activity(p: dict) -> dict:
    if p.get("request_path"):
        request = json.loads(batching.checked_path(WS, p["request_path"]).read_text())
        if request["trial_id"] != p["trial_id"]:
            raise ValueError("trial request path belongs to a different trial")
        p = {**request, "run_workflow_id": p.get("run_workflow_id")}
    async with LAUNCH_SLOTS:
        out = await blocking(lambda: launch_once(p))
    # One line per launch decision; the Cloud Logging metric `swesim/trial_reconcile` counts `decision=reconcile`.
    log.info("decision=%s trial=%s launcher=%s runtime=%s launched=%s handle=%s", out["decision"], out["trial_id"], out["launcher"],
             out.get("runtime"), out["launched"], out.get("handle"))
    return out


@swesim_activity
async def await_trial_activity(p: dict) -> dict:
    """Read-only, retryable, heartbeating poll until terminal evidence exists. Never launches."""
    tid = p["trial_id"]; d = trial_dir(tid); unit = unit_name(tid); deadline = time.monotonic() + AWAIT_DEADLINE_SEC; polls = 0
    while True:
        polls += 1
        ev = read_evidence(tid)
        _heartbeat({"trial_id": tid, "poll": polls, "runtime": f"{ev.runtime_state}: {ev.runtime_detail}", "status": ev.status})
        if ev.has_record:
            r = _read_json(d / "record.json")
            if r.get("status") is not None:
                log.info("terminal trial=%s status=%s polls=%d runtime=%s", tid, r["status"], polls, ev.runtime_state)
                return {"trial_id": r.get("trial_id", tid), "status": r["status"], "reward": r.get("reward", {})}
            # record.json exists but is empty/partial: the run stage writes it non-atomically, so the writer may still be
            # flushing. Fall through: keep polling while the runtime runs; report incomplete evidence once it has stopped.
        if ev.has_runner_error:
            return {"trial_id": tid, "status": "runner-error", "unit": unit, "error": _read_json(d / "runner-error.json").get("error")}
        decision = decide_launch(ev)
        if decision == "launch-failed":
            return {"trial_id": tid, "status": "launch-failed", "unit": unit, "reason": "launcher refused or returned nonzero; nothing ran; never relaunch automatically"}
        if decision == "unresolved":
            return {"trial_id": tid, "status": "unresolved", "unit": unit,
                    "reason": "launch evidence exists but the runtime is gone without a terminal record (VM loss or manual cleanup); inspect the runtime's logs and Harbor job; never relaunch"}
        if decision == "launch" and polls >= LAUNCH_GRACE_POLLS:
            return {"trial_id": tid, "status": "launch-failed", "unit": unit, "reason": "no launch evidence for this trial; the launch activity did not reach the launcher"}
        if decision == "ingest" and ev.runtime_state != "running":
            # Terminal status.json without a readable record.json and the runtime is not running: nothing more will appear.
            return {"trial_id": tid, "status": ev.status or "unknown", "unit": unit, "reward": {},
                    "reason": "terminal evidence incomplete (status.json terminal without record.json, or record.json unreadable) and the runtime is not running; read its logs; never relaunch"}
        if decision == "reconcile":
            if ev.runtime_state == "failed":
                return {"trial_id": tid, "status": "unit-failed", "unit": unit, "runtime": ev.runtime_detail,
                        "reason": "runtime failed without a terminal record; read its logs; never relaunch"}
            if ev.runtime_state in ("succeeded", "unknown"):
                return {"trial_id": tid, "status": "unresolved", "unit": unit, "runtime": ev.runtime_detail,
                        "reason": "runtime finished (or cannot be seen from this worker) without terminal evidence; inspect its logs; never relaunch"}
        if time.monotonic() >= deadline:
            raise ApplicationError("no terminal evidence before the await deadline; inspect the same runtime; never relaunch", non_retryable=True)
        await asyncio.sleep(POLL_SECONDS)


# ------------------------------------------------------------------ bookkeeping (CONTROL queue): cost ledger and the spend cap

def _read_json_list(path: Path) -> list:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else list(data.get("records", [])) if isinstance(data, dict) else []


def recorded_spend(settings: Settings, ws: Workspace) -> dict:
    """Known recorded USD plus explicit completeness; both trial and creation ledgers are authoritative fallbacks."""
    from swesim.costs import accounting_status
    accounting = accounting_status(ws)
    fallback = "files"
    if settings.index_enabled:
        try:
            from swesim.index.models import TableFilter
            from swesim.index.store import IndexStore
            store = IndexStore.from_settings(settings)
            if store is not None and hasattr(store, "spend"):
                rows = store.spend()
                unknown = store.table("costs", TableFilter(where={"usd": None}, limit=1)).total
                return {"spend_usd": float(sum(float(r.usd) for r in rows if r.usd is not None)), "source": "index",
                        "records": int(sum(getattr(r, "records", 1) for r in rows)), "unpriced_records": unknown,
                        **accounting, "spend_complete": accounting["usage_complete"] and unknown == 0}
        except Exception as e:  # noqa: BLE001 - files remain the record if the disposable index is unavailable
            fallback = f"files (index unreadable: {type(e).__name__}: {str(e)[:120]})"
        else:
            fallback = "files (index has no spend ledger yet)"
    total = 0.0; records = unpriced = unreadable = 0
    paths = [*sorted((ws.root / "trials").glob("*/costs.json")), *sorted((ws.root / "costs").glob("*.json"))]
    for path in paths:
        try:
            data = json.loads(path.read_text())
            rows = data if isinstance(data, list) else data.get("records", [])
            for rec in rows:
                records += 1
                if rec.get("usd") is None:
                    unpriced += 1
                else:
                    total += float(rec["usd"])
        except (OSError, ValueError, TypeError, AttributeError):
            unreadable += 1
    return {"spend_usd": total, "source": fallback, "records": records, "unpriced_records": unpriced,
            **accounting, "unreadable_cost_files": unreadable, "spend_complete": accounting["usage_complete"] and not (unpriced or unreadable)}


@swesim_activity
async def spend_check_activity(p: dict) -> dict:
    """Recorded spend against the cap: {"spend_usd", "cap_usd", "exceeded", ...}. The cap is `p["cap_usd"]` when given, else
    `Settings.spend_cap_usd`; no cap means never exceeded (the operator enforces the budget by hand)."""
    cap = p.get("cap_usd", None)
    cap = SETTINGS.spend_cap_usd if cap is None else cap
    spend = await blocking(lambda: recorded_spend(SETTINGS, WS))
    cap_usd = float(cap) if cap is not None else None
    return {**spend, "cap_usd": cap_usd, "exceeded": cap_usd is not None and spend["spend_usd"] > cap_usd}


def _solver_usage(costs, record: dict, d: Path):
    """Usage evidence for the solver: the Harbor trial dir the record names (`costs.usage_from_trial_dir`: the agent's trajectory or
    Harbor's agent_result), else trials/<tid>/trajectory.json. None when there is nothing to read."""
    harbor_dir = record.get("trial_dir")
    if harbor_dir and Path(harbor_dir).is_dir() and hasattr(costs, "usage_from_trial_dir"):
        usage = costs.usage_from_trial_dir(Path(harbor_dir))
        if usage is not None:
            return usage
    trajectory = _read_json(d / "trajectory.json")
    try:
        return costs.solver_usage_from_trajectory(trajectory) if trajectory else None
    except getattr(costs, "MissingUsage", ()):  # old injected cost modules do not expose this classification
        return None


@swesim_activity
async def record_cost_activity(p: dict) -> dict:
    """Solver cost of one finished trial into trials/<tid>/costs.json through `swesim.costs` (usage from the trajectory, price from
    the table). Idempotent: a solver record for the trial is written once. Without `swesim.costs`, a record or usage evidence it
    reports `recorded: False` with the reason; it never fails the workflow."""
    tid = p["trial_id"]; d = trial_dir(tid)
    try:
        costs = importlib.import_module("swesim.costs")  # through sys.modules: a worker without the module reports it; tests substitute one
    except ImportError:
        return {"trial_id": tid, "recorded": False, "reason": "swesim.costs is not available on this worker"}
    existing = _read_json_list(d / "costs.json")
    if any(r.get("role") == "solver" for r in existing):
        return {"trial_id": tid, "recorded": False, "reason": "solver cost already recorded", "records": len(existing)}
    record = _read_json(d / "record.json")
    if not record:
        return {"trial_id": tid, "recorded": False, "reason": "no record.json"}
    usage = _solver_usage(costs, record, d)
    if usage is None:
        return {"trial_id": tid, "recorded": False, "reason": "no trajectory or agent result to read usage from"}
    model = record.get("model") or (record.get("run_config") or {}).get("model") or p.get("model")
    out = await blocking(lambda: costs.record_cost(_layout(), tid, p.get("workflow_id"), "solver", model, usage))
    usd = getattr(out, "usd", None)
    return {"trial_id": tid, "recorded": True, "model": model, "usd": float(usd) if usd is not None else None,
            "price_source": getattr(out, "price_source", None), "records": len(existing) + 1}


# ------------------------------------------------------------------ trial runtime housekeeping

CLEANUP_DECISIONS = ("ingest", "runner-error", "launch-failed")  # terminal evidence exists; the runtime resource is no longer the evidence


@swesim_activity
async def cancel_trial_activity(p: dict) -> dict:
    """Ask the launcher to stop the trial's runtime (workflow cancellation). Without launch evidence there is nothing to stop.
    The trial id stays consumed: a later run is a new attempt index, never a relaunch."""
    tid = p["trial_id"]; launch = _read_json(trial_dir(tid) / "launch.json")
    if not launch:
        return {"trial_id": tid, "cancelled": False, "reason": "no launch evidence"}
    await blocking(lambda: launcher().cancel(tid, launch))
    directory = trial_dir(tid)
    if not (directory / "record.json").exists() and not (directory / "runner-error.json").exists():
        atomic_json(directory / "status.json", {**_read_json(directory / "status.json"), "trial_id": tid, "status": "cancelled",
                                                 "finished_at": time.time(), "origin": SETTINGS.origin, "worker_image_digest": SETTINGS.worker_image_digest})
    return {"trial_id": tid, "cancelled": True, "launcher": launcher().name, "handle": launch.get("handle")}


@swesim_activity
async def cleanup_trial_activity(p: dict) -> dict:
    """Release the trial's runtime resource (Cloud Run: delete the Job; systemd/subprocess: nothing) once terminal evidence
    exists and nothing is running. Refuses otherwise: until the record is written the runtime is the evidence."""
    tid = p["trial_id"]; ev = read_evidence(tid); decision = decide_launch(ev)
    if ev.runtime_state == "running" or decision not in CLEANUP_DECISIONS or (decision == "ingest" and not ev.has_record):
        return {"trial_id": tid, "cleaned": False, "reason": f"not terminal: decision={decision} runtime={ev.runtime_state}"}
    launch = _read_json(trial_dir(tid) / "launch.json")
    await blocking(lambda: launcher().cleanup(tid, launch))
    return {"trial_id": tid, "cleaned": True, "launcher": launcher().name, "handle": launch.get("handle")}


# ------------------------------------------------------------------ the per-trial process (runs inside the unit or Job)

def execute_trial_request(request_path: Path) -> dict:
    """Body of the trial process: load the frozen spec and the exact RunConfig, check identity, run the trial.
    Configuration is the process's own `Settings` (the unit or Job sets SWESIM_WORKSPACE, SWESIM_ENV_FILE, ...); secrets
    come from the environment or the env file through `swesim.settings.secret`, never from arguments."""
    p = json.loads(Path(request_path).read_text())
    s = Settings.from_env(); ws = Workspace(s.workspace, s.registry_path)
    from dotenv import load_dotenv

    load_dotenv(s.env_file)  # provider keys for the solver and stakeholder clients; a missing file is a no-op (Cloud Run sets them directly)
    d = ws.root / "trials" / p["trial_id"]
    try:
        from swesim.stages.run import run_trial

        spec = ScenarioSpec.model_validate_json((ws.scenario_dir(p["scenario_id"]) / "scenario.json").read_text())
        rc = RunConfig.model_validate(p["run_config"])
        if spec.spec_hash != p["spec_hash"]:
            raise ValueError("frozen spec changed")
        expected = trial_id(spec.spec_hash, rc.condition, rc.run_config_hash, p["attempt_index"])
        if expected != p["trial_id"]:
            raise ValueError("trial id does not match its run config; refusing to run under another identity")
        worker_secret = secret("SWESIM_WORKER_SECRET", s) if has_secret("SWESIM_WORKER_SECRET", s) else ""
        if rc.condition == "interactive" and not worker_secret:
            raise ValueError("SWESIM_WORKER_SECRET missing from the environment and the env file")
        rec = run_trial(ws, spec, rc, p["attempt_index"], p["stakeholder_url"], worker_secret)
        return {"trial_id": rec.trial_id, "status": rec.status, "reward": rec.reward}
    except Exception as e:
        atomic_json(d / "runner-error.json", {"trial_id": p["trial_id"], "error": _redact(repr(e)), "type": type(e).__name__, "at": time.time()})
        raise


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "run-trial":
        print("usage: python -m swesim.workflows.activities run-trial <workspace/trials/<trial_id>/request.json>", file=sys.stderr)
        return 2
    print(json.dumps(execute_trial_request(Path(argv[1]))))
    return 0


ALL = [probe_models_activity, discover_activity, mine_activity, judge_activity, list_candidates_activity, judge_one_activity, probe_candidate_activity,
       scenario_version_activity, archive_scenario_activity, draft_activity,
       auto_approve_activity, precheck_draft_activity, human_approve_activity, verify_approval_activity, compile_activity,
       validate_activity, validate_control_activity, evaluate_controls_activity, register_activity, plan_trials_activity, launch_trial_activity,
       await_trial_activity, cancel_trial_activity, cleanup_trial_activity, score_activity, export_activity, spend_check_activity, record_cost_activity,
       prepare_trial_plan_activity, prepare_candidate_plan_activity, read_plan_page_activity, checkpoint_workflow_activity]
ALL.append(difficulty_label_activity)

if __name__ == "__main__":
    raise SystemExit(main())
