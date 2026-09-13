"""Thin Temporal client helpers used by the CLI and the console. No secrets in payloads or memos."""
from __future__ import annotations

import os
from pathlib import Path
import uuid

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from swesim.workflows.workflows import CandidateWorkflow, CreateTasksWorkflow, PipelineWorkflow, RunMatrixWorkflow, RunTasksWorkflow

TASK_QUEUE = "swesim"
TYPES = {"create": "CreateTasksWorkflow", "candidate": "CandidateWorkflow", "run": "RunTasksWorkflow",
         "pipeline": "PipelineWorkflow", "matrix": "RunMatrixWorkflow", "trial": "TrialWorkflow"}
REJECT_DUP = WorkflowIDReusePolicy.REJECT_DUPLICATE


def connect_kwargs(env: dict | None = None) -> dict:
    """Connection settings from the environment. Local dev server by default; Temporal Cloud with `TEMPORAL_API_KEY` (TLS on,
    address like `us-central1.gcp.api.temporal.io:7233`, namespace like `swesim.abcde`) or mTLS with `TEMPORAL_TLS_CERT` /
    `TEMPORAL_TLS_KEY` (PEM file paths). The key value never appears in logs: `describe_connection` reports only the mode."""
    e = os.environ if env is None else env
    kw: dict = {"target_host": e.get("TEMPORAL_ADDRESS", "localhost:7233"), "namespace": e.get("TEMPORAL_NAMESPACE", "default")}
    if e.get("TEMPORAL_API_KEY"):
        kw["api_key"] = e["TEMPORAL_API_KEY"]; kw["tls"] = True
    elif e.get("TEMPORAL_TLS_CERT") and e.get("TEMPORAL_TLS_KEY"):
        from temporalio.service import TLSConfig
        kw["tls"] = TLSConfig(client_cert=Path(e["TEMPORAL_TLS_CERT"]).read_bytes(), client_private_key=Path(e["TEMPORAL_TLS_KEY"]).read_bytes())
    return kw


def describe_connection(env: dict | None = None) -> str:
    kw = connect_kwargs(env)
    mode = "api-key" if "api_key" in kw else "mtls" if kw.get("tls") else "plain"
    return f"temporal={kw['target_host']} namespace={kw['namespace']} auth={mode}"


async def connect() -> Client:
    return await Client.connect(**connect_kwargs())


def _summary(params: dict) -> dict:
    """Small, secret-free memo shown by `list_runs` (ids, model ids, counts only)."""
    out: dict = {}
    if "sources" in params:
        out["sources"] = [s.get("repo") or f"{s.get('kind', 'swebench-annotated')}:{s.get('pool', 'A')}" for s in params.get("sources", [])]
    for k in ("scenario_ids", "models", "conditions", "k", "judge_model", "draft_model", "stakeholder_model", "approval_mode", "purpose", "source", "repo"):
        if params.get(k) not in (None, "", []):
            out[k] = params[k]
    return out


async def _start(workflow_run, params: dict, prefix: str) -> str:
    c = await connect(); wid = f"{prefix}-{uuid.uuid4().hex}"
    await c.start_workflow(workflow_run, params, id=wid, task_queue=TASK_QUEUE, id_reuse_policy=REJECT_DUP, memo={"summary": _summary(params)})
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
    """Request cancellation; children are cancelled with the parent. A cancelled trial's systemd unit keeps running on the
    VM and finishes on its own, but no workflow collects it afterwards (its trial id is consumed); a later run uses a new
    attempt index and is a fresh launch, never a relaunch of the cancelled one."""
    c = await connect()
    await c.get_workflow_handle(workflow_id).cancel()


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
