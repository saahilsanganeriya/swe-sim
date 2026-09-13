"""Cloud Run Jobs launcher: one Job per trial, executed once; the Job name is the launch-once guard (brief §2.2, interfaces §3).

Contract
- `start` creates Job `swesim-trial-<tid>` in the worker's project and region (task_count 1, parallelism 1, max_retries 0,
  timeout = the request's solver_timeout_sec + 2 h, labels swesim-trial-id / swesim-scenario-id / swesim-run-workflow-id,
  the worker image, the worker service account, env from `Settings`, secrets as Secret Manager references by name at
  `latest`, the record bucket mounted read-write at /workspace, the Cloud SQL socket volume when a connection name is
  configured) and then runs it exactly once. `AlreadyExists` on create means an earlier start reached the API: the trial
  counts as launched and nothing runs again (the acknowledgement says so). launch.json is written by `launch_once`
  before `start` is called, so a lost acknowledgement is reconciled, never relaunched.
- `runtime_state` reads the Job's latest execution: succeeded_count -> succeeded; failed_count or cancelled_count, or a
  Completed condition in state FAILED -> failed; an execution still open -> running; no Job or no execution -> absent.
  Any other API error raises `JobStateUnavailable` (retryable); the state is never guessed.
- `cancel` cancels the latest open execution; `cleanup` deletes the Job (call it once the record exists: logs stay in
  Cloud Logging, the record and launch.json are the evidence). Both tolerate NotFound: they are idempotent.
- The Job runs `python -m swesim.workflows.activities run-trial /workspace/trials/<tid>/request.json`: the worker's
  request path re-rooted under the mount, so the worker and the Job name the same bucket object.
- Configuration: `Settings` supplies project, region, bucket, digest, the image reference (`worker_image`), the service
  account (`worker_service_account`) and the trial env; `from_settings` fails closed with `LauncherConfigError` when the
  image, the service account, the project or the bucket is missing. The Cloud SQL connection name is not a Settings
  field yet (`SWESIM_SQL_CONNECTION_NAME` requested from the integrator): `from_settings` reads a Settings attribute of
  that name when present, else `SQL_CONNECTION_NAME` below; None means no Cloud SQL volume (a trial does not index).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from google.api_core import exceptions as gexc
from google.cloud import run_v2

from swesim.settings import Settings
from swesim.storage import layout as storage_layout
from swesim.workflows.launchers.base import LaunchAck, LauncherConfigError, RuntimeState, RuntimeStateUnavailable, read_json, unit_name

LABEL_TRIAL, LABEL_SCENARIO, LABEL_RUN = "swesim-trial-id", "swesim-scenario-id", "swesim-run-workflow-id"
WORKSPACE_VOLUME, WORKSPACE_MOUNT = "swesim-workspace", "/workspace"
CLOUDSQL_VOLUME, CLOUDSQL_MOUNT = "cloudsql", "/cloudsql"
# Secret Manager secrets a trial needs (provider keys for the solver and the stakeholder client, Modal, the worker HMAC).
JOB_SECRETS = ("SWESIM_WORKER_SECRET", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET")
EXTRA_TIMEOUT = timedelta(hours=2)
DEFAULT_SOLVER_TIMEOUT_SEC = 2400
GCS_MOUNT_OPTIONS = json.loads(Path(storage_layout.__file__).with_name("mount_options.json").read_text())
CREATE_TIMEOUT_SEC = 120
RESOURCES = {"cpu": "1", "memory": "2Gi"}  # Harbor is a client; the heavy work runs on Modal
# Not a Settings field yet (asked of the integrator, see the stream report): read here until then.
SQL_CONNECTION_NAME: str | None = None
_LABEL_RE = re.compile(r"[^a-z0-9_-]")


class JobStateUnavailable(RuntimeStateUnavailable):
    """The Cloud Run API did not answer; the execution's state is unknown, not absent."""


def label_value(value: str | None) -> str:
    """A GCP label value: lowercase letters, digits, `_` and `-`, at most 63 characters; empty when unknown."""
    return _LABEL_RE.sub("-", str(value or "").lower())[:63]


def execution_state(ex: Any) -> tuple[RuntimeState, str]:
    """Execution counts and conditions -> protocol state; the detail names the execution and its counts."""
    detail = f"{ex.name} running={ex.running_count} succeeded={ex.succeeded_count} failed={ex.failed_count} cancelled={ex.cancelled_count}"
    if ex.succeeded_count >= 1:
        return "succeeded", detail
    if ex.failed_count >= 1 or ex.cancelled_count >= 1:
        return "failed", detail
    completed = next((c for c in ex.conditions if c.type_ == "Completed"), None)
    if completed is not None and completed.state == run_v2.Condition.State.CONDITION_FAILED:
        return "failed", f"{detail} Completed=FAILED {completed.message}"
    if completed is not None and completed.state == run_v2.Condition.State.CONDITION_SUCCEEDED:
        return "succeeded", detail
    return "running", detail


def trial_env(s: Settings) -> list[run_v2.EnvVar]:
    """The Job's environment: the interfaces §9 names with values from Settings, plus Secret Manager references for `JOB_SECRETS`.
    No secret value ever enters the spec."""
    values = {
        "SWESIM_WORKSPACE": WORKSPACE_MOUNT, "SWESIM_REGISTRY": f"{WORKSPACE_MOUNT}/registry.json", "SWESIM_BUCKET": s.bucket,
        "SWESIM_TRIAL_LAUNCHER": "cloudrun", "SWESIM_ORIGIN": "cloudrun", "SWESIM_WORKER_IMAGE_DIGEST": s.worker_image_digest,
        "SWESIM_GCP_PROJECT": s.gcp_project, "SWESIM_GCP_REGION": s.gcp_region,
        "SWESIM_SPEND_CAP_USD": str(s.spend_cap_usd) if s.spend_cap_usd is not None else None,
        "SWESIM_STAKEHOLDER_URL": s.stakeholder_url or None, "SWESIM_CONSOLE_URL": s.console_url,
        "TEMPORAL_ADDRESS": s.temporal_address, "TEMPORAL_NAMESPACE": s.temporal_namespace, "PYTHONUNBUFFERED": "1",
    }
    env = [run_v2.EnvVar(name=k, value=v) for k, v in values.items() if v]
    env += [run_v2.EnvVar(name=k, value_source=run_v2.EnvVarSource(secret_key_ref=run_v2.SecretKeySelector(secret=k, version="latest")))
            for k in JOB_SECRETS]
    return env


@dataclass
class CloudRunLauncher:
    settings: Settings
    image: str
    service_account: str
    sql_connection_name: str | None = None
    jobs: Any = None          # run_v2.JobsClient; tests pass a fake
    executions: Any = None    # run_v2.ExecutionsClient; tests pass a fake
    name: str = field(default="cloudrun", init=False)

    @classmethod
    def from_settings(cls, s: Settings, *, jobs: Any = None, executions: Any = None) -> "CloudRunLauncher":
        sql = getattr(s, "sql_connection_name", None) or SQL_CONNECTION_NAME
        missing = [n for n, v in (("SWESIM_WORKER_IMAGE", s.worker_image), ("SWESIM_WORKER_SERVICE_ACCOUNT", s.worker_service_account),
                                  ("SWESIM_GCP_PROJECT", s.gcp_project), ("SWESIM_BUCKET", s.bucket)) if not v]
        if missing:
            raise LauncherConfigError(f"cloudrun launcher cannot start trials without {', '.join(missing)}")
        return cls(settings=s, image=s.worker_image, service_account=s.worker_service_account, sql_connection_name=sql, jobs=jobs, executions=executions)

    def _jobs(self) -> Any:
        if self.jobs is None:
            self.jobs = run_v2.JobsClient()
        return self.jobs

    def _executions(self) -> Any:
        if self.executions is None:
            self.executions = run_v2.ExecutionsClient()
        return self.executions

    @property
    def parent(self) -> str:
        return f"projects/{self.settings.gcp_project}/locations/{self.settings.gcp_region}"

    def job_name(self, trial_id: str) -> str:
        return f"{self.parent}/jobs/{unit_name(trial_id)}"

    def job_request_path(self, request_path: Path) -> str:
        """The worker's request path as the Job sees it (the bucket is mounted at /workspace on both)."""
        try:
            rel = Path(request_path).resolve().relative_to(Path(self.settings.workspace).resolve())
        except ValueError as e:
            raise LauncherConfigError(f"{request_path} is not under the workspace {self.settings.workspace}; the Job mounts the bucket at {WORKSPACE_MOUNT}") from e
        return f"{WORKSPACE_MOUNT}/{rel.as_posix()}"

    def job_spec(self, trial_id: str, request_path: Path, request: dict) -> run_v2.Job:
        solver_timeout = int((request.get("run_config") or {}).get("solver_timeout_sec") or DEFAULT_SOLVER_TIMEOUT_SEC)
        labels = {LABEL_TRIAL: label_value(trial_id), LABEL_SCENARIO: label_value(request.get("scenario_id")), LABEL_RUN: label_value(request.get("run_workflow_id"))}
        mounts = [run_v2.VolumeMount(name=WORKSPACE_VOLUME, mount_path=WORKSPACE_MOUNT)]
        volumes = [run_v2.Volume(name=WORKSPACE_VOLUME, gcs=run_v2.GCSVolumeSource(
            bucket=self.settings.bucket, read_only=False,
                                                        mount_options=GCS_MOUNT_OPTIONS))]
        if self.sql_connection_name:
            mounts.append(run_v2.VolumeMount(name=CLOUDSQL_VOLUME, mount_path=CLOUDSQL_MOUNT))
            volumes.append(run_v2.Volume(name=CLOUDSQL_VOLUME, cloud_sql_instance=run_v2.CloudSqlInstance(instances=[self.sql_connection_name])))
        container = run_v2.Container(name="trial", image=self.image, env=trial_env(self.settings), volume_mounts=mounts,
                                     command=["python", "-m", "swesim.workflows.activities", "run-trial", self.job_request_path(request_path)],
                                     resources=run_v2.ResourceRequirements(limits=dict(RESOURCES)))
        task = run_v2.TaskTemplate(containers=[container], volumes=volumes, max_retries=0, timeout=timedelta(seconds=solver_timeout) + EXTRA_TIMEOUT,
                                   service_account=self.service_account)
        return run_v2.Job(labels=labels, template=run_v2.ExecutionTemplate(task_count=1, parallelism=1, labels=labels, template=task))

    def start(self, trial_id: str, request_path: Path, intent: dict) -> LaunchAck:
        name = self.job_name(trial_id); intent["job"] = name
        try:
            self._jobs().create_job(parent=self.parent, job=self.job_spec(trial_id, request_path, read_json(Path(request_path))),
                                    job_id=unit_name(trial_id)).result(timeout=CREATE_TIMEOUT_SEC)
        except gexc.AlreadyExists:
            # The name is the launch-once guard: an earlier start reached the API; whatever it ran is the trial's runtime.
            return LaunchAck(0, "", f"job {name} already exists: already launched; not run again", name)
        op = self._jobs().run_job(name=name)
        handle = getattr(getattr(op, "metadata", None), "name", None) or name
        return LaunchAck(0, "", f"execution {handle}", str(handle))

    def latest_execution(self, trial_id: str) -> Any | None:
        try:
            execs = list(self._executions().list_executions(parent=self.job_name(trial_id)))
        except gexc.NotFound:
            return None
        except gexc.GoogleAPICallError as e:
            raise JobStateUnavailable(f"list_executions for {unit_name(trial_id)} failed: {e}") from e
        if not execs:
            return None
        execs.sort(key=lambda e: e.create_time.timestamp() if getattr(e, "create_time", None) else 0.0)
        return execs[-1]

    def runtime_state(self, trial_id: str, launch: dict) -> tuple[RuntimeState, str]:
        ex = self.latest_execution(trial_id)
        if ex is None:
            return "absent", f"no execution of {unit_name(trial_id)}"
        return execution_state(ex)

    def cancel(self, trial_id: str, launch: dict) -> None:
        ex = self.latest_execution(trial_id)
        if ex is None or execution_state(ex)[0] != "running":
            return None
        try:
            self._executions().cancel_execution(name=ex.name)
        except gexc.NotFound:
            return None

    def cleanup(self, trial_id: str, launch: dict) -> None:
        try:
            self._jobs().delete_job(name=self.job_name(trial_id))
        except gexc.NotFound:
            return None
