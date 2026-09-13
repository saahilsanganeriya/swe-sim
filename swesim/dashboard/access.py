"""Data-access boundary of the console: one reader, one layout, both resolved from ``Settings``.

Contract
- ``reader_for(settings)`` returns the storage stream's ``swesim.index.get_reader(settings)`` (IndexStore when the index is
  enabled, its file reader otherwise) whenever that function exists; until it is merged, the console's own
  ``FileIndexReader`` over ``settings.workspace``. The console never reads ``os.environ``.
- ``layout_for(settings)`` returns ``swesim.storage.layout.Layout`` rooted at the workspace: paths are derived there and
  nowhere else in the dashboard.
- ``record_action(reader, settings, row)`` writes an ``OperatorActionRow`` through ``add_operator_action`` on the
  store/reader when it has one, else appends to ``evidence/console/actions.jsonl``.
- One reader per settings object (cached); ``reset()`` drops the cache (tests).
"""
from __future__ import annotations

import json
import re

from swesim.dashboard.filereader import FileIndexReader
from swesim.settings import Settings
from swesim.storage.layout import Layout

_READERS: dict[int, object] = {}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
TRIAL_RE = re.compile(r"^[a-f0-9]{24}$")


def layout_for(settings: Settings) -> Layout:
    return Layout(root=settings.workspace)


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
