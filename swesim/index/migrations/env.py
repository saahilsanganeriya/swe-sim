"""Alembic environment of the swesim index.

Contract
- Target metadata is `swesim.index.schema.metadata`; revisions are generated from it and never edited by hand to
  diverge (a test compares the migrated database with the metadata).
- Connection: `config.attributes["connection"]` when `IndexStore.migrate()` runs us inside its own transaction;
  otherwise an engine built from `Settings.database_url` (the `alembic` CLI path). The URL is never logged.
- `TZDateTime` renders as `sa.TIMESTAMP(timezone=True)` in generated revisions so migration files import only
  `sqlalchemy` and `alembic`.
"""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from swesim.index.schema import TZDateTime, metadata

config = context.config
target_metadata = metadata


def _render_item(type_, obj, autogen_context):
    if type_ == "type" and isinstance(obj, TZDateTime):
        return "sa.TIMESTAMP(timezone=True)"
    return False


def _url() -> str:
    from swesim.settings import get_settings

    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("SWESIM_DATABASE_URL is not set; the index is disabled")
    return settings.database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, render_item=_render_item,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_item=_render_item, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = create_engine(_url(), pool_pre_ping=True)
    with engine.connect() as connection:
        _run(connection)
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
