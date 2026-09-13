"""`swesim workflows reconcile` (P1-6): close `running` workflow rows the index still shows from Temporal's own view of each
execution (`swesim.workflows.client.reconcile_workflows`).

The integrator mounts it in `swesim/cli.py`, next to the other `workflows_app` commands, with one line:

    from swesim.workflows.cli_reconcile import mount as mount_reconcile; mount_reconcile(workflows_app)

Also runnable on its own (`python -m swesim.workflows.cli_reconcile --max-age-hours 12`). Exit code 2 when the index is
disabled. Nothing here reads `os.environ`: the connection and the database come from `Settings`.
"""
from __future__ import annotations

import asyncio
import json

import typer

from swesim.settings import get_settings


def reconcile(max_age_hours: int = typer.Option(48, help="Only running rows started within this many hours (0: every running row)."),
              as_json: bool = typer.Option(False, "--json", help="Print the result as JSON.")) -> None:
    """Close stale `running` workflow rows from Temporal: completed | failed | cancelled | terminated | timed_out, or `unknown`
    when Temporal no longer has the execution. Rows already terminal are never touched."""
    from swesim.workflows.client import reconcile_workflows

    settings = get_settings()
    if not settings.index_enabled:
        typer.echo("index disabled: SWESIM_DATABASE_URL is not set", err=True)
        raise typer.Exit(code=2)
    out = asyncio.run(reconcile_workflows(settings, max_age_hours=max_age_hours if max_age_hours > 0 else None))
    if as_json:
        typer.echo(json.dumps(out, indent=2, default=str))
        return
    for row in out["rows"]:
        typer.echo(f"{row['workflow_id']}\t{row['from']} -> {row['to']}")
    for err in out["errors"]:
        typer.echo(f"{err['workflow_id']}\terror: {err['error']}", err=True)
    typer.echo(f"checked={out['checked']} reconciled={out['reconciled']} unknown={out['unknown']} still_running={out['still_running']} errors={len(out['errors'])}")


def mount(workflows_app: typer.Typer) -> None:
    """Register `reconcile` on the `swesim workflows` group (the integrator's one-line mount)."""
    workflows_app.command("reconcile")(reconcile)


reconcile_app = typer.Typer(help="Reconcile the index's running workflow rows with Temporal.")
mount(reconcile_app)

if __name__ == "__main__":
    reconcile_app()
