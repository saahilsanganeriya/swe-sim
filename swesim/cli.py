"""Command-line entry point. Stages are wired in as they are implemented.

Pipeline: mine -> judge -> spec -> compile -> validate -> register -> run -> score -> view
See docs/superpowers/specs/2026-09-12-swe-simulator-design.md, section 7.
"""

from __future__ import annotations

import typer

app = typer.Typer(no_args_is_help=True, help=__doc__)

STAGES = ["mine", "judge", "spec", "compile", "validate", "register", "run", "score", "view"]


def _not_implemented(stage: str) -> None:
    typer.echo(f"swesim {stage}: not implemented yet (see spec section 7)", err=True)
    raise typer.Exit(code=2)


for _stage in STAGES:

    def _make(stage: str):
        def _cmd() -> None:
            _not_implemented(stage)

        _cmd.__name__ = stage
        _cmd.__doc__ = f"Pipeline stage: {stage}."
        return _cmd

    app.command(name=_stage)(_make(_stage))


if __name__ == "__main__":
    app()
