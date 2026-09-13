"""Temporal worker: one `Worker` per task queue in `Settings.task_queues`, started once (systemd unit or Cloud Run
worker pool) and polling until stopped.

Contract
- Every workflow type is registered on every queue. Operator workflows start on `swesim-authoring`; children are
  started there too (see `workflows.py`), so a pool polling one queue still runs the workflow tasks routed to it.
- Activities are partitioned by `queues.QUEUE_OF`: a worker polling queue Q registers exactly the activities routed to
  Q, taken from `swesim.workflows.activities.ALL` plus `swesim.index.activities.ALL` and `swesim.audit.activities.ALL`
  when those modules import (a stream not merged yet must not keep the worker from starting). An activity with no
  queue is a configuration error and stops the worker at startup (`queues.UnroutedActivity`).
- Concurrency per queue is `Settings.max_concurrent_activities` when set, else `queues.CONCURRENCY[queue]`.
- Heartbeats are throttled to at most `HEARTBEAT_THROTTLE` (5 s). Activity cancellation reaches an activity only in a
  heartbeat response, and the SDK's default throttle under `act()`'s two-minute heartbeat timeout is 60 s: without this an
  operator's cancel could go unseen by a running stage for a minute (and a launch thread that finished first would not be
  cancelled at all).
- Configuration comes from `Settings` only; the startup line names the connection mode, the queues, the launcher and
  the worker image digest, never a secret. A pool that polls `swesim-trials` builds its launcher at startup so a
  misconfigured launcher (no image, no service account) fails the deploy instead of the first trial. The pool that
  polls `swesim-control` runs the index migration at startup when the index is enabled.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Callable

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from swesim.settings import Settings, get_settings
from swesim.workflows import activities as A
from swesim.workflows import queues
from swesim.workflows.client import connect, describe_connection
from swesim.workflows.workflows import (CandidateWorkflow, CreateTasksWorkflow, PipelineWorkflow, RunMatrixWorkflow, RunTasksWorkflow,
                                        TrialWorkflow)

from swesim.workflows.operator_actions import OperatorActionWorkflow

WORKFLOWS = [OperatorActionWorkflow, TrialWorkflow, RunMatrixWorkflow, PipelineWorkflow, CandidateWorkflow, CreateTasksWorkflow, RunTasksWorkflow]
# Activity modules other streams own; each is optional until its stream is merged.
OPTIONAL_ACTIVITY_MODULES = ("swesim.index.activities", "swesim.audit.activities", "swesim.workflows.operator_actions")
HEARTBEAT_THROTTLE = timedelta(seconds=5)  # bounds how long an activity cancellation can go unseen (see the contract)


def activity_name(fn: Callable) -> str:
    return activity._Definition.from_callable(fn).name


def optional_activities(modules: tuple[str, ...] = OPTIONAL_ACTIVITY_MODULES) -> list[Callable]:
    """`ALL` of every optional module that imports; a missing module contributes nothing."""
    import importlib
    out: list[Callable] = []
    for name in modules:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        out.extend(getattr(mod, "ALL", []))
    return out


def registered_activities() -> list[Callable]:
    """Every activity this worker can run: the pipeline's own plus the optional streams', deduplicated by name."""
    seen: dict[str, Callable] = {}
    for fn in [*A.ALL, *optional_activities()]:
        seen.setdefault(activity_name(fn), fn)
    return list(seen.values())


def activities_for_queue(queue: str, candidates: list[Callable] | None = None) -> list[Callable]:
    """The registered activities routed to `queue`; every candidate must have a queue (fail closed at startup)."""
    return [fn for fn in (candidates if candidates is not None else registered_activities()) if queues.queue_for(activity_name(fn)) == queue]


def concurrency(settings: Settings, queue: str) -> int:
    return settings.max_concurrent_activities or queues.CONCURRENCY[queue]


def startup_line(settings: Settings) -> str:
    """The one log line at start: connection mode, queues, launcher, workspace, image digest. Never a secret."""
    return (f"swesim worker: {describe_connection()} queues={','.join(settings.task_queues)} launcher={settings.trial_launcher} "
            f"workspace={settings.workspace} origin={settings.origin} worker_image_digest={settings.worker_image_digest or '-'}")


def worker_options(settings: Settings, queue: str) -> dict:
    """The per-queue `Worker` keyword options: concurrency and the heartbeat throttle."""
    return {"max_concurrent_activities": concurrency(settings, queue), "max_heartbeat_throttle_interval": HEARTBEAT_THROTTLE}


def build_workers(client: Client, settings: Settings) -> list[Worker]:
    candidates = registered_activities()
    return [Worker(client, task_queue=q, workflows=WORKFLOWS, activities=activities_for_queue(q, candidates), **worker_options(settings, q))
            for q in settings.task_queues]


def configure_logging() -> None:
    """INFO to stdout once per process (Cloud Logging reads the pool's stdout); a process that configured logging keeps its handlers."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def migrate_index_if_enabled(settings: Settings) -> dict:
    """The control pool runs the index migration at startup (`swesim.index.ensure_migrated`), so nobody reaches Cloud SQL from a
    laptop. Skipped when the index is disabled or this worker does not poll the control queue; reported when the storage stream's
    entry point is not merged yet."""
    if not settings.index_enabled:
        return {"migrated": False, "reason": "index disabled"}
    if queues.CONTROL not in settings.task_queues:
        return {"migrated": False, "reason": "not the control pool"}
    try:
        from swesim.index import ensure_migrated
    except ImportError:
        return {"migrated": False, "reason": "swesim.index.ensure_migrated is not available on this worker"}
    ensure_migrated(settings)
    return {"migrated": True}


def executor_size(settings: Settings) -> int:
    """Threads for `asyncio.to_thread` / `blocking()`: every concurrent activity may hold one for minutes (LLM calls, Modal
    builds, gh scans). Python's default is cpu_count + 4 (5 on a 1-vCPU Cloud Run instance), which queues blocking stages
    behind each other; a bare `to_thread` waiting in that queue sends no heartbeat and the attempt times out (measured
    2026-09-13 on discover_activity). Size the pool to the sum of the queues' concurrency plus headroom."""
    total = sum(concurrency(settings, q) for q in settings.task_queues)
    return max(8, total + 8)


async def run_workers(client: Client, settings: Settings, stop: asyncio.Event | None = None) -> None:
    """Run one worker per configured queue until `stop` is set (forever when None)."""
    configure_logging()
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=executor_size(settings), thread_name_prefix="swesim-stage"))
    A.configure(settings)
    if queues.TRIALS in settings.task_queues:
        A.launcher()  # fail closed now, not at the first trial
    if settings.trial_launcher != "systemd" and settings.trial_launcher != "cloudrun":
        print(f"WARNING: trial launcher {settings.trial_launcher!r} is for local smoke only; NOT production", file=sys.stderr, flush=True)
    migration = await asyncio.to_thread(migrate_index_if_enabled, settings)
    print(f"{startup_line(settings)} index_migration={migration}", flush=True)
    async with contextlib.AsyncExitStack() as stack:
        for worker in build_workers(client, settings):
            await stack.enter_async_context(worker)
        await (stop or asyncio.Event()).wait()


async def main(launcher: str | None = None, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    if launcher:
        s = s.model_copy(update={"trial_launcher": launcher})
    await run_workers(await connect(), s)


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m swesim.workflows.worker", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--launcher", choices=["systemd", "cloudrun", "subprocess"], default=None,
                        help="override SWESIM_TRIAL_LAUNCHER; subprocess is local smoke only, NOT production")
    args = parser.parse_args(argv)
    asyncio.run(main(args.launcher))


if __name__ == "__main__":
    cli()
