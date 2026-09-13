"""Check the Temporal Cloud connection and live queue pollers, without printing credentials."""
from __future__ import annotations

import asyncio
import json

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest

from swesim.settings import MissingSecret, Settings, get_settings, secret
from swesim.workflows.client import connect
from swesim.workflows.queues import AUTHORING


async def check_temporal(settings: Settings, client=None) -> dict:
    """Cloud API-key authentication and a recent poller on every activity queue and the workflow queue are required."""
    if not settings.temporal_cloud:
        return {"passed": False, "reason": "Temporal address is not a Cloud endpoint"}
    try:
        secret("TEMPORAL_API_KEY", settings)
    except MissingSecret:
        return {"passed": False, "reason": "Temporal Cloud API key is missing"}
    if client is None:
        client = await asyncio.wait_for(connect(), 30)
    queues = [(q, TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY) for q in settings.task_queues]
    queues.append((AUTHORING, TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW))

    async def pollers(queue: str, queue_type: int) -> tuple[str, int]:
        response = await asyncio.wait_for(client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
            namespace=settings.temporal_namespace, task_queue=TaskQueue(name=queue), task_queue_type=queue_type)), 20)
        label = "activity" if queue_type == TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY else "workflow"
        return f"{queue}/{label}", len(response.pollers)

    counts = dict(await asyncio.gather(*(pollers(q, t) for q, t in queues)))
    return {"passed": all(counts.values()), "auth": "api-key", "endpoint": settings.temporal_address,
            "namespace": settings.temporal_namespace, "pollers": counts}


def main() -> int:
    try:
        result = asyncio.run(check_temporal(get_settings()))
    except Exception as error:  # verification boundary: provider errors may contain credentials; report only their type
        result = {"passed": False, "error_class": type(error).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
