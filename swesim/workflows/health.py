"""Read-only Temporal Cloud queue observations, shared by runtime verification and the console.

API-key authentication is mandatory. Five bounded, concurrent DescribeTaskQueue calls
observe four activity queues and the authoring workflow queue. Only pollers with a
recent last-access timestamp count; identities and provider error text never leave
this module. Polling is evidence of queue consumption, not GCP resource readiness,
backlog, available capacity, or successful task execution.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client

from swesim.settings import MissingSecret, Settings, secret
from swesim.workflows.queues import AUTHORING, BUILD, CONTROL, TRIALS

CONNECT_TIMEOUT_SECONDS = 3.0
RPC_TIMEOUT_SECONDS = 2.0
RECENT_POLLER_SECONDS = 120
QUEUES = ((AUTHORING, "activity"), (BUILD, "activity"), (TRIALS, "activity"), (CONTROL, "activity"), (AUTHORING, "workflow"))


def _stamp(value: Any) -> datetime | None:
    if hasattr(value, "ToDatetime"):
        value = value.ToDatetime(tzinfo=UTC)
    return value.astimezone(UTC) if isinstance(value, datetime) and value.tzinfo is not None else None


def _unavailable(queue: str, kind: str, checked: str, error_class: str | None) -> dict:
    return {"queue": queue, "kind": kind, "state": "unavailable", "recent_pollers": None, "reported_pollers": None,
            "unknown_timestamps": None, "last_polled_at": None, "observed_at": checked, "error_class": error_class}


async def check_temporal(settings: Settings, client: Any = None, *, now: datetime | None = None) -> dict:
    """Return credential-free observations in at most connect timeout + RPC timeout.

`client` and `now` are injectable for offline verification. Successful RPCs with no
recent pollers are degraded; absent authentication and failed RPCs are unavailable.
"""
    observed = now or datetime.now(UTC)
    checked = observed.isoformat()
    result = {"passed": False, "state": "unavailable", "auth": "unavailable", "checked_at": checked,
              "recent_within_seconds": RECENT_POLLER_SECONDS, "resource_status": "not_measured", "resource_checked_at": None,
              "namespace": settings.temporal_namespace, "pollers": {}, "queues": [], "error_class": None, "reason": None}

    def failed(reason: str, error_class: str | None = None) -> dict:
        return {**result, "reason": reason, "error_class": error_class,
                "queues": [_unavailable(q, kind, checked, error_class) for q, kind in QUEUES]}

    if not settings.temporal_cloud:
        return failed("Temporal address is not a Cloud endpoint")
    try:
        key = secret("TEMPORAL_API_KEY", settings)
    except MissingSecret:
        return failed("Temporal Cloud API key is missing", "MissingSecret")
    result["auth"] = "api-key"
    if client is None:
        try:
            client = await asyncio.wait_for(Client.connect(target_host=settings.temporal_address, namespace=settings.temporal_namespace,
                                                           api_key=key, tls=True, rpc_metadata={"temporal-namespace": settings.temporal_namespace}),
                                            CONNECT_TIMEOUT_SECONDS)
        except Exception as error:  # SDK/transport boundary: error messages may contain credentials
            return failed("Temporal connection unavailable", type(error).__name__)

    async def describe(queue: str, kind: str) -> dict:
        queue_type = TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY if kind == "activity" else TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW
        try:
            response = await asyncio.wait_for(client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
                namespace=settings.temporal_namespace, task_queue=TaskQueue(name=queue), task_queue_type=queue_type)), RPC_TIMEOUT_SECONDS)
            pollers = list(response.pollers)
            times = [_stamp(getattr(p, "last_access_time", None)) for p in pollers]
            known = [t for t in times if t is not None and t.year > 1970]
            current = now or datetime.now(UTC)
            recent = sum(-30 <= (current - t).total_seconds() <= RECENT_POLLER_SECONDS for t in known)
            return {"queue": queue, "kind": kind, "state": "polling" if recent else "no_recent_pollers", "recent_pollers": recent,
                    "reported_pollers": len(pollers), "unknown_timestamps": len(pollers) - len(known),
                    "last_polled_at": max(known).isoformat() if known else None, "observed_at": current.isoformat(), "error_class": None}
        except Exception as error:  # protobuf, SDK and transport errors are sanitized per queue
            return _unavailable(queue, kind, (now or datetime.now(UTC)).isoformat(), type(error).__name__)

    observations = await asyncio.gather(*(describe(q, kind) for q, kind in QUEUES))
    result["queues"] = observations
    result["pollers"] = {f"{row['queue']}/{row['kind']}": row["recent_pollers"] for row in observations}
    result["passed"] = all(row["state"] == "polling" for row in observations)
    result["state"] = "ok" if result["passed"] else "unavailable" if all(row["state"] == "unavailable" for row in observations) else "degraded"
    result["checked_at"] = (now or datetime.now(UTC)).isoformat()
    return result
