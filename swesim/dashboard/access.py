"""Data-access boundary of the console: one reader, one layout, both resolved from ``Settings``.

Contract
- ``reader_for(settings)`` returns the storage stream's ``swesim.index.get_reader(settings)`` (IndexStore when the index is
  enabled, its FileIndexReader otherwise) whenever ``swesim.index`` is importable; else the console's own
  ``FileIndexReader`` shim over ``settings.workspace``. The console never reads ``os.environ``.
- ``layout_for(settings)`` returns ``swesim.storage.layout.Layout`` when present, else ``_LayoutShim`` with the same
  method names and the path table of interfaces §1 (paths derived here and nowhere else in the dashboard).
- ``record_action(reader, row)`` writes an ``OperatorActionRow`` through ``add_operator_action`` on the store/reader when it
  has one, else appends to ``evidence/console/actions.jsonl``.
- One reader per settings object (cached); ``reset()`` drops the cache (tests).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from swesim.dashboard.filereader import FileIndexReader
from swesim.settings import Settings

_READERS: dict[int, object] = {}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
TRIAL_RE = re.compile(r"^[a-f0-9]{24}$")


class LayoutError(ValueError):
    """Invalid scenario/trial id or a name outside the layout."""


class _LayoutShim:
    """Interfaces §1 path table, used until ``swesim/storage/layout.py`` is merged."""

    SCENARIO_FILES = ("scenario.json", "scenario.draft.json", "draft.meta.json", "judge.json", "approval.json", "compile.json", "validation.json",
                      "difficulty.json", "audit.json")
    TRIAL_FILES = ("request.json", "launch.json", "status.json", "record.json", "report.json", "reward.json", "run_config.json", "spec.json",
                   "transcript.json", "runner-error.json", "launcher.log", "costs.json", "audit.json")

    def __init__(self, root: Path):
        self.root = Path(root)

    def _sid(self, sid: str) -> str:
        if not ID_RE.fullmatch(sid or ""):
            raise LayoutError(f"invalid scenario id {sid!r}")
        return sid

    def _tid(self, tid: str) -> str:
        if not TRIAL_RE.fullmatch(tid or ""):
            raise LayoutError(f"invalid trial id {tid!r}")
        return tid

    def scenario_dir(self, scenario_id: str) -> Path:
        return self.root / "scenarios" / self._sid(scenario_id)

    def scenario_file(self, scenario_id: str, name: str) -> Path:
        if name not in self.SCENARIO_FILES:
            raise LayoutError(f"not a scenario file: {name!r}")
        return self.scenario_dir(scenario_id) / name

    def scenario_assets_dir(self, scenario_id: str) -> Path:
        return self.scenario_dir(scenario_id) / "assets"

    def scenario_archive_dir(self, scenario_id: str, version: int) -> Path:
        return self.scenario_dir(scenario_id) / "archive" / f"v{int(version)}"

    def task_dir(self, scenario_id: str, condition: str) -> Path:
        if condition not in ("full", "hidden", "interactive"):
            raise LayoutError(f"invalid condition {condition!r}")
        return self.root / "tasks" / f"{self._sid(scenario_id)}__{condition}"

    def trial_dir(self, trial_id: str) -> Path:
        return self.root / "trials" / self._tid(trial_id)

    def trial_file(self, trial_id: str, name: str) -> Path:
        if name not in self.TRIAL_FILES:
            raise LayoutError(f"not a trial file: {name!r}")
        return self.trial_dir(trial_id) / name

    def trial_task_dir(self, trial_id: str) -> Path:
        return self.trial_dir(trial_id) / "task"

    def export_path(self, trial_id: str) -> Path:
        return self.root / "exports" / f"{self._tid(trial_id)}.episode.json"

    def candidates_dir(self) -> Path:
        return self.root / "candidates"

    def probe_dir(self, instance_id: str) -> Path:
        return self.root / "probes" / instance_id

    def evidence_dir(self, kind: str) -> Path:
        return self.root / "evidence" / kind

    def jobs_dir(self, kind: str) -> Path:
        return self.root / "jobs" / kind

    def registry_path(self) -> Path:
        return self.root / "registry.json"

    def dataset_path(self) -> Path:
        return self.root / "dataset.json"

    def relative(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.root.resolve()).as_posix()


def layout_for(settings: Settings):
    try:
        from swesim.storage.layout import Layout  # type: ignore
        return Layout(root=settings.workspace)
    except ImportError:
        return _LayoutShim(settings.workspace)


def reader_for(settings: Settings):
    """The console's single IndexReader for this settings object."""
    key = id(settings)
    if key in _READERS:
        return _READERS[key]
    try:
        from swesim.index import get_reader  # type: ignore
        reader = get_reader(settings)
    except ImportError:
        reader = FileIndexReader(settings.workspace, settings.registry_path, origin=settings.origin, spend_cap_usd=settings.spend_cap_usd)
    _READERS[key] = reader
    return reader


def reset() -> None:
    _READERS.clear()


def record_action(reader, settings: Settings, row) -> None:
    """Audit an operator action: the store's ``add_operator_action`` when present, else the JSONL under evidence/console/."""
    fn = getattr(reader, "add_operator_action", None)
    if callable(fn):
        fn(row)
        return
    path = settings.workspace / "evidence" / "console" / "actions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row.model_dump(mode="json"), sort_keys=True) + "\n")
