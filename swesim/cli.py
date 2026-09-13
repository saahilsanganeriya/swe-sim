"""swesim command-line entry point.

Operator entry points (Temporal): `swesim create` (task creation), `swesim run-tasks` (graded runs), `swesim workflows ...`, and the
console (`swesim dashboard`). The stage commands (mine, judge, probe, draft, approve, compile, validate, run) are the building blocks
those workflows call; they are hidden and refuse to run directly unless SWESIM_DIRECT=1 (debugging only).
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


DIRECT_STAGE_HINT = {
    "mine": "swesim create --source <repo URL | swebench:A[:ids]>", "judge": "swesim create ...", "probe": "swesim create ...",
    "draft": "swesim create ...", "approve": "swesim create ... --approval-mode hold  +  swesim workflows approve", "compile": "swesim create ...",
    "validate": "swesim create ...", "run": "swesim run-tasks --scenario-ids <id> --models <ids> --conditions <c> --k <n>",
    "probe-tests": "swesim create ...",
}


def _direct_stage(name: str) -> None:
    """Task creation and graded runs go through Temporal (`swesim create`, `swesim run-tasks`, or the console), which gives
    every trial a launch-once identity, reconciliation and laptop-independent execution. The stage commands below are the
    building blocks those workflows call; running them directly is for debugging only and must be opted into."""
    if os.environ.get("SWESIM_DIRECT") == "1":
        return
    typer.echo(f"swesim {name}: direct stage execution is disabled. Use Temporal: `{DIRECT_STAGE_HINT.get(name, 'swesim create / swesim run-tasks')}` "
               f"(or the console's Create/Run pages). For debugging only, set SWESIM_DIRECT=1.", err=True)
    raise typer.Exit(code=3)


# ----------------------------------------------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------------------------------------------


@app.command(hidden=True)
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
    _direct_stage("mine")
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


@app.command(hidden=True)
def judge(
    candidates: Path,
    model: str = typer.Option(JUDGE_MODEL_DEFAULT, help="Judge model id (explicit; differs from the draft model)"),
    limit: int = typer.Option(20),
) -> None:
    """Per-gap underspecification judgement for each candidate row; writes <candidates>.judged.jsonl."""
    _direct_stage("judge")
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


@app.command(hidden=True)
def draft(
    judged: Path,
    instance_id: str,
    model: str = typer.Option(DRAFT_MODEL_DEFAULT, help="Draft model id (explicit; differs from the judge model)"),
) -> None:
    """Draft scenario.draft.json + assets for one judged candidate (natural + withheld briefs, ledger, controls)."""
    _direct_stage("draft")
    from swesim.stages.draft_spec import draft as draft_stage

    row = next((r for r in _rows(judged) if r["instance_id"] == instance_id), None)
    if row is None:
        typer.echo(f"{instance_id} not in {judged}", err=True)
        raise typer.Exit(code=2)
    scenario_id = instance_id.lower().replace("__", "--")
    path = draft_stage(row, row["judge"], model, WS.scenario_dir(scenario_id))
    typer.echo(str(path))


@app.command("probe-tests", hidden=True)
def probe_tests(candidates: Path, instance_id: str) -> None:
    """Re-probe one github-repo candidate on Modal: base vs gold test runs -> fail_to_pass / pass_to_pass."""
    _direct_stage("probe-tests")
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


@app.command(hidden=True)
def approve(
    scenario_id: str,
    auto: bool = typer.Option(False, "--auto", help="Unattended: code-owned gates decide; recorded as approved_by=auto-validators, review_needed=true"),
) -> None:
    """Freeze scenario.draft.json into scenario.json (human command, or --auto gates)."""
    _direct_stage("approve")
    from swesim.stages.approve import approve_scenario

    out = approve_scenario(WS, scenario_id, auto=auto)
    typer.echo(json.dumps(out))
    raise typer.Exit(code=0 if out.get("approved") else 1)


@app.command(hidden=True)
def compile(
    scenario_id: str,
    stakeholder_url: str = typer.Option("", envvar="SWESIM_STAKEHOLDER_URL", help="Compiled into allowed_hosts; empty = model hosts only"),
    condition: str = typer.Option("interactive"),
    resolve_digest: bool = typer.Option(True, "--resolve-digest/--no-resolve-digest"),
) -> None:
    """Render the Harbor task directories (full, hidden, interactive) for an approved scenario."""
    _direct_stage("compile")
    from swesim.stages.compile import compile_scenario

    spec = _load(scenario_id)
    res = compile_scenario(WS, spec, RunConfig.default(condition), _stakeholder_url(stakeholder_url), resolve_digest=resolve_digest)
    typer.echo(json.dumps({"task_dirs": {k: str(v) for k, v in res.task_dirs.items()}, "image_digest": res.image_digest, "compile_json": str(res.compile_json)}))


@app.command(hidden=True)
def validate(
    scenario_id: str,
    stakeholder_url: str = typer.Option("", envvar="SWESIM_STAKEHOLDER_URL"),
) -> None:
    """Run the control sweep on Modal (base, gold, controls, tamper exploits, reachability probe) -> validation.json."""
    _direct_stage("validate")
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


@app.command(hidden=True)
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
    _direct_stage("run")
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


@app.command(hidden=True)
def probe(
    judged: Path = typer.Option(..., "--judged", help="A .judged.jsonl (or .probed.jsonl) file"),
    instance: list[str] = typer.Option(None, "--instance", help="Restrict to these instance ids (repeatable)"),
    max_builds: int = typer.Option(3, help="How many candidates to probe on Modal in this call"),
) -> None:
    """Measure the contract offline (base vs gold on Modal) before drafting; writes <judged>.probed.jsonl."""
    _direct_stage("probe")
    from swesim.stages.mine import probe_candidates

    out = probe_candidates(WS, judged, instance or None, max_builds)
    typer.echo(str(out))


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


# -- two-workflow operator commands (create tasks / run tasks / inspect workflows) --------------------------
@app.command()
def create(source: list[str] = typer.Option(..., "--source", help="repeatable: a GitHub repo URL, or 'swebench:A' (pool A), or 'swebench:A:<id1>,<id2>'"),
           judge_model: str = typer.Option(JUDGE_MODEL_DEFAULT), draft_model: str = typer.Option(DRAFT_MODEL_DEFAULT),
           since: str = typer.Option("2026-06-01"), max_candidates: int = typer.Option(20), max_parallel: int = typer.Option(8),
           approval_mode: str = typer.Option("auto", help="auto | hold (review queue; approve with `swesim workflows approve`)"),
           stakeholder_url: str = typer.Option("", envvar="SWESIM_STAKEHOLDER_URL")) -> None:
    """Create tasks from many repos/pools: one CreateTasksWorkflow, one CandidateWorkflow per candidate."""
    import asyncio
    from swesim.workflows.client import start_create
    sources = []
    for s in source:
        if s.startswith("swebench:"):
            _, pool, *ids = s.split(":", 2)
            sources.append({"kind": "swebench-annotated", "pool": pool or "A", "limit": max_candidates, **({"instance_ids": ids[0].split(",")} if ids else {})})
        else:
            sources.append({"kind": "github-repo", "repo": s, "since": since, "max_candidates": max_candidates})
    typer.echo(asyncio.run(start_create({"sources": sources, "judge_model": judge_model, "draft_model": draft_model, "max_parallel": max_parallel,
                                         "approval_mode": approval_mode, "stakeholder_url": _stakeholder_url(stakeholder_url)})))

@app.command("run-tasks")
def run_tasks(scenario_ids: str = typer.Option(..., help="comma-separated scenario ids"), models: str = typer.Option(SOLVER_MODEL_DEFAULT),
              conditions: str = typer.Option("full,hidden,interactive"), k: int = typer.Option(3), purpose: str = typer.Option("comparison"),
              brief_profile: str = typer.Option("withheld"), stakeholder_model: str = typer.Option("", help="resolved stakeholder id (workspace/evidence/model-probe.json)"),
              attempt_offset: int = typer.Option(0), max_parallel_trials: int = typer.Option(1, help="1 on a laptop; raise on the VM"),
              stakeholder_url: str = typer.Option("", envvar="SWESIM_STAKEHOLDER_URL")) -> None:
    """Run selected task ids on selected models: one RunTasksWorkflow, TrialWorkflow children with REJECT_DUPLICATE ids."""
    import asyncio
    from swesim.workflows.client import start_run
    params = {"scenario_ids": scenario_ids.split(","), "models": models.split(","), "conditions": conditions.split(","), "k": k, "purpose": purpose,
              "brief_profile": brief_profile, "attempt_offset": attempt_offset, "max_parallel_trials": max_parallel_trials, "stakeholder_url": _stakeholder_url(stakeholder_url)}
    if stakeholder_model:
        params["stakeholder_model"] = stakeholder_model
    typer.echo(asyncio.run(start_run(params)))

workflows_app = typer.Typer(help="Inspect and steer Temporal workflows.")
app.add_typer(workflows_app, name="workflows")

@workflows_app.command("list")
def workflows_list(kind: str = typer.Argument("all", help="create | candidate | run | pipeline | matrix | trial | all"), limit: int = typer.Option(50)) -> None:
    import asyncio
    from swesim.workflows.client import list_runs
    typer.echo(json.dumps(asyncio.run(list_runs(kind, limit)), indent=2))

@workflows_app.command("progress")
def workflows_progress(workflow_id: str) -> None:
    import asyncio
    from swesim.workflows.client import progress
    typer.echo(json.dumps(asyncio.run(progress(workflow_id)), indent=2))

@workflows_app.command("cancel")
def workflows_cancel(workflow_id: str) -> None:
    import asyncio
    from swesim.workflows.client import cancel
    asyncio.run(cancel(workflow_id)); typer.echo(json.dumps({"cancelled": workflow_id}))

@workflows_app.command("approve")
def workflows_approve(workflow_id: str, scenario_id: str, spec_hash: str, decision: str = typer.Option("approve", help="approve | reject"), note: str = typer.Option("")) -> None:
    """Hold-mode reviewer decision; workflow_id may be the create-<hex> parent or create:<scenario_id>."""
    import asyncio
    from swesim.workflows.client import approve_candidate
    typer.echo(json.dumps({"signalled": asyncio.run(approve_candidate(workflow_id, scenario_id, spec_hash, decision, note)), "decision": decision}))


if __name__ == "__main__":
    app()
