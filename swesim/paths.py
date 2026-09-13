"""Workspace adapter and the atomic JSON writer.

Contract
- `Workspace` is a thin adapter over `swesim.storage.layout.Layout` that keeps the names today's stages and tests call
  (`root`, `registry_path`, `scenario_dir`, `task_dir`, `jobs_dir`, `candidates_dir`). New code takes a `Layout`.
  `registry_path` may live outside the root on a laptop (`Settings.registry_path`); the layout's own
  `registry_path()` is the record's.
- `atomic_json` writes to a temporary file in the target directory and `os.replace`s it: readers see the old file or
  the whole new one, never a partial write. That is the property GCS FUSE keeps within one directory.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from swesim.storage.layout import Layout


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, sort_keys=True, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Workspace:
    """Today's callers' view of the record; every path comes from `self.layout`."""

    def __init__(self, root: Path | str = "workspace", registry: Path | str = "registry.json"):
        self.root = Path(root).resolve()
        self.registry_path = Path(registry).resolve()
        self.layout = Layout(root=self.root)

    def scenario_dir(self, sid: str) -> Path:
        return self.layout.scenario_dir(sid)

    def task_dir(self, sid: str, condition: str) -> Path:
        return self.layout.task_dir(sid, condition)  # type: ignore[arg-type]  (validated by Layout)

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    @property
    def candidates_dir(self) -> Path:
        return self.layout.candidates_dir()
