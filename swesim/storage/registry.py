"""Registry transactions: PostgreSQL-serialized GCS I/O when hosted, flock for local files only.

Hosted reads bypass FUSE caches and every upload has a generation precondition. The registry is authoritative;
dataset.json is derived and can be repaired by retrying an interrupted registration under the same lock.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator, Protocol

from google.api_core.exceptions import GoogleAPIError

from swesim.paths import Workspace, atomic_json
from swesim.settings import Settings, get_settings

if TYPE_CHECKING:
    from google.cloud.storage import Bucket
    from swesim.index.store import IndexStore


class RegistryIO(Protocol):
    def read(self) -> dict: ...
    def write(self, registry: dict, dataset: dict) -> None: ...


def index_for(settings: Settings) -> IndexStore | None:
    from swesim.index.store import IndexStore
    return IndexStore.from_settings(settings)


def bucket_for(settings: Settings) -> Bucket:
    from google.cloud import storage
    return storage.Client(project=settings.gcp_project).bucket(settings.bucket)


def _registry(data: bytes | str) -> dict:
    value = json.loads(data)
    if not isinstance(value, dict) or not isinstance(value.get("scenarios", {}), dict):
        raise ValueError("registry must contain a scenarios object")
    return value


@dataclass
class LocalRegistry:
    workspace: Workspace

    def read(self) -> dict:
        path = self.workspace.registry_path
        return _registry(path.read_bytes()) if path.exists() else {"scenarios": {}}

    def write(self, registry: dict, dataset: dict) -> None:
        atomic_json(self.workspace.registry_path, registry)
        atomic_json(self.workspace.layout.dataset_path(), dataset)


@dataclass
class RemoteRegistry:
    bucket: Bucket
    registry_name: str
    dataset_name: str
    generation: int = 0

    def read(self) -> dict:
        try:
            blob = self.bucket.get_blob(self.registry_name)
            if blob is None:
                self.generation = 0
                return {"scenarios": {}}
            self.generation = int(blob.generation)
            return _registry(blob.download_as_bytes(if_generation_match=self.generation))
        except (GoogleAPIError, OSError) as exc:
            raise RuntimeError(f"registry read failed: {type(exc).__name__}") from exc

    def write(self, registry: dict, dataset: dict) -> None:
        try:
            self.bucket.blob(self.registry_name).upload_from_string(
                json.dumps(registry, sort_keys=True, indent=2, allow_nan=False),
                content_type="application/json", if_generation_match=self.generation)
        except (GoogleAPIError, OSError) as exc:
            raise RuntimeError(f"registry write failed: {type(exc).__name__}; reconcile and retry") from exc
        try:
            prior = self.bucket.get_blob(self.dataset_name)
            self.bucket.blob(self.dataset_name).upload_from_string(
                json.dumps(dataset, sort_keys=True, indent=2, allow_nan=False), content_type="application/json",
                if_generation_match=int(prior.generation) if prior is not None else 0)
        except (GoogleAPIError, OSError) as exc:
            raise RuntimeError(f"dataset write failed after registry publication: {type(exc).__name__}; retry repairs it") from exc


@contextmanager
def locked_registry(workspace: Workspace, settings: Settings | None = None) -> Iterator[RegistryIO]:
    """Hold the lock across every registry read, mutation and registry/dataset publication."""
    settings = settings or get_settings()
    if settings.origin == "cloudrun":
        index = index_for(settings)
        if index is None or index.engine.dialect.name != "postgresql":
            raise ValueError("hosted registry requires PostgreSQL advisory locking")
        if not settings.bucket:
            raise ValueError("hosted registry requires a record bucket")
        name = workspace.layout.relative(workspace.registry_path)
        dataset_name = workspace.layout.relative(workspace.layout.dataset_path())
        with index.advisory_lock(f"swesim.registry:{settings.bucket}:{name}"):
            yield RemoteRegistry(bucket_for(settings), name, dataset_name)
        return
    import fcntl
    workspace.registry_path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(workspace.registry_path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield LocalRegistry(workspace)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
