"""The Postgres index of the record (interfaces §2): a disposable, rebuildable projection of the files.

The files under `Layout.root` are the record; every table here can be truncated and rebuilt from them
(`IndexStore.rebuild_from_layout`, `swesim index rebuild`). One Pydantic row model per table (`models.py`), one
SQLAlchemy table per model (`schema.py`), forward-only Alembic revisions (`migrations/`), the writer and SQL reader
(`store.py`), the file-backed reader (`files.py`), the shared projection from files to rows (`project.py`), the
report computations both readers share (`reports.py`), the Temporal activities that keep the index current
(`activities.py`) and the operator CLI (`cli.py`).

`get_reader(settings)` is the console's entry point: `IndexStore` when the index is enabled, else `FileIndexReader`.
"""
from __future__ import annotations

from swesim.index.reads import IndexReader, get_reader

__all__ = ["IndexReader", "get_reader"]
