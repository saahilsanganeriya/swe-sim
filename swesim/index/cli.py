"""`swesim index`: operate the Postgres index (mounted by `swesim/cli.py` as `app.add_typer(index_app, name="index")`;
also runnable as `python -m swesim.index.cli`).

Contract
- `migrate`: Alembic upgrade to head on `Settings.database_url`.
- `rebuild [--origin laptop|vm-systemd|cloudrun] [--json]`: truncate-and-reproject every file-backed table from
  `Settings.workspace` / `Settings.registry_path`; prints the counts, the diff and the skipped ids.
- `status`: index enabled?, dialect, current revision, row counts per table, the last rebuild. Never prints the URL.
- Exit code 2 when the index is disabled or an argument is invalid; nothing here reads `os.environ`.
"""
from __future__ import annotations

import json

import sqlalchemy as sa
import typer

from swesim.index import schema as S
from swesim.index.project import ORIGINS
from swesim.index.store import IndexStore
from swesim.settings import Settings, get_settings
from swesim.storage.layout import Layout

index_app = typer.Typer(no_args_is_help=True, help="The Postgres index of the record: migrate, rebuild from the files, status.")


def _store(settings: Settings) -> IndexStore:
    store = IndexStore.from_settings(settings)
    if store is None:
        typer.echo("index disabled: SWESIM_DATABASE_URL is not set", err=True)
        raise typer.Exit(code=2)
    return store


@index_app.command()
def migrate() -> None:
    """Upgrade the index schema to the latest revision."""
    settings = get_settings()
    store = _store(settings)
    store.migrate()
    typer.echo(f"index: migrated to head ({store.engine.dialect.name}, revision {_revision(store)})")


@index_app.command()
def rebuild(origin: str | None = typer.Option(None, help="Label for rows whose files carry none: laptop | vm-systemd | cloudrun (default: SWESIM_ORIGIN)."),
            as_json: bool = typer.Option(False, "--json", help="Print the rebuild row as JSON.")) -> None:
    """Rebuild every file-backed table from the record (the files stay the truth; the index is disposable)."""
    settings = get_settings()
    if origin is not None and origin not in ORIGINS:
        typer.echo(f"index: --origin must be one of {', '.join(ORIGINS)}", err=True)
        raise typer.Exit(code=2)
    store = _store(settings)
    row = store.rebuild_from_layout(Layout(root=settings.workspace), settings.registry_path, origin=origin)
    if as_json:
        typer.echo(row.model_dump_json(indent=2))
        return
    typer.echo(f"index: rebuild {row.rebuild_id} from {settings.workspace} (origin {row.origin}) in "
               f"{(row.finished_at - row.started_at).total_seconds():.1f}s")
    for name, d in row.diff.items():
        typer.echo(f"  {name:<17} {d['after']:>7}  (was {d['before']}, {d['delta']:+d})")
    for table, items in row.counts.get("skipped", {}).items():
        for item in items:
            typer.echo(f"  skipped {table} {item['id']}: {item['reason']}")


@index_app.command()
def status() -> None:
    """Index enabled?, revision, row counts per table, last rebuild."""
    settings = get_settings()
    typer.echo(f"index enabled: {settings.index_enabled}")
    typer.echo(f"workspace: {settings.workspace}  registry: {settings.registry_path}  origin: {settings.origin}")
    if not settings.index_enabled:
        return
    store = IndexStore.from_settings(settings)
    assert store is not None
    typer.echo(f"dialect: {store.engine.dialect.name}  revision: {_revision(store) or 'not migrated'}")
    tables = set(sa.inspect(store.engine).get_table_names())
    with store.engine.connect() as conn:
        for name, table in S.TABLES.items():
            count = conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() if name in tables else "missing"
            typer.echo(f"  {name:<17} {count!s:>7}")
    rebuilds = store.rebuilds() if "index_rebuilds" in tables else []
    if rebuilds:
        last = rebuilds[0]
        deltas = {k: v["delta"] for k, v in (last.diff or {}).items() if v["delta"]}
        typer.echo(f"last rebuild: {last.rebuild_id} at {last.started_at.isoformat(timespec='seconds')} (origin {last.origin}) "
                   f"diff {json.dumps(deltas) if deltas else 'none'}")
    else:
        typer.echo("last rebuild: none")


def _revision(store: IndexStore) -> str | None:
    if "alembic_version" not in sa.inspect(store.engine).get_table_names():
        return None
    with store.engine.connect() as conn:
        return conn.execute(sa.text("select version_num from alembic_version")).scalar_one_or_none()


if __name__ == "__main__":
    index_app()
