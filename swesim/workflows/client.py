"""Thin Temporal client helpers used by the CLI (`pipeline`, `approve-signal`, `matrix`)."""
from __future__ import annotations

import os
import uuid

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from swesim.workflows.workflows import PipelineWorkflow, RunMatrixWorkflow

TASK_QUEUE = "swesim"


async def connect() -> Client:
    return await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"), namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"))


async def start_pipeline(params: dict) -> str:
    c = await connect(); wid = "pipeline-" + uuid.uuid4().hex
    await c.start_workflow(PipelineWorkflow.run, params, id=wid, task_queue=TASK_QUEUE, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
    return wid


async def approve(workflow_id: str, scenario_id: str, spec_hash: str) -> None:
    c = await connect()
    await c.get_workflow_handle(workflow_id).signal(PipelineWorkflow.approve_spec, {"scenario_id": scenario_id, "spec_hash": spec_hash})


async def progress(workflow_id: str) -> dict:
    c = await connect()
    return await c.get_workflow_handle(workflow_id).query(PipelineWorkflow.progress)


async def start_matrix(params: dict) -> str:
    c = await connect(); wid = "matrix-" + uuid.uuid4().hex
    await c.start_workflow(RunMatrixWorkflow.run, params, id=wid, task_queue=TASK_QUEUE, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
    return wid
