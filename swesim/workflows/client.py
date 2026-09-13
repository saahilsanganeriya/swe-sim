"""Thin Temporal client helpers used by the CLI, the worker and the console. No secrets in payloads, memos or logs.

Connection settings come from `Settings` (process environment, then the env file named by SWESIM_ENV_FILE): the CLI
reaches the same namespace as the worker without exporting anything. Temporal Cloud is detected by the presence of
`TEMPORAL_API_KEY` (read on demand with `swesim.settings.secret`, TLS on, the namespace routing header set);
`describe_connection` reports only the auth mode.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from swesim.settings import MissingSecret, Settings, get_settings, secret
from swesim.workflows import queues, search_attributes
from swesim.workflows.payloads import check_payload
from swesim.workflows.workflows import (
    CandidateWorkflow,
    CreateTasksWorkflow,
    PipelineWorkflow,
    RunMatrixWorkflow,
    RunTasksWorkflow,
    params_summary,
)

TASK_QUEUE = queues.AUTHORING  # operator workflows start here; children are started here too
TYPES = {"create": "CreateTasksWorkflow", "candidate": "CandidateWorkflow", "run": "RunTasksWorkflow",
         "pipeline": "PipelineWorkflow", "matrix": "RunMatrixWorkflow", "trial": "TrialWorkflow", "operator": "OperatorActionWorkflow"}
REJECT_DUP = WorkflowIDReusePolicy.REJECT_DUPLICATE
TEMPORAL_KEYS = ("TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE", "TEMPORAL_API_KEY")  # the variables the connection depends on


def _settings(environ: Mapping[str, str] | None) -> Settings:
    return get_settings() if environ is None else Settings.from_env(environ)


def connect_kwargs(environ: Mapping[str, str] | None = None) -> dict:
    """Connection settings from `Settings` (or from an explicit mapping, for tools and tests). Local dev server by
    default; Temporal Cloud when `TEMPORAL_API_KEY` is present (TLS on, address like `us-central1.gcp.api.temporal.io:7233`,
    namespace like `swesim.abcde`). The key never appears in logs: `describe_connection` reports only the mode."""
    s = _settings(environ)
    kw: dict = {"target_host": s.temporal_address, "namespace": s.temporal_namespace}
    try:
        api_key = secret("TEMPORAL_API_KEY", s, environ)
    except MissingSecret:
        return kw
    # Temporal Cloud regional endpoints route by this header; measured 2026-09-13: without it the same key is "Request unauthorized".
    kw.update(api_key=api_key, tls=True, rpc_metadata={"temporal-namespace": s.temporal_namespace})
    return kw


def describe_connection(environ: Mapping[str, str] | None = None) -> str:
    kw = connect_kwargs(environ)
    return f"temporal={kw['target_host']} namespace={kw['namespace']} auth={'api-key' if 'api_key' in kw else 'plain'}"


async def connect(environ: Mapping[str, str] | None = None) -> Client:
    return await Client.connect(**connect_kwargs(environ))


_summary = params_summary  # the memo shown by `list_runs` is the same summary the index rows carry


async def _start(workflow_run, params: dict, prefix: str) -> str:
    check_payload(params, "workflow input")
    c = await connect(); wid = f"{prefix}-{uuid.uuid4().hex}"
    await c.start_workflow(workflow_run, params, id=wid, task_queue=TASK_QUEUE, id_reuse_policy=REJECT_DUP, memo={"summary": _summary(params)},
                           search_attributes=search_attributes.for_params(params))
    return wid


async def start_pipeline(params: dict) -> str:
    return await _start(PipelineWorkflow.run, params, "pipeline")


async def start_matrix(params: dict) -> str:
    return await _start(RunMatrixWorkflow.run, params, "matrix")


async def start_create(params: dict) -> str:
    """CreateTasksWorkflow: {sources: [...], judge_model, draft_model, stakeholder_url, max_parallel, approval_mode}."""
    return await _start(CreateTasksWorkflow.run, params, "create")


async def start_run(params: dict) -> str:
    """RunTasksWorkflow: {scenario_ids, models, conditions, k, purpose, brief_profile, stakeholder_model, attempt_offset, max_parallel_trials}."""
    return await _start(RunTasksWorkflow.run, params, "run")


async def approve(workflow_id: str, scenario_id: str, spec_hash: str) -> None:
    """PipelineWorkflow signal mode."""
    c = await connect()
    await c.get_workflow_handle(workflow_id).signal(PipelineWorkflow.approve_spec, {"scenario_id": scenario_id, "spec_hash": spec_hash})


def candidate_workflow_id(scenario_id: str) -> str:
    return f"create:{scenario_id}"


async def approve_candidate(workflow_id: str, scenario_id: str, spec_hash: str, decision: str = "approve", note: str = "") -> str:
    """Hold-mode reviewer decision. `workflow_id` may be the CandidateWorkflow (`create:<scenario_id>`) or its parent
    CreateTasksWorkflow id; the signal always goes to the candidate, whose id is derived from the scenario id."""
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    c = await connect()
    target = workflow_id if workflow_id.startswith("create:") else await _candidate_id_from_parent(c, workflow_id, scenario_id)
    await c.get_workflow_handle(target).signal(CandidateWorkflow.approve, {"scenario_id": scenario_id, "spec_hash": spec_hash, "decision": decision, "note": note})
    return target


async def _candidate_id_from_parent(c: Client, parent_id: str, scenario_id: str) -> str:
    """The parent's progress knows the child's actual id (a rebuild runs as create:<sid>@v<n>); fall back to create:<sid>."""
    try:
        prog = await c.get_workflow_handle(parent_id).query("progress")
        cid = ((prog or {}).get("candidates") or {}).get(scenario_id, {}).get("candidate_workflow_id")
    except Exception:  # noqa: BLE001 - an unreachable or non-create parent falls back to the plain id
        cid = None
    return cid or candidate_workflow_id(scenario_id)


async def progress(workflow_id: str) -> dict:
    c = await connect()
    return await c.get_workflow_handle(workflow_id).query("progress")


async def cancel(workflow_id: str) -> None:
    """Request cancellation; children are cancelled with the parent. A cancelled TrialWorkflow asks its launcher to stop
    the runtime (`cancel_trial_activity`); its trial id is consumed, so a later run uses a new attempt index and is a
    fresh launch, never a relaunch of the cancelled one."""
    c = await connect()
    await c.get_workflow_handle(workflow_id).cancel()


async def open_types() -> list[dict]:
    """Open executions per workflow type, `[{"type", "open_count"}]` sorted by type: the deploy path refuses to roll worker
    pools while a type whose code changed has open executions and the change is not behind `workflow.patched`."""
    c = await connect(); counts: dict[str, int] = {}
    async for wf in c.list_workflows("ExecutionStatus = 'Running'"):
        counts[wf.workflow_type] = counts.get(wf.workflow_type, 0) + 1
    return [{"type": t, "open_count": n} for t, n in sorted(counts.items())]


async def list_runs(kind: str = "all", limit: int = 50) -> list[dict]:
    """Recent workflows of one kind (create | candidate | run | pipeline | matrix | trial | all) with a params summary."""
    if kind != "all" and kind not in TYPES:
        raise ValueError(f"kind must be one of {sorted(TYPES)} or all")
    types = [TYPES[kind]] if kind != "all" else list(TYPES.values())
    query = " OR ".join(f"WorkflowType = '{t}'" for t in types)
    c = await connect(); out: list[dict] = []
    async for wf in c.list_workflows(query, limit=limit):
        try:
            memo = await wf.memo()
        except Exception:  # noqa: BLE001 - a missing/undecodable memo is not an error for a listing
            memo = {}
        out.append({"workflow_id": wf.id, "run_id": wf.run_id, "type": wf.workflow_type,
                    "status": wf.status.name if wf.status is not None else None,
                    "start_time": wf.start_time.isoformat() if wf.start_time else None,
                    "close_time": wf.close_time.isoformat() if wf.close_time else None,
                    "summary": (memo or {}).get("summary", {})})
    return out


def override_request(reason: str, actor: str | None = None) -> dict:
    """The `frontier_override` params entry the CLI flag builds (`swesim run-tasks --frontier-override "<reason>"`): `{"actor",
    "reason"}`; the actor defaults to the CLI user (`getpass.getuser()`: the process's login name, a stdlib lookup, not a settings
    field). An empty reason is a ValueError: an override without a reason is never recorded."""
    import getpass

    text = (reason or "").strip()
    if not text:
        raise ValueError("a frontier override needs a reason")
    return {"actor": (actor or "").strip() or getpass.getuser(), "reason": text}


# Temporal's `WorkflowExecutionStatus` names -> the index's `WorkflowStatus` (interfaces §2). RUNNING is left alone.
TEMPORAL_STATUS: dict[str, str] = {"COMPLETED": "completed", "FAILED": "failed", "CANCELED": "cancelled", "TERMINATED": "terminated",
                                   "TIMED_OUT": "timed_out", "CONTINUED_AS_NEW": "continued_as_new"}
UNKNOWN_FAILURE = "Temporal has no execution for this workflow/run (retention passed, or a row from another namespace): the terminal state is unknown"
RECONCILE_PAGE = 500     # rows read per index page
RECONCILE_ROWS = 200     # rows listed in the result (the activity result must stay small)


async def reconcile_workflows(store_or_settings, *, max_age_hours: int | None = 48, client: Client | None = None) -> dict:
    """P1-6: close `running` workflow rows from Temporal's own view of each execution. Every `running` row started within the
    last `max_age_hours` (None or 0: every running row) is `describe`d by workflow id and run id; a closed execution writes
    its terminal status (completed | failed | cancelled | terminated | timed_out | continued_as_new) with Temporal's close
    time and `lifecycle_source="temporal"`, which no later workflow hook can reopen (`swesim.index.history.choose_workflow`);
    an execution Temporal no longer has is closed as `unknown`; a still-running one is left alone. Rows already terminal are
    never read. One row's RPC error is reported in `errors`, never raised. The summary is also written to the record
    (`Layout.reconciliation_checkpoint`). Returns `{"checked", "reconciled", "unknown", "still_running", "errors", "rows"}`,
    or `{"indexed": False, "reason": "index disabled"}` without a database."""
    from temporalio.service import RPCError, RPCStatusCode
    from swesim.index.activities import index_workflow
    from swesim.index.models import WorkflowFilter
    from swesim.index.store import IndexStore
    from swesim.paths import atomic_json
    from swesim.storage.layout import Layout
    if isinstance(store_or_settings, IndexStore):
        store, settings = store_or_settings, store_or_settings.settings
    else:
        settings = store_or_settings; store = IndexStore.from_settings(settings)
    if store is None:
        return {"indexed": False, "reason": "index disabled"}
    now = datetime.now(UTC)
    since = now - timedelta(hours=max_age_hours) if max_age_hours else None
    rows: list = []; offset = 0
    while True:
        page = store.workflows(WorkflowFilter(status=["running"], started_from=since, offset=offset, limit=RECONCILE_PAGE, order_by="started_at", desc=False))
        rows.extend(page.items); offset += len(page.items)
        if not page.items or offset >= page.total:
            break
    out: dict = {"checked": len(rows), "reconciled": 0, "unknown": 0, "still_running": 0, "errors": [], "rows": []}
    c = client if client is not None else (await connect() if rows else None)
    for row in rows:
        try:
            desc = await c.get_workflow_handle(row.workflow_id, run_id=row.run_id).describe()
        except RPCError as e:
            if e.status != RPCStatusCode.NOT_FOUND:
                out["errors"].append({"workflow_id": row.workflow_id, "run_id": row.run_id, "error": f"{e.status.name}: {str(e)[:200]}"})
                continue
            status, closed_at, failure, type_, queue = "unknown", now, UNKNOWN_FAILURE, row.type, row.task_queue
        else:
            name = desc.status.name if desc.status is not None else "RUNNING"
            if name == "RUNNING":
                out["still_running"] += 1
                continue
            status = TEMPORAL_STATUS.get(name)
            if status is None:
                out["errors"].append({"workflow_id": row.workflow_id, "run_id": row.run_id, "error": f"unmapped Temporal status {name}"})
                continue
            closed_at = getattr(desc, "close_time", None) or now
            failure = f"Temporal reports the execution {name.lower().replace('_', ' ')}" if status in ("failed", "terminated", "timed_out") else None
            type_ = getattr(desc, "workflow_type", None) or row.type; queue = getattr(desc, "task_queue", None) or row.task_queue
        payload = {"workflow_id": row.workflow_id, "run_id": row.run_id, "type": type_, "status": status, "params_summary": row.params_summary,
                   "failure": failure, "parent_workflow_id": row.parent_workflow_id, "started_at": row.started_at.isoformat(), "closed_at": closed_at.isoformat(),
                   "task_queue": queue, "cost_usd": str(row.cost_usd) if row.cost_usd is not None else None, "search_attributes": row.search_attributes,
                   "lifecycle_source": "temporal"}
        await asyncio.to_thread(index_workflow, payload, settings)
        out["unknown" if status == "unknown" else "reconciled"] += 1
        if len(out["rows"]) < RECONCILE_ROWS:
            out["rows"].append({"workflow_id": row.workflow_id, "run_id": row.run_id, "from": "running", "to": status})
    try:
        atomic_json(Layout(root=settings.workspace).reconciliation_checkpoint(),
                    {"at": now.isoformat(), "max_age_hours": max_age_hours, **{k: out[k] for k in ("checked", "reconciled", "unknown", "still_running")},
                     "errors": len(out["errors"]), "origin": settings.origin, "worker_image_digest": settings.worker_image_digest})
    except OSError:  # a read-only mount (the console) still gets the answer; the record keeps the last writable run
        pass
    return out


async def operator_action(params: dict, request_id: str) -> dict:
    """Submit one confirmation-bound mutation; repeat confirmation reads its original result."""
    import hashlib
    import json
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from swesim.workflows.operator_actions import OperatorActionWorkflow
    check_payload(params, "operator action input")
    c = await connect()
    wid = "operator-" + request_id
    binding = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    try:
        handle = await c.start_workflow(OperatorActionWorkflow.run, {**params, "request_id": request_id},
                                        id=wid, task_queue=queues.CONTROL, id_reuse_policy=REJECT_DUP,
                                        memo={"summary": {"action": params["action"]}, "request_sha256": binding},
                                        search_attributes=search_attributes.for_params(params))
    except WorkflowAlreadyStartedError:
        handle = c.get_workflow_handle(wid)
        memo = await (await handle.describe()).memo()
        if memo.get("request_sha256") != binding:
            raise ValueError("request ID already binds different operator parameters")
    import asyncio
    try:
        result = await asyncio.wait_for(handle.result(), timeout=15)
    except asyncio.TimeoutError:
        result = {"ok": True, "pending": True, "saved": False, "message": "Action queued. Its workflow tracks completion."}
    return {**result, "workflow_id": wid}
