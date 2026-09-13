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
- Configuration comes from `Settings` only; the startup line names the connection mode, the queues, the launcher and
  the worker image digest, never a secret. A pool that polls `swesim-trials` builds its launcher at startup so a
  misconfigured launcher (no image, no service account) fails the deploy instead of the first trial.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
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

WORKFLOWS = [TrialWorkflow, RunMatrixWorkflow, PipelineWorkflow, CandidateWorkflow, CreateTasksWorkflow, RunTasksWorkflow]
# Activity modules other streams own; each is optional until its stream is merged.
OPTIONAL_ACTIVITY_MODULES = ("swesim.index.activities", "swesim.audit.activities")


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


def build_workers(client: Client, settings: Settings) -> list[Worker]:
    candidates = registered_activities()
    return [Worker(client, task_queue=q, workflows=WORKFLOWS, activities=activities_for_queue(q, candidates),
                   max_concurrent_activities=concurrency(settings, q)) for q in settings.task_queues]


async def run_workers(client: Client, settings: Settings, stop: asyncio.Event | None = None) -> None:
    """Run one worker per configured queue until `stop` is set (forever when None)."""
    A.configure(settings)
    if queues.TRIALS in settings.task_queues:
        A.launcher()  # fail closed now, not at the first trial
    if settings.trial_launcher != "systemd" and settings.trial_launcher != "cloudrun":
        print(f"WARNING: trial launcher {settings.trial_launcher!r} is for local smoke only; NOT production", file=sys.stderr, flush=True)
    print(startup_line(settings), flush=True)
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
