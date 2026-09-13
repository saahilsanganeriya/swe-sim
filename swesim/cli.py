"""swesim command-line entry point.

Pipeline: mine -> judge -> draft -> approve -> compile -> validate -> register -> run -> score -> export.
Orchestrated variants: pipeline / matrix / worker (Temporal). Viewing: dashboard.
See docs/superpowers/specs/2026-09-12-swe-simulator-design.md, section 7, and docs/architecture.md.

Every stage import is lazy so `swesim --help` works without optional services, and every command is a thin
adapter over a stage function (the stage functions are the tested surface; see tests/test_contracts.py).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from swesim.models import RunConfig, ScenarioSpec
from swesim.paths import Workspace

app = typer.Typer(no_args_is_help=True, help=__doc__)

WS = Workspace(os.environ.get("SWESIM_WORKSPACE", "workspace"))

JUDGE_MODEL_DEFAULT = "openai/gpt-5.5"
DRAFT_MODEL_DEFAULT = "anthropic/claude-sonnet-5"
SOLVER_MODEL_DEFAULT = "anthropic/claude-haiku-4-5-20251001"


def _load(scenario_id: str, draft: bool = False) -> ScenarioSpec:
    """Load the approved scenario.json (or scenario.draft.json when draft=True)."""
    name = "scenario.draft.json" if draft else "scenario.json"
    path = WS.scenario_dir(scenario_id) / name
    if not path.exists():
        typer.echo(f"swesim: {path} not found", err=True)
        raise typer.Exit(code=2)
    return ScenarioSpec.model_validate_json(path.read_text())


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _stakeholder_url(value: str) -> str:
    """Resolve the stakeholder URL: explicit flag/env wins, else the ignored .stakeholder_url file, else empty."""
    if value:
        return value.rstrip("/")
    p = Path(".stakeholder_url")
    return p.read_text().strip().rstrip("/") if p.exists() else ""


def _worker_secret(value: str) -> str:
    """Resolve the worker secret from the flag/env or the dotenv file (never printed)."""
    if value:
        return value
    from dotenv import dotenv_values

    env_file = os.environ.get("SWESIM_ENV_FILE", str(Path.home() / "Documents/.env"))
    secret = dotenv_values(env_file).get("SWESIM_WORKER_SECRET") or ""
    if not secret:
        typer.echo("swesim: SWESIM_WORKER_SECRET not set (flag, env, or dotenv file)", err=True)
        raise typer.Exit(code=2)
    return secret


# ----------------------------------------------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------------------------------------------


@app.command()
def mine(
    source: str = typer.Option("swebench-annotated", help="swebench-annotated | github-repo"),
    pool: str = typer.Option("A", help="SWE-bench annotated pool (A = underspecified >= 2, valid tests)"),
    instance: list[str] = typer.Option(None, help="Restrict to these SWE-bench instance ids (repeatable)"),
    no_threads: bool = typer.Option(False, help="Skip GitHub issue/PR thread fetching"),
    repo: str = typer.Option("", help="github-repo: repository URL or owner/repo"),
    pr: int = typer.Option(0, help="github-repo: optional PR override (recorded as repo_only=false)"),
    since: str = typer.Option("2026-06-01", help="github-repo: only PRs merged on/after this date"),
    max_prs: int = typer.Option(400, help="github-repo: how many merged PRs to page through"),
    max_builds: int = typer.Option(3, help="github-repo: how many candidates to probe on Modal"),
    kind: str = typer.Option("feature", help="github-repo: feature | bugfix band"),
) -> None:
    """Discover candidates: SWE-bench annotated pool A, or any GitHub repository URL (issues + linked merged PRs)."""
    from swesim.stages import mine as mine_stage

    if source == "swebench-annotated":
        out = mine_stage.mine_swebench(WS, pool, instance or None, pytest_only=True, fetch_threads=not no_threads)
    elif source == "github-repo":
        if not repo:
            typer.echo("swesim mine --source github-repo requires --repo", err=True)
            raise typer.Exit(code=2)
        out = mine_stage.mine_github_repo(WS, repo, pr or None, since=since, max_prs=max_prs, max_builds=max_builds, kind=kind)
    else:
        typer.echo(f"unknown source {source}", err=True)
        raise typer.Exit(code=2)
    typer.echo(str(out))


@app.command()
def judge(
    candidates: Path,
    model: str = typer.Option(JUDGE_MODEL_DEFAULT, help="Judge model id (explicit; differs from the draft model)"),
    limit: int = typer.Option(20),
) -> None:
    """Per-gap underspecification judgement for each candidate row; writes <candidates>.judged.jsonl."""
    from swesim.stages.judge import judge_candidate

    rows = _rows(candidates)[:limit]
    out = candidates.with_suffix(".judged.jsonl")
    tmp = out.with_suffix(".jsonl.tmp")
    kept = errors = 0
    with tmp.open("w") as fh:
        for row in rows:
            try:
                row["judge"] = judge_candidate(row, model)
            except Exception as e:  # one bad row (provider hiccup, oversize payload) must not abort or wipe the file
                row["judge"] = {"keep": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
                errors += 1
            kept += int(bool(row["judge"].get("keep")))
            fh.write(json.dumps(row) + "\n")
    os.replace(tmp, out)  # the previous judged file survives a crash mid-run
    typer.echo(json.dumps({"judged": out.as_posix(), "rows": len(rows), "keep": kept, "errors": errors}))


@app.command("judge-calibrate")
def judge_calibrate(
    n: int = typer.Option(150),
    model: str = typer.Option(JUDGE_MODEL_DEFAULT),
    seed: int = typer.Option(0),
) -> None:
    """Agreement of the judge with the human SWE-bench Verified labels (>= 2 threshold) on a random sample."""
    from swesim.sources import swebench_annotated as sb
    from swesim.stages.judge import calibrate

    rows_by_id = {}
    for r in sb._load_dataset():
        rows_by_id[r["instance_id"]] = {
            **r,
            "fail_to_pass": json.loads(r["FAIL_TO_PASS"]) if isinstance(r.get("FAIL_TO_PASS"), str) else r.get("FAIL_TO_PASS", []),
            "pass_to_pass": json.loads(r["PASS_TO_PASS"]) if isinstance(r.get("PASS_TO_PASS"), str) else r.get("PASS_TO_PASS", []),
            "image": sb.image_for(r["instance_id"]),
            "annotation": {},
            "thread": {},
        }
    result = calibrate(Path("data/annotations/ensembled_annotations_public.csv"), rows_by_id, model, n, seed)
    typer.echo(json.dumps({k: v for k, v in result.items() if k != "per_instance"}, indent=1))


@app.command()
def draft(
    judged: Path,
    instance_id: str,
    model: str = typer.Option(DRAFT_MODEL_DEFAULT, help="Draft model id (explicit; differs from the judge model)"),
) -> None:
    """Draft scenario.draft.json + assets for one judged candidate (natural + withheld briefs, ledger, controls)."""
    from swesim.stages.draft_spec import draft as draft_stage

    row = next((r for r in _rows(judged) if r["instance_id"] == instance_id), None)
    if row is None:
        typer.echo(f"{instance_id} not in {judged}", err=True)
        raise typer.Exit(code=2)
    scenario_id = instance_id.lower().replace("__", "--")
    path = draft_stage(row, row["judge"], model, WS.scenario_dir(scenario_id))
    typer.echo(str(path))


@app.command("probe-tests")
def probe_tests(candidates: Path, instance_id: str) -> None:
    """Re-probe one github-repo candidate on Modal: base vs gold test runs -> fail_to_pass / pass_to_pass."""
    from swesim.sources import github_repo

    row = next((r for r in _rows(candidates) if r["instance_id"] == instance_id), None)
    if row is None:
        typer.echo(f"{instance_id} not in {candidates}", err=True)
        raise typer.Exit(code=2)
    row.update(github_repo.build_recipe(row["repo"], row["base_commit"]))
    typer.echo(json.dumps(github_repo.probe_tests(row, WS)))


# ----------------------------------------------------------------------------------------------------------
# Approval, compile, validate
# ----------------------------------------------------------------------------------------------------------


@app.command()
def approve(
    scenario_id: str,
    auto: bool = typer.Option(False, "--auto", help="Unattended: code-owned gates decide; recorded as approved_by=auto-validators, review_needed=true"),
) -> None:
    """Freeze scenario.draft.json into scenario.json (human command, or --auto gates)."""
    from swesim.stages.approve import approve_scenario

    out = approve_scenario(WS, scenario_id, auto=auto)
    typer.echo(json.dumps(out))
    raise typer.Exit(code=0 if out.get("approved") else 1)


@app.command()
def compile(
    scenario_id: str,
    stakeholder_url: str = typer.Option("", envvar="SWESIM_STAKEHOLDER_URL", help="Compiled into allowed_hosts; empty = model hosts only"),
    condition: str = typer.Option("interactive"),
    resolve_digest: bool = typer.Option(True, "--resolve-digest/--no-resolve-digest"),
) -> None:
    """Render the Harbor task directories (full, hidden, interactive) for an approved scenario."""
    from swesim.stages.compile import compile_scenario

    spec = _load(scenario_id)
    res = compile_scenario(WS, spec, RunConfig.default(condition), _stakeholder_url(stakeholder_url), resolve_digest=resolve_digest)
    typer.echo(json.dumps({"task_dirs": {k: str(v) for k, v in res.task_dirs.items()}, "image_digest": res.image_digest, "compile_json": str(res.compile_json)}))


@app.command()
def validate(
    scenario_id: str,
    stakeholder_url: str = typer.Option("", envvar="SWESIM_STAKEHOLDER_URL"),
) -> None:
    """Run the control sweep on Modal (base, gold, controls, tamper exploits, reachability probe) -> validation.json."""
    from swesim.stages.validate import validate_scenario

    spec = _load(scenario_id)
    out = validate_scenario(WS, spec, RunConfig.default("full"), _stakeholder_url(stakeholder_url))
    typer.echo(json.dumps({"status": out["status"], "failures": out.get("failures", [])}))
    raise typer.Exit(code=0 if out["status"] == "valid" else 1)


# ----------------------------------------------------------------------------------------------------------
# Registry, run, score, export
# ----------------------------------------------------------------------------------------------------------


@app.command()
def register(scenario_id: str) -> None:
    """Append/update registry.json and workspace/dataset.json for a validated scenario."""
    from swesim.stages.register import register_scenario

    typer.echo(json.dumps(register_scenario(WS, _load(scenario_id))))


@app.command()
def run(
    scenario_id: str,
    condition: str = typer.Option("interactive", help="full | hidden | interactive"),
    attempt: int = typer.Option(0, help="Attempt index (part of the trial id; retries use a fresh index)"),
    model: str = typer.Option(SOLVER_MODEL_DEFAULT),
    brief_profile: str = typer.Option("withheld", help="withheld | natural (hidden and interactive share it)"),
    purpose: str = typer.Option("demonstration", help="calibration | comparison | demonstration"),
    stakeholder_url: str = typer.Option("", envvar="SWESIM_STAKEHOLDER_URL"),
    worker_secret: str = typer.Option("", envvar="SWESIM_WORKER_SECRET", show_default=False),
) -> None:
    """Run one graded trial on Modal through Harbor and bind its stakeholder transcript (writes workspace/trials/<trial_id>/)."""
    from swesim.stages.run import run_trial

    spec = _load(scenario_id)
    compile_json = WS.scenario_dir(scenario_id) / "compile.json"
    compiled_hash = json.loads(compile_json.read_text()).get("compiled_hash") if compile_json.exists() else None
    rc = RunConfig(condition=condition, model=model, brief_profile=brief_profile, purpose=purpose, compiled_hash=compiled_hash)
    rec = run_trial(WS, spec, rc, attempt, _stakeholder_url(stakeholder_url), _worker_secret(worker_secret))
    typer.echo(json.dumps({"trial_id": rec.trial_id, "status": rec.status, "reward": rec.reward}))


@app.command()
def score(scenario_id: str, k: int = typer.Option(3)) -> None:
    """Per (condition, model, run_config_hash) pass@k, pass^k, outcome counts -> scores.json."""
    from swesim.stages.score import score_scenario

    typer.echo(str(score_scenario(WS, scenario_id, k)))


@app.command()
def export(scenario_id: str) -> None:
    """Episode exports (policy_visible / evaluator_only / lineage) -> workspace/exports/<trial_id>.episode.json."""
    from swesim.stages.export import export_scenario

    typer.echo(json.dumps([str(p) for p in export_scenario(WS, scenario_id)]))


@app.command()
def dashboard(host: str = typer.Option("127.0.0.1"), port: int = typer.Option(8080)) -> None:
    """Runs table and episode pages (conversation, verifier outcome) over the workspace."""
    import uvicorn

    os.environ.setdefault("SWESIM_WORKSPACE", str(WS.root))
    os.environ.setdefault("SWESIM_REGISTRY", str(WS.registry_path))
    uvicorn.run("swesim.dashboard.app:app", host=host, port=port)


# ----------------------------------------------------------------------------------------------------------
# Temporal
# ----------------------------------------------------------------------------------------------------------


@app.command()
def worker(launcher: str = typer.Option("systemd", help="systemd (production, VM) | subprocess (local smoke only, NOT production)")) -> None:
    """Temporal worker on task queue `swesim` (on the VM: swesim-worker.service)."""
    import asyncio

    from swesim.workflows.worker import main

    asyncio.run(main(launcher))


@app.command()
def pipeline(
    source: str = typer.Option("swebench-annotated", help="swebench-annotated | github-repo"),
    repo: str = typer.Option(""),
    pr: int = typer.Option(0),
    judge_model: str = typer.Option(JUDGE_MODEL_DEFAULT),
    draft_model: str = typer.Option(DRAFT_MODEL_DEFAULT),
    model: str = typer.Option(SOLVER_MODEL_DEFAULT),
    approval_mode: str = typer.Option("auto", help="auto (unattended, default) | signal (wait for approve-signal)"),
    conditions: str = typer.Option("interactive"),
    k: int = typer.Option(1),
    max_scenarios: int = typer.Option(1),
    max_candidates_tried: int = typer.Option(3),
    stakeholder_url: str = typer.Option("", envvar="SWESIM_STAKEHOLDER_URL"),
) -> None:
    """Start a PipelineWorkflow on Temporal (mine -> judge -> draft -> approve -> compile -> validate -> register -> trial -> score/export)."""
    import asyncio

    from swesim.workflows.client import start_pipeline

    params = {
        "source": source, "repo": repo, "judge_model": judge_model, "draft_model": draft_model, "models": [model],
        "conditions": conditions.split(","), "k": k, "approval_mode": approval_mode, "max_scenarios": max_scenarios,
        "max_candidates_tried": max_candidates_tried, "stakeholder_url": _stakeholder_url(stakeholder_url),
    }
    if pr:
        params["pr"] = pr
    typer.echo(asyncio.run(start_pipeline(params)))


@app.command("approve-signal")
def approve_signal(workflow_id: str, scenario_id: str, spec_hash: str) -> None:
    """Signal a PipelineWorkflow started with --approval-mode signal."""
    import asyncio

    from swesim.workflows.client import approve as approve_wf

    asyncio.run(approve_wf(workflow_id, scenario_id, spec_hash))
    typer.echo(json.dumps({"signalled": workflow_id, "scenario_id": scenario_id, "spec_hash": spec_hash}))


@app.command("pipeline-progress")
def pipeline_progress(workflow_id: str) -> None:
    """Query a PipelineWorkflow's progress."""
    import asyncio

    from swesim.workflows.client import progress

    typer.echo(json.dumps(asyncio.run(progress(workflow_id))))


@app.command()
def matrix(
    scenario_id: str,
    models: str = typer.Option(SOLVER_MODEL_DEFAULT, help="Comma-separated solver model ids"),
    conditions: str = typer.Option("full,hidden,interactive"),
    k: int = typer.Option(3),
    attempt_offset: int = typer.Option(0, help="First attempt index; manual retries use a fresh index, never the same trial id"),
    purpose: str = typer.Option("comparison"),
    brief_profile: str = typer.Option("withheld"),
    stakeholder_url: str = typer.Option("", envvar="SWESIM_STAKEHOLDER_URL"),
) -> None:
    """Start a RunMatrixWorkflow: serial TrialWorkflow children with REJECT_DUPLICATE ids."""
    import asyncio

    from swesim.workflows.activities import plan_trials
    from swesim.workflows.client import start_matrix

    p = plan_trials({
        "scenario_id": scenario_id, "models": models.split(","), "conditions": conditions.split(","), "k": k,
        "attempt_offset": attempt_offset, "purpose": purpose, "brief_profile": brief_profile,
        "stakeholder_url": _stakeholder_url(stakeholder_url),
    })
    typer.echo(asyncio.run(start_matrix(p)))


if __name__ == "__main__":
    app()
