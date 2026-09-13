"""Immutable authoritative row snapshots, published before disposable SQL projections.

Contract
- Path: `Layout.history_snapshot(kind, identity, revision)` where `identity` is the SHA256 of the row's primary key and
  `revision` the SHA256 of its content without `created_at`/`updated_at`, so a replay of the same report lands on the
  same object and a changed report is a new snapshot beside it (nothing is ever overwritten).
- Hosted (`Settings.origin == "cloudrun"` with a bucket): writes are generation-zero GCS uploads through the object API,
  which also works from the console's read-only FUSE mount; reads list the object prefix, so a rebuild never depends
  on a FUSE instance's cached directory listing. Anywhere else (laptop, VM, an import with `SWESIM_BUCKET` set) the
  files under `Layout.root` are used: a fully flushed temporary file is hard-linked into place, which fails atomically
  when the snapshot already exists; the existing snapshot is then verified to carry the same content.
- `read_rows` validates every snapshot against its model and its path; a mismatch is a `ValueError` (the caller
  records the skip), never a silently different row.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from google.api_core.exceptions import PreconditionFailed
from pydantic import BaseModel

from swesim.index import schema as S
from swesim.settings import Settings
from swesim.storage.layout import HistoryKind, Layout


def _json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _semantic(row: BaseModel) -> dict:
    return row.model_dump(mode="json", exclude_none=True, exclude={"created_at", "updated_at"})


def _raw_path(layout: Layout, kind: str, data: dict) -> Path:
    identity = hashlib.sha256(_json({key: data[key] for key in S.PRIMARY_KEYS[kind]})).hexdigest()
    content = {key: value for key, value in data.items() if key not in ("created_at", "updated_at") and value is not None}
    revision = hashlib.sha256(_json(content)).hexdigest()
    return layout.history_snapshot(kind, identity, revision)


def row_kind(row: BaseModel) -> str:
    kind = next((name for name, model in S.ROW_MODELS.items() if type(row) is model), None)
    if kind not in HistoryKind.__args__:
        raise ValueError(f"unsupported authoritative history model: {type(row).__name__}")
    return kind


def snapshot_path(layout: Layout, row: BaseModel) -> Path:
    kind = row_kind(row)
    return _raw_path(layout, kind, row.model_dump(mode="json"))


def hosted(settings: Settings | None) -> bool:
    """Cloud Run with a record bucket: the FUSE mount is not the write path (the console's is read-only; listings are cached)."""
    return settings is not None and settings.origin == "cloudrun" and bool(settings.bucket)


def bucket_for(settings: Settings):
    from google.cloud import storage
    return storage.Client(project=settings.gcp_project).bucket(settings.bucket)


def _verify_existing(data: bytes, row: BaseModel) -> None:
    existing = type(row).model_validate_json(data)
    if _semantic(existing) != _semantic(row):
        raise ValueError("immutable history snapshot content does not match its identity")


def persist_row(layout: Layout, row: BaseModel, settings: Settings | None = None) -> Path:
    """Publish the first snapshot for this row content, without changing an existing snapshot on replay."""
    path = snapshot_path(layout, row)
    data = _json(row.model_dump(mode="json"))
    if hosted(settings):
        bucket = bucket_for(settings)
        blob = bucket.blob(layout.relative(path))
        try:
            blob.upload_from_string(data, content_type="application/json", if_generation_match=0)
        except PreconditionFailed:
            current = bucket.get_blob(layout.relative(path))
            if current is None:
                raise RuntimeError("history snapshot disappeared while reconciling publication")
            _verify_existing(current.download_as_bytes(if_generation_match=current.generation), row)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".history-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            _verify_existing(path.read_bytes(), row)
    finally:
        if os.path.exists(temp): os.unlink(temp)
    return path


def read_rows(layout: Layout, kind: HistoryKind, settings: Settings | None = None) -> list[BaseModel]:
    """Read and validate every immutable snapshot of one entity; corruption is an explicit projection failure."""
    directory = layout.history_dir(kind)
    model = S.ROW_MODELS[kind]
    rows: list[BaseModel] = []
    if hosted(settings):
        bucket = bucket_for(settings)
        prefix = layout.relative(directory) + "/"
        records = ((blob.name, blob.download_as_bytes(if_generation_match=blob.generation))
                   for blob in bucket.list_blobs(prefix=prefix) if blob.name.endswith(".json"))
    else:
        records = ((layout.relative(path), path.read_bytes()) for path in sorted(directory.glob("*/*.json")))
    for name, data in records:
        raw = json.loads(data)
        row = model.model_validate_json(data)
        if layout.relative(_raw_path(layout, kind, raw)) != name:
            raise ValueError(f"history snapshot does not match its path: {name}")
        rows.append(row)
    return rows


def latest_rows(rows: Iterable[BaseModel]) -> list[BaseModel]:
    """Latest snapshot per primary key; lifecycle-aware workflow precedence is applied by its projection."""
    latest = {}
    for row in rows:
        kind = row_kind(row)
        key = tuple(getattr(row, column) for column in S.PRIMARY_KEYS[kind])
        prior = latest.get(key)
        if prior is None or row.updated_at >= prior.updated_at:
            latest[key] = row
    return list(latest.values())
