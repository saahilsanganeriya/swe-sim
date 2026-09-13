"""Hosted registration serializes the entire RMW and bypasses stale FUSE caches."""
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import PreconditionFailed

from swesim.models import ScenarioSpec, minimal_spec_dict
from swesim.paths import Workspace, atomic_json
from swesim.settings import Settings
from swesim.stages.register import register_scenario


class FakeIndex:
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def __init__(self):
        self.lock = threading.Lock()
        self.owner = None
        self.keys = []

    @contextmanager
    def advisory_lock(self, key):
        self.keys.append(key)
        with self.lock:
            self.owner = threading.get_ident()
            try:
                yield
            finally:
                self.owner = None


class FakeBucket:
    def __init__(self, index):
        self.index = index
        self.data = {}
        self.fail_dataset_once = False

    def check(self):
        assert self.index.owner == threading.get_ident(), "bucket read/write occurred outside the registry lock"

    def get_blob(self, name):
        self.check()
        return FakeBlob(self, name, self.data[name][0]) if name in self.data else None

    def blob(self, name):
        return FakeBlob(self, name)


class FakeBlob:
    def __init__(self, bucket, name, generation=None):
        self.bucket, self.name, self.generation = bucket, name, generation

    def download_as_bytes(self, *, if_generation_match, **kwargs):
        self.bucket.check()
        generation, data = self.bucket.data[self.name]
        if generation != if_generation_match:
            raise PreconditionFailed("stale generation")
        return data

    def upload_from_string(self, data, *, if_generation_match, **kwargs):
        self.bucket.check()
        prior = self.bucket.data.get(self.name, (0, b""))[0]
        if prior != if_generation_match:
            raise PreconditionFailed("stale generation")
        if self.name == "dataset.json" and self.bucket.fail_dataset_once:
            self.bucket.fail_dataset_once = False
            raise ConnectionError("lost dataset write")
        self.bucket.data[self.name] = (prior + 1, data.encode() if isinstance(data, str) else data)


def prepare(tmp_path, sid="scenario-one"):
    spec = ScenarioSpec.model_validate({**minimal_spec_dict(), "id": sid})
    ws = Workspace(tmp_path / "workspace", tmp_path / "workspace" / "registry.json")
    atomic_json(ws.scenario_dir(sid) / "compile.json", {"spec_hash": spec.spec_hash, "compiled_hash": "c" * 64})
    atomic_json(ws.scenario_dir(sid) / "validation.json", {"status": "valid", "spec_hash": spec.spec_hash, "compiled_hash": "c" * 64})
    settings = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_WORKSPACE": str(ws.root),
                                  "SWESIM_REGISTRY": str(ws.registry_path), "SWESIM_ORIGIN": "cloudrun", "SWESIM_BUCKET": "test-record",
                                  "SWESIM_DATABASE_URL": "postgresql+psycopg://unused@localhost/test"})
    return ws, spec, settings


@pytest.fixture
def hosted(monkeypatch):
    from swesim.storage import registry
    index = FakeIndex(); bucket = FakeBucket(index)
    monkeypatch.setattr(registry, "index_for", lambda settings: index)
    monkeypatch.setattr(registry, "bucket_for", lambda settings: bucket)
    import fcntl
    monkeypatch.setattr(fcntl, "flock", lambda *args: pytest.fail("hosted registration attempted FUSE flock"))
    return index, bucket


def test_hosted_registry_uses_remote_generation_and_not_stale_mount(tmp_path, hosted):
    index, bucket = hosted
    ws, spec, settings = prepare(tmp_path)
    ws.registry_path.write_text('{"scenarios":{"stale-mount-only":{}}}')
    register_scenario(ws, spec, settings=settings)
    assert set(json.loads(bucket.data["registry.json"][1])["scenarios"]) == {spec.id}
    assert len(json.loads(bucket.data["dataset.json"][1])["tasks"]) == 3
    assert index.keys and not (ws.root / "registry.json.lock").exists()


def test_concurrent_registrations_preserve_both_scenarios(tmp_path, hosted):
    _, bucket = hosted
    ws, first, settings = prepare(tmp_path, "scenario-one")
    _, second, _ = prepare(tmp_path, "scenario-two")
    failures = []

    def register(spec):
        try:
            register_scenario(ws, spec, settings=settings)
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=register, args=(spec,)) for spec in (first, second)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=5)
    assert not failures and not any(thread.is_alive() for thread in threads)
    assert set(json.loads(bucket.data["registry.json"][1])["scenarios"]) == {first.id, second.id}
    assert len(json.loads(bucket.data["dataset.json"][1])["tasks"]) == 6


def test_retry_repairs_dataset_after_registry_commit(tmp_path, hosted):
    _, bucket = hosted
    ws, spec, settings = prepare(tmp_path)
    bucket.fail_dataset_once = True
    with pytest.raises(RuntimeError, match="dataset"):
        register_scenario(ws, spec, settings=settings)
    assert spec.id in json.loads(bucket.data["registry.json"][1])["scenarios"]
    register_scenario(ws, spec, settings=settings)
    assert len(json.loads(bucket.data["dataset.json"][1])["tasks"]) == 3


def test_hosted_registration_requires_postgres(tmp_path, monkeypatch):
    from swesim.storage import registry
    ws, spec, settings = prepare(tmp_path)
    monkeypatch.setattr(registry, "index_for", lambda settings: None)
    with pytest.raises(ValueError, match="PostgreSQL"):
        register_scenario(ws, spec, settings=settings)
    assert not (ws.root / "registry.json.lock").exists()


def test_generation_conflict_never_overwrites_external_registry_update(hosted):
    from swesim.storage.registry import RemoteRegistry
    index, bucket = hosted
    bucket.data["registry.json"] = (1, b'{"scenarios":{}}')
    with index.advisory_lock("registry"):
        storage = RemoteRegistry(bucket, "registry.json", "dataset.json")
        first = storage.read()
        external = b'{"scenarios":{"external":{"version":2}}}'
        bucket.data["registry.json"] = (2, external)
        with pytest.raises(RuntimeError, match="PreconditionFailed"):
            storage.write(first, {"tasks": []})
    assert bucket.data["registry.json"] == (2, external) and "dataset.json" not in bucket.data
