"""Cloud verification checks actual queue consumers, beyond a resource's Ready condition."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from swesim.settings import Settings

spec = importlib.util.spec_from_file_location("verify_runtime", Path(__file__).parents[1] / "scripts/cloud/verify_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def test_cloud_verification_rejects_local_temporal(tmp_path):
    settings = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "absent")})
    result = asyncio.run(runtime.check_temporal(settings))
    assert not result["passed"] and "not a Cloud" in result["reason"]


def test_all_worker_queues_need_live_pollers(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMPORAL_API_KEY", "private-test-key")
    settings = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "absent"),
                                 "TEMPORAL_ADDRESS": "us-central1.gcp.api.temporal.io:7233",
                                 "TEMPORAL_NAMESPACE": "example.ns"})
    absent = {"swesim-trials"}
    requests = []
    async def describe(request):
        requests.append(request)
        return SimpleNamespace(pollers=[] if request.task_queue.name in absent else [object()])
    client = SimpleNamespace(workflow_service=SimpleNamespace(describe_task_queue=describe))
    result = asyncio.run(runtime.check_temporal(settings, client))
    assert not result["passed"] and result["pollers"]["swesim-trials/activity"] == 0
    assert {request.namespace for request in requests} == {"example.ns"}
    assert "private-test-key" not in str(result)
    absent.clear()
    assert asyncio.run(runtime.check_temporal(settings, client))["passed"]
