"""Offline tests for the Cloud Run Jobs launcher over fake Jobs/Executions clients (no network, no credentials).

Asserted behaviour: the Job spec the brief fixes (name, labels, counts, timeout, image, service account, env names,
Secret Manager references, volumes, command); create -> run exactly once; 409 on create -> already launched, no second
run; execution states -> runtime_state; cancel and cleanup are idempotent; configuration fails closed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from google.api_core import exceptions as gexc
from google.cloud import run_v2

from swesim.models import RunConfig, ScenarioSpec, minimal_spec_dict, trial_id
from swesim.paths import atomic_json
from swesim.settings import Settings
from swesim.workflows.launchers import base as B
from swesim.workflows.launchers import cloudrun as CR


class FakeOp:
    def __init__(self, result=None, metadata=None):
        self._result = result; self.metadata = metadata

    def result(self, timeout=None):
        return self._result


class FakeCloudRun:
    """Jobs + Executions clients over one dict of jobs; every call is recorded."""

    def __init__(self):
        self.jobs: dict[str, run_v2.Job] = {}; self.executions: dict[str, list[run_v2.Execution]] = {}
        self.created: list[str] = []; self.runs: list[str] = []; self.deleted: list[str] = []; self.cancelled: list[str] = []
        self.fail_list: Exception | None = None

    # JobsClient
    def create_job(self, *, parent, job, job_id):
        name = f"{parent}/jobs/{job_id}"
        if name in self.jobs: raise gexc.AlreadyExists(f"{name} exists")
        self.jobs[name] = job; self.created.append(name); return FakeOp(result=job)

    def run_job(self, *, name):
        if name not in self.jobs: raise gexc.NotFound(name)
        self.runs.append(name); n = len(self.executions.get(name, [])) + 1
        ex = run_v2.Execution(name=f"{name}/executions/{name.rsplit('/', 1)[1]}-{n}", running_count=1,
                              create_time=datetime(2026, 9, 13, 8, 0, n, tzinfo=timezone.utc),
                              conditions=[run_v2.Condition(type_="Completed", state=run_v2.Condition.State.CONDITION_PENDING)])
        self.executions.setdefault(name, []).append(ex); return FakeOp(metadata=ex)

    def delete_job(self, *, name):
        if name not in self.jobs: raise gexc.NotFound(name)
        del self.jobs[name]; self.executions.pop(name, None); self.deleted.append(name); return FakeOp()

    # ExecutionsClient
    def list_executions(self, *, parent):
        if self.fail_list: raise self.fail_list
        if parent not in self.jobs: raise gexc.NotFound(parent)
        return list(self.executions.get(parent, []))

    def cancel_execution(self, *, name):
        self.cancelled.append(name); return FakeOp()

    # test helpers
    def finish(self, job_name, *, succeeded=True):
        ex = self.executions[job_name][-1]
        ex.running_count = 0
        if succeeded: ex.succeeded_count = 1
        else: ex.failed_count = 1
        ex.conditions[0].state = run_v2.Condition.State.CONDITION_SUCCEEDED if succeeded else run_v2.Condition.State.CONDITION_FAILED


def settings(tmp_path, **over) -> Settings:
    env = {"SWESIM_WORKSPACE": str(tmp_path / "workspace"), "SWESIM_REGISTRY": str(tmp_path / "workspace" / "registry.json"),
           "SWESIM_BUCKET": "swesim-proj-workspace", "SWESIM_GCP_PROJECT": "proj", "SWESIM_GCP_REGION": "us-central1",
           "SWESIM_TRIAL_LAUNCHER": "cloudrun", "SWESIM_ORIGIN": "cloudrun", "SWESIM_WORKER_IMAGE_DIGEST": "sha256:" + "a" * 64,
           "SWESIM_STAKEHOLDER_URL": "https://stake.example", "SWESIM_SPEND_CAP_USD": "1500", "TEMPORAL_ADDRESS": "us-central1.gcp.api.temporal.io:7233",
           "TEMPORAL_NAMESPACE": "swesim.abcde", "SWESIM_ENV_FILE": str(tmp_path / "absent.env"), "ANTHROPIC_API_KEY": "fake-anthropic-value-123456",
           "SWESIM_WORKER_SECRET": "fake-worker-secret-xyz", "SWESIM_WORKER_IMAGE": "us-central1-docker.pkg.dev/proj/swesim/worker@sha256:" + "a" * 64,
           "SWESIM_WORKER_SERVICE_ACCOUNT": "swesim-worker@proj.iam.gserviceaccount.com", **over}
    return Settings.from_env({k: v for k, v in env.items() if v is not None})


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    fake = FakeCloudRun(); s = settings(tmp_path)
    monkeypatch.setattr(CR, "SQL_CONNECTION_NAME", "proj:us-central1:swesim")
    launcher = CR.CloudRunLauncher.from_settings(s, jobs=fake, executions=fake)
    return fake, launcher, B.WorkspaceLayout(s.workspace), s


def request(run_workflow_id="run-abc123", solver_timeout=2400):
    spec = ScenarioSpec.model_validate(minimal_spec_dict())
    rc = RunConfig(condition="hidden", model="openrouter/deepseek/deepseek-v3.2", compiled_hash="c" * 64, brief_profile="natural", solver_timeout_sec=solver_timeout)
    tid = trial_id(spec.spec_hash, "hidden", rc.run_config_hash, 0)
    return dict(trial_id=tid, scenario_id=spec.id, spec_hash=spec.spec_hash, condition="hidden", model=rc.model, run_config=rc.model_dump(mode="json"),
                run_config_hash=rc.run_config_hash, attempt_index=0, stakeholder_url="https://stake.example", run_workflow_id=run_workflow_id)


def test_from_settings_fails_closed_without_image_service_account_project_or_bucket(tmp_path, monkeypatch):
    with pytest.raises(B.LauncherConfigError, match="SWESIM_WORKER_IMAGE.*SWESIM_WORKER_SERVICE_ACCOUNT"):
        CR.CloudRunLauncher.from_settings(settings(tmp_path, SWESIM_WORKER_IMAGE=None, SWESIM_WORKER_SERVICE_ACCOUNT=None))
    with pytest.raises(B.LauncherConfigError, match="SWESIM_GCP_PROJECT"):
        CR.CloudRunLauncher.from_settings(settings(tmp_path, SWESIM_GCP_PROJECT=None))
    with pytest.raises(B.LauncherConfigError, match="SWESIM_BUCKET"):
        CR.CloudRunLauncher.from_settings(settings(tmp_path, SWESIM_BUCKET=None))
    launcher = B.get_launcher(settings(tmp_path))
    assert isinstance(launcher, CR.CloudRunLauncher) and launcher.name == "cloudrun" and launcher.sql_connection_name is None
    assert issubclass(B.LauncherConfigError, ValueError) and issubclass(CR.JobStateUnavailable, RuntimeError)


def test_start_creates_the_job_spec_the_brief_fixes_and_runs_it_once(cloud):
    fake, launcher, layout, s = cloud; p = request()
    out = B.launch_once(layout, launcher, p, image_digest=s.worker_image_digest, origin="cloudrun")
    job_name = f"projects/proj/locations/us-central1/jobs/swesim-trial-{p['trial_id']}"
    assert out["launched"] is True and out["launcher"] == "cloudrun" and out["handle"] == f"{job_name}/executions/swesim-trial-{p['trial_id']}-1"
    assert fake.created == [job_name] and fake.runs == [job_name]
    job = fake.jobs[job_name]; task = job.template.template; container = task.containers[0]
    assert dict(job.labels) == {"swesim-trial-id": p["trial_id"], "swesim-scenario-id": "example-scenario", "swesim-run-workflow-id": "run-abc123"}
    assert job.template.task_count == 1 and job.template.parallelism == 1 and task.max_retries == 0
    assert task.timeout == timedelta(seconds=2400 + 7200) and task.service_account == "swesim-worker@proj.iam.gserviceaccount.com"
    assert container.image == "us-central1-docker.pkg.dev/proj/swesim/worker@sha256:" + "a" * 64
    assert list(container.command) == ["python", "-m", "swesim.workflows.activities", "run-trial", f"/workspace/trials/{p['trial_id']}/request.json"]
    assert dict(container.resources.limits) == {"cpu": "1", "memory": "2Gi"}
    env = {e.name: e for e in container.env}
    assert env["SWESIM_WORKSPACE"].value == "/workspace" and env["SWESIM_REGISTRY"].value == "/workspace/registry.json" and env["SWESIM_BUCKET"].value == "swesim-proj-workspace"
    assert env["SWESIM_TRIAL_LAUNCHER"].value == "cloudrun" and env["SWESIM_ORIGIN"].value == "cloudrun" and env["SWESIM_WORKER_IMAGE_DIGEST"].value == s.worker_image_digest
    assert env["SWESIM_GCP_PROJECT"].value == "proj" and env["SWESIM_SPEND_CAP_USD"].value == "1500" and env["TEMPORAL_NAMESPACE"].value == "swesim.abcde"
    for name in CR.JOB_SECRETS:
        assert env[name].value == "" and env[name].value_source.secret_key_ref.secret == name and env[name].value_source.secret_key_ref.version == "latest"
    spec_text = run_v2.Job.to_json(job)
    assert "fake-anthropic-value-123456" not in spec_text and "fake-worker-secret-xyz" not in spec_text   # secret values never enter the spec
    volumes = {v.name: v for v in task.volumes}; mounts = {m.name: m.mount_path for m in container.volume_mounts}
    assert volumes["swesim-workspace"].gcs.bucket == "swesim-proj-workspace" and volumes["swesim-workspace"].gcs.read_only is False
    # The worker compiler and trial verifier hash file modes; their FUSE views must agree.
    terraform = Path(__file__).parents[1] / "infra/modules/run/main.tf"
    source = re.search(r'gcs_mount_options\s*=\s*jsondecode\(file\("\$\{path.module\}([^"\n]+)"\)\)', terraform.read_text())
    assert source, "worker mount options must come from the shared configuration"
    worker_options = json.loads((terraform.parent / source.group(1).lstrip('/')).read_text())
    assert list(volumes["swesim-workspace"].gcs.mount_options) == worker_options
    assert {"uid=1000", "gid=1000"} <= set(worker_options)
    assert list(volumes["cloudsql"].cloud_sql_instance.instances) == ["proj:us-central1:swesim"]
    assert mounts == {"swesim-workspace": "/workspace", "cloudsql": "/cloudsql"}
    launch = json.loads((layout.trial_dir(p["trial_id"]) / "launch.json").read_text())
    assert launch["phase"] == "acknowledged" and launch["launcher"] == "cloudrun" and launch["job"] == job_name and launch["handle"] == out["handle"]
    assert launch["image_digest"] == s.worker_image_digest and launch["origin"] == "cloudrun"
    # a second launch_once reconciles the running execution; nothing is created or run again
    again = B.launch_once(layout, launcher, p, image_digest=s.worker_image_digest, origin="cloudrun")
    assert again["decision"] == "reconcile" and again["runtime"] == "running" and fake.runs == [job_name] and len(fake.created) == 1


def test_existing_job_on_create_means_already_launched_and_never_a_second_run(cloud):
    fake, launcher, layout, s = cloud; p = request(); job_name = launcher.job_name(p["trial_id"])
    # An earlier worker created and ran the Job but its launch.json was lost: the 409 is the launch-once guard.
    fake.create_job(parent=launcher.parent, job=run_v2.Job(), job_id=f"swesim-trial-{p['trial_id']}"); fake.run_job(name=job_name)
    fake.created.clear(); fake.runs.clear()
    d = layout.trial_dir(p["trial_id"]); d.mkdir(parents=True); atomic_json(d / "request.json", p)
    # runtime_state sees the execution, so launch_once reconciles before even calling start
    assert B.launch_once(layout, launcher, p)["decision"] == "reconcile" and fake.runs == []
    # and start() itself, called directly, treats the 409 as already launched
    ack = launcher.start(p["trial_id"], d / "request.json", {})
    assert ack.returncode == 0 and "already launched" in ack.stderr and ack.handle == job_name and fake.runs == [] and fake.created == []


def test_runtime_state_follows_the_execution(cloud):
    fake, launcher, layout, s = cloud; p = request(); tid = p["trial_id"]; job_name = launcher.job_name(tid)
    assert launcher.runtime_state(tid, {})[0] == "absent"                                # no Job
    B.launch_once(layout, launcher, p)
    assert launcher.runtime_state(tid, {})[0] == "running"
    fake.finish(job_name, succeeded=True)
    state, detail = launcher.runtime_state(tid, {})
    assert state == "succeeded" and "succeeded=1" in detail
    fake.finish(job_name, succeeded=False); fake.executions[job_name][-1].succeeded_count = 0
    assert launcher.runtime_state(tid, {})[0] == "failed"
    ex = fake.executions[job_name][-1]; ex.failed_count = 0; ex.cancelled_count = 1
    assert launcher.runtime_state(tid, {})[0] == "failed"
    ex.cancelled_count = 0                                                                # only the Completed=FAILED condition left
    assert launcher.runtime_state(tid, {})[0] == "failed"
    fake.jobs[job_name] = fake.jobs[job_name]; fake.executions[job_name] = []             # Job without executions (run_job never reached)
    assert launcher.runtime_state(tid, {})[0] == "absent"
    fake.fail_list = gexc.ServiceUnavailable("503")
    with pytest.raises(CR.JobStateUnavailable): launcher.runtime_state(tid, {})
    with pytest.raises(CR.JobStateUnavailable): B.read_evidence(layout, launcher, tid)   # never read as absent
    # the latest execution decides when there are several
    fake.fail_list = None; fake.run_job(name=job_name); fake.finish(job_name, succeeded=True); fake.run_job(name=job_name)
    assert launcher.runtime_state(tid, {})[0] == "running"


def test_decisions_over_cloud_run_evidence(cloud):
    fake, launcher, layout, s = cloud; p = request(); tid = p["trial_id"]; job_name = launcher.job_name(tid); d = layout.trial_dir(tid)
    B.launch_once(layout, launcher, p); fake.finish(job_name, succeeded=False)
    assert B.decide_launch(B.read_evidence(layout, launcher, tid)) == "reconcile"          # failed execution, no record: unit-failed downstream
    atomic_json(d / "record.json", {"trial_id": tid, "status": "completed", "reward": {"reward": 1.0, "success": 1}})
    assert B.decide_launch(B.read_evidence(layout, launcher, tid)) == "ingest"
    # a Job that was created but never ran, with the intent recorded: unresolved, never a relaunch
    fake.executions[job_name] = []; (d / "record.json").unlink()
    assert B.decide_launch(B.read_evidence(layout, launcher, tid)) == "unresolved" and fake.runs == [job_name]


def test_cancel_and_cleanup_are_idempotent(cloud):
    fake, launcher, layout, s = cloud; p = request(); tid = p["trial_id"]; job_name = launcher.job_name(tid)
    launcher.cancel(tid, {}); launcher.cleanup(tid, {})                                   # nothing exists: no error
    assert fake.cancelled == [] and fake.deleted == []
    B.launch_once(layout, launcher, p)
    launcher.cancel(tid, {})
    assert fake.cancelled == [f"{job_name}/executions/swesim-trial-{tid}-1"]
    fake.finish(job_name, succeeded=True); launcher.cancel(tid, {})
    assert len(fake.cancelled) == 1                                                       # a finished execution is not cancelled again
    launcher.cleanup(tid, {}); launcher.cleanup(tid, {})
    assert fake.deleted == [job_name] and job_name not in fake.jobs and launcher.runtime_state(tid, {})[0] == "absent"


def test_labels_and_request_path_are_sanitised(cloud):
    fake, launcher, layout, s = cloud
    assert CR.label_value("Run:ABC/def") == "run-abc-def" and CR.label_value(None) == "" and len(CR.label_value("x" * 100)) == 63
    p = request(run_workflow_id=None, solver_timeout=600); d = layout.trial_dir(p["trial_id"]); d.mkdir(parents=True); atomic_json(d / "request.json", p)
    job = launcher.job_spec(p["trial_id"], d / "request.json", p)
    assert job.labels["swesim-run-workflow-id"] == "" and job.template.template.timeout == timedelta(seconds=600 + 7200)
    with pytest.raises(B.LauncherConfigError, match="not under the workspace"):
        launcher.job_request_path(layout.root.parent / "elsewhere" / "request.json")
