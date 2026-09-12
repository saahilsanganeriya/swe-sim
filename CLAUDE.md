# swesim: working notes for Claude Code

## What this is
A pipeline that turns underspecified GitHub issues (SWE-bench's human-annotated
pool and any repository URL) into three-condition Harbor tasks with a simulated
stakeholder, graded in a separate offline verifier on Modal, orchestrated by
Temporal. Spec: docs/superpowers/specs/2026-09-12-swe-simulator-design.md.
Plan: docs/superpowers/plans/2026-09-12-swesim-implementation.md. Checklist: PLAN.md.

## Commands
- Install: `uv sync`; tools: `uv tool install 'harbor[modal]' && uv tool install modal`
- Secrets: `export $(grep -v '^#' ~/Documents/.env | xargs)` before any `harbor run` or LLM call. Never commit them.
- Tests: `uv run pytest -q` (fast, offline). Modal runs are never part of pytest.
- Verify milestone: `bash scripts/verify.sh <n>`
- CLI: `uv run swesim --help` (mine, judge, draft, approve, compile, validate, register, run, score, export, view, worker, pipeline, matrix)
- Harbor smoke recipe that works: `references/modal-smoke-task/` (read its README first)
- Stakeholder deploy: `bash scripts/deploy_stakeholder.sh` → URL in `.stakeholder_url`; export `SWESIM_STAKEHOLDER_URL=$(cat .stakeholder_url)` and `SWESIM_WORKER_SECRET` from `.env`
- Temporal locally: `temporal server start-dev --db-filename /tmp/swesim-temporal.db` (UI :8233), then `uv run swesim worker`

## Hard rules (from two adversarial reviews; do not relitigate)
- `reward.json` numeric only; `report.json` for everything else. `success` is binary and drives pass@k/pass^k; `reward` is partial credit by requirement groups; any PASS_TO_PASS failure sets reward to 0.
- Any `incorrect-exploit` tamper control that earns reward > 0 makes the scenario `invalid`.
- Collection = root `[[verifier.collect]]` hook (`user = "root"`, absolute paths): pkill agent, fresh `/var/swesim-collect` (700), tar, freeze, receipt. File artifacts only. Never trust `/logs/artifacts`.
- Strict allowlist baseline (model API + stakeholder host) in both `[environment]` and `[agent]`; `[verifier]` no-network, separate. Agent toolchain pre-baked (uv system-wide, `uv tool install mini-swe-agent` as user agent, `UV_OFFLINE=1`).
- Grader code under `swesim/verifier/` is stdlib-only (it runs inside the verifier image).
- Stakeholder LLM context = stakeholder-visible view only (no author-only facts, no oracle, no tests). Patience is code-owned. Asking is never rewarded or penalized.
- `TrialWorkflow` id = `trial_id(spec_hash, condition, run_config_hash, attempt_index)`; `run_trial_activity` retry maximum_attempts=1.
- Pool A only for SWE-bench; pytest-based repos only (no django, no sympy). github-repo demo repository: py-pdf/pypdf.

## Conventions
- Python 3.12, pydantic v2 `extra="forbid"` models, typer CLI, small focused modules (see plan file structure).
- TDD: write the failing test from the plan first, run it, implement, run, commit. Commit message ends with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Push after every milestone.
- Workspace outputs live under `workspace/` (gitignored). Tracked deliverables: `examples/`, `registry.json`, docs.

## Pitfalls already hit
- Harbor directory artifacts silently truncate on absolute symlinks: use the collect-hook tar.
- Modal's allowlist blocks apt over HTTP even when mirrors are listed: never rely on network at agent setup.
- Harbor runs `uv` bootstrap as the agent user in a non-login shell: install uv at `/usr/local/bin`, `chown -R agent:agent /home/agent`, set `UV_CACHE_DIR=/tmp/uv-cache`.
- The verifier runs as root; chown the applied tree to `tester` before running tests.
- `harbor run --agent module.path:Class` is the non-deprecated way to load `PatchAgent`; the repo root must be on PYTHONPATH.
- SWE-bench images keep the repo at `/testbed` with a conda env `testbed`; run tests via `source /opt/miniconda3/bin/activate testbed && python -I -m pytest -rA ...`.
