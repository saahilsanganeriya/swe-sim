"""Evaluator commands of the task-sense audit (mounted by the integrator as ``swesim audit ...``; also
``python -m swesim.audit``). These audit records that already exist; they create no task and run no trial.

Commands: ``scenario <sid>``, ``trial <tid>``, ``all`` (every registered scenario and every trial directory with a
``transcript.json``; failures are recorded per item and never stop the run), ``report`` (aggregate and write Markdown +
JSON). The record root and registry come from ``Settings`` (``SWESIM_WORKSPACE``, ``SWESIM_REGISTRY``).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import typer

from swesim.audit import report as REP
from swesim.audit import run as RUN
from swesim.audit.activities import layout_from_settings
from swesim.audit.llm_audit import AuditLLMError, Judge
from swesim.audit.record import AuditInputError, RecordPaths
from swesim.settings import get_settings

app = typer.Typer(help="Task-sense audit (evaluator-only): label scenarios and transcripts, aggregate the report.", no_args_is_help=True)


def _echo(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, sort_keys=True, default=str))


@app.command()
def scenario(scenario_id: str, force: bool = typer.Option(False, help="re-audit even when audit.json is current")) -> None:
    """Audit one scenario: coherent, fair-tests, plausible-withholding, facts-within-contract."""
    judge = Judge()
    audit, written = RUN.audit_scenario_result(layout_from_settings(), scenario_id, force=force, judge=judge)
    _echo({"written": written, "audit": audit.model_dump(mode="json"), "llm_usage": judge.usage()})


@app.command()
def trial(trial_id: str, force: bool = typer.Option(False, help="re-audit even when audit.json is current")) -> None:
    """Audit one trial's transcript: per-question and per-reply labels, asked-before-implementing."""
    judge = Judge()
    audit, written = RUN.audit_trial_result(layout_from_settings(), trial_id, force=force, judge=judge)
    _echo({"written": written, "audit": audit.model_dump(mode="json"), "llm_usage": judge.usage()})


def audit_everything(layout, registry_path: Path | None, *, force: bool = False, judge: Judge | None = None) -> dict:
    """Audit every registered scenario and every trial directory with a transcript.json; per-item failures are recorded."""
    judge = judge or Judge()
    paths = RecordPaths(layout)
    registry = REP._registry(paths, registry_path)
    summary: dict = {"scenarios": {}, "trials": {}, "errors": []}
    for sid in sorted(registry):
        try:
            audit, written = RUN.audit_scenario_result(layout, sid, force=force, judge=judge)
            summary["scenarios"][sid] = {"written": written, "coherent": audit.coherent, "fair_tests": audit.fair_tests,
                                         "plausible_withholding": audit.plausible_withholding,
                                         "facts_within_contract": audit.facts_within_contract, "model": audit.model}
        except (AuditInputError, AuditLLMError) as e:
            summary["errors"].append({"scenario_id": sid, "error": f"{type(e).__name__}: {e}"})
    troot = paths.trials_root()
    for tdir in sorted(p for p in troot.iterdir() if p.is_dir()) if troot.exists() else []:
        if not (tdir / "transcript.json").exists():
            continue
        try:
            audit, written = RUN.audit_trial_result(layout, tdir.name, force=force, judge=judge)
            summary["trials"][tdir.name] = {"written": written, "legacy": audit.legacy, "stakeholder_version": audit.stakeholder_version,
                                            "questions": len(audit.questions), "replies": len(audit.replies),
                                            "asked_before_implementing": audit.asked_before_implementing, "model": audit.model}
        except (AuditInputError, AuditLLMError) as e:
            summary["errors"].append({"trial_id": tdir.name, "error": f"{type(e).__name__}: {e}"})
    summary["llm_usage"] = judge.usage()
    return summary


@app.command("all")
def all_(force: bool = typer.Option(False, help="re-audit everything"),
         report_dir: Path | None = typer.Option(None, help="also write the aggregate report here (default: <workspace>/evidence/audit)"),
         stem: str | None = typer.Option(None, help="report file stem (default: <today>-task-sense)")) -> None:
    """Audit every registered scenario and every trial with a transcript, then write the report."""
    settings = get_settings()
    layout = layout_from_settings()
    judge = Judge()
    summary = audit_everything(layout, settings.registry_path, force=force, judge=judge)
    out_dir = report_dir or (settings.workspace / "evidence" / "audit")
    report = REP.build_report(layout, settings.registry_path, llm_usage=judge.usage())
    md_path, json_path = REP.write_report(report, out_dir, stem or f"{date.today().isoformat()}-task-sense")
    summary["report"] = {"markdown": str(md_path), "json": str(json_path)}
    _echo(summary)


@app.command()
def report(report_dir: Path | None = typer.Option(None, help="where to write (default: <workspace>/evidence/audit)"),
           stem: str | None = typer.Option(None, help="file stem (default: <today>-task-sense)")) -> None:
    """Aggregate the existing audit.json files into Markdown and JSON (no model calls)."""
    settings = get_settings()
    layout = layout_from_settings()
    rep = REP.build_report(layout, settings.registry_path)
    md_path, json_path = REP.write_report(rep, report_dir or (settings.workspace / "evidence" / "audit"), stem or f"{date.today().isoformat()}-task-sense")
    _echo({"markdown": str(md_path), "json": str(json_path), "trials": rep["trials"], "models": list(rep["models"])})
