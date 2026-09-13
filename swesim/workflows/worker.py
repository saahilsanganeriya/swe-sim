"""Temporal worker: one process on task queue `swesim`, launched once by systemd, polling until stopped.

Environment (read here or by the activities; all optional except on the VM where systemd sets them):
  TEMPORAL_ADDRESS        default localhost:7233
  TEMPORAL_NAMESPACE      default "default"
  SWESIM_WORKSPACE        workspace root (default ./workspace); trial units receive the same value
  SWESIM_ENV_FILE         path of the python-dotenv file trial units load for provider keys and the worker secret
  SWESIM_TRIAL_LAUNCHER   systemd (default, production) | subprocess (local smoke only, NOT production)
  SWESIM_TRIAL_PYTHON     interpreter for trial units (default: this process's sys.executable)
  SWESIM_TRIAL_POLL_SEC   poll interval of await_trial_activity (default 10)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from temporalio.client import Client
from temporalio.worker import Worker

from swesim.workflows import activities as A
from swesim.workflows.workflows import PipelineWorkflow, RunMatrixWorkflow, TrialWorkflow

TASK_QUEUE = "swesim"
MAX_CONCURRENT_ACTIVITIES = 4  # light activities; trials are serialized by the workflows and LAUNCH_SLOTS
WORKFLOWS = [TrialWorkflow, RunMatrixWorkflow, PipelineWorkflow]


def address() -> str:
    return os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")


def namespace() -> str:
    return os.environ.get("TEMPORAL_NAMESPACE", "default")


async def main(launcher: str | None = None) -> None:
    A.LAUNCHER = launcher or os.environ.get("SWESIM_TRIAL_LAUNCHER", A.LAUNCHER)
    if A.LAUNCHER != "systemd":
        print(f"WARNING: trial launcher {A.LAUNCHER!r} is for local smoke only; NOT production", file=sys.stderr, flush=True)
    client = await Client.connect(address(), namespace=namespace())
    print(f"swesim worker: temporal={address()} namespace={namespace()} queue={TASK_QUEUE} launcher={A.LAUNCHER} workspace={A.WS.root}", flush=True)
    async with Worker(client, task_queue=TASK_QUEUE, workflows=WORKFLOWS, activities=A.ALL, max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES):
        await asyncio.Event().wait()


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m swesim.workflows.worker", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--launcher", choices=["systemd", "subprocess"], default=None, help="trial launcher; subprocess is local smoke only, NOT production")
    args = parser.parse_args(argv)
    asyncio.run(main(args.launcher))


if __name__ == "__main__":
    cli()
