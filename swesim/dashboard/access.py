"""Data-access boundary of the console: one reader, one layout, both resolved from ``Settings`` (interfaces §8).

Contract
- ``reader_for(settings)`` is ``swesim.index.get_reader(settings)``: ``IndexStore`` when the index is enabled, the storage
  stream's ``FileIndexReader`` over ``settings.workspace`` otherwise. One instance per settings object (``reset()`` for tests).
- ``layout_for(settings)`` is ``swesim.storage.layout.Layout`` rooted at the workspace: the dashboard derives no path itself.
- ``record_action(reader, settings, row)`` writes an ``OperatorActionRow`` through the store's ``add_operator_action`` when
  the reader has one, else appends to ``evidence/console/actions.jsonl`` (which the file reader lists as ``operator_actions``).
- The console never reads ``os.environ``.
"""
from __future__ import annotations

import json
import re

from swesim.index import get_reader
from swesim.settings import Settings
from swesim.storage.layout import Layout

_READERS: dict[int, object] = {}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
TRIAL_RE = re.compile(r"^[a-f0-9]{24}$")


def layout_for(settings: Settings) -> Layout:
    return Layout(root=settings.workspace)


def reader_for(settings: Settings):
    key = id(settings)
    if key not in _READERS:
        _READERS[key] = get_reader(settings)
    return _READERS[key]


def reset() -> None:
    _READERS.clear()


def all_rows(read, f):
    """Read every matching indexed row in bounded pages."""
    offset = 0
    while True:
        pg = read(f.model_copy(update={"offset": offset, "limit": 500}))
        yield from pg.items
        offset += len(pg.items)
        if not pg.items or offset >= pg.total:
            return


def record_action(reader, settings: Settings, row) -> None:
    fn = getattr(reader, "add_operator_action", None)
    if callable(fn):
        fn(row)
        return
    if settings.bucket:
        raise RuntimeError("hosted operator audit backend is unavailable; console mount is read-only")
    path = layout_for(settings).evidence_dir("console") / "actions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row.model_dump(mode="json"), sort_keys=True) + "\n")
