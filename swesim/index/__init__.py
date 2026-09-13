"""The Postgres index of the record (interfaces §2): a disposable, rebuildable projection of the files.

The files under `Layout.root` are the record; every table here can be truncated and rebuilt from them
(`IndexStore.rebuild_from_layout`, `swesim index rebuild`). One Pydantic row model per table (`models.py`), one
SQLAlchemy table per model (`schema.py`), forward-only Alembic revisions (`migrations/`), the writer and SQL reader
(`store.py`), the file-backed reader (`files.py`), the shared projection from files to rows (`project.py`), the
Temporal activities that keep the index current (`activities.py`) and the operator CLI (`cli.py`).
"""
