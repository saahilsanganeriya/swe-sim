# swesim implementation conventions

Plan: `docs/superpowers/plans/2026-09-12-swesim-implementation.md`.
Checklist: `PLAN.md`.
Execute the working model in PLAN.md: an integrator owns models.py and cli.py; six parallel streams in separate worktrees own disjoint files; cloud phases run from integrated main. Fall back to one sequential stream if subagents are unavailable.
The repository-URL source discovers PRs itself. SWE-bench demonstrates the
interaction mechanism; the GitHub feature task demonstrates implementation
complexity. Diff size is a filter, never measured difficulty.

## Commands

- Fresh checkout: `uv sync --extra dev --frozen`; `make check` = that sync plus the offline suite.
- Fast checks: `uv run --frozen --offline python -m pytest -q tests --ignore=tests/integration` (the integration tests need credentials and are not part of the offline suite).
- Acceptance: `bash scripts/verify.sh N`. It does not run cloud jobs or start servers.
- CLI: `uv run --frozen swesim --help`.
- Environment file: python-dotenv reads the file named by `SWESIM_ENV_FILE` (default `./.env`, git-ignored, copied from the generated `.env.example`); the process environment wins over the file. `swesim.settings` is the only reader (`swesim.settings.secret()` in code; no `os.environ` reads elsewhere). Never `export $(...)`, shell-source a dotenv file, or print secret values; pass `SWESIM_ENV_FILE=...` in a command's environment when a command needs credentials.
- Cloud targets (`Makefile`, all read `SWESIM_ENV_FILE`): `make cloud-bootstrap PROJECT= REGION=` (APIs, state bucket, tfvars, apply, secrets, `bootstrap-namespace`, image, pools/console, migrate, verify), `make deploy SHA=<sha>` (open-executions guard → build → pin → apply `FORCE_PIN=1` → `write-pin` → migrate → `verify-cloud`), `make plan | apply | outputs | push-secrets | verify-cloud | migrate | pull | pull-pin | push-pin | gh-variables | cloud-destroy [PURGE=1]`. Operating procedures are in `docs/runbook.md`; setup in `docs/setup.md`.
- Harbor 0.23.0 and Modal 1.5.5 are project dependencies, not merely tools on PATH; all builds and graded runs go through Harbor's Modal backend. The worker image (`Dockerfile.worker`) must carry every binary the stages shell out to (`git`, `gh`).
- Stakeholder: `bash scripts/deploy_stakeholder.sh` (or `make deploy-stakeholder`); keep `.stakeholder_url` and credential files ignored.

## Contracts

- Hidden and interactive share the selected brief profile; full uses natural plus stakeholder-visible facts. Each category-targeted removal has an original-issue excerpt and a withholding operation. Preserve and measure the natural profile; validate provenance/leaks before human approval.

- `reward.json` has finite numbers only. `success` is whole-contract correctness; `reward` is partial credit by fixed positive-weight requirement groups. Any regression sets reward to zero.
- No-op grading still runs tests. Missing base statuses cannot validate a task. Incorrect-exploit controls must apply and earn zero; infrastructure errors do not count as defeated attacks.
- Diff the submission against the untouched base, apply allowed source changes, then overlay hidden assertions. Preserve `/testbed` or `/app` so editable installs import submitted code.
- Start the trusted pytest launcher with the task's own interpreter and `-I`; import pytest before adding submitted roots. Require exact IDs, JUnit/terminal/exit agreement. Disagreement earns zero; reruns are diagnostics.
- Collector is root-owned, invoked with a cleared environment and absolute executables. Freeze in `finally`, including oversized/invalid submissions. Root-only `/var/swesim-collect` contains file artifacts and a JSON receipt. No git commands run during collection.
- Baseline and agent network policy are strict allowlists; separate verifier is offline. Agent toolchain is pinned and pre-baked. Compiler checks actual asset hashes and consumes the approved Dockerfile asset in both images.
- Stakeholder state is persisted before replies, request IDs bind to bodies, and exchange/freeze use the same lock. Author-only facts, oracle and hidden tests are excluded from its context. Fact labels are worker-only annotations.
- The stakeholder speaks as the reporter: behavior wanted and observed, never tests, verification steps, identifiers outside the brief, or a pick among the agent's options; replies are regenerated and redacted against hidden-test identifiers (`forbidden_terms`). Review is behavior-only by default: `confirm-behavior` sends a plain-language description of the resulting behavior and the stakeholder confirms or names an unmet requirement. Diff review (`request-review`) exists only under `review_mode: diff-only`, is explicitly unverified, and has no sandbox reviewer, source debugging or patch suggestions. Asking changes availability, never grader reward.
- Policy exports use actual agent observations. A generated service reply is not proof the policy received it. Keep audit annotations under evaluator-only; unadjudicated simulator traces are not labeled training-ready.
- Trial identity includes spec hash, condition, actual model/config/build hash, purpose and attempt index. Save frozen spec/config with each run. Never pool calibration and comparison.
- One Cloud Run Job per trial (`swesim-trial-<trial_id>`) owns Harbor; the Temporal worker launches once and polls. Persisted workflow IDs alone do not make an activity exactly-once. Unknown launch state is reconciled, not retried blindly. (The VM backend's per-trial systemd unit is the labelled legacy fallback, `origin=vm-systemd`.)
- Approval is unattended by default: `swesim approve --auto <id>` lets the code-owned gates (schema, assets, judge acceptance, on-request fact linked to F2P, withholding leak, controls) decide and records `approved_by: auto-validators, review_needed: true`. Never hand-edit a draft to pass a gate; re-draft once or take the next candidate. Supervised human approval remains available but is not used overnight.

## Execution and evidence

Use tests before implementation for the substantive contracts in the plan.
Execute Task 12 before Task 11. Do not duplicate the UI surfaces: Temporal
shows workflow state; the episode page shows the conversation and outcomes.
Commit coherent tasks; push milestones. Keep `workspace/` ignored and
`examples/`, `registry.json`, docs tracked. Do not stage other agents' edits.
Commit messages end with the attribution line this environment adds by default (`Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`); do not add other attributions.

The prior smoke used `/logs/artifacts` and did not test the hardened root
collector. Preserve that distinction. A passing unit test is not a Modal
integration result. Record actual failures and unresolved limits in
`docs/ops.md`; any incompatible spec requirement belongs in the plan review's
spec-issues list until the owner revises it.

Known residual: submitted code runs inside pytest and can attempt to forge
its reports. These checks are tested defenses, not a proof against every
attack. New feature tasks need independent behavioral controls and measured
outcomes; 200–1,500 source lines does not guarantee a capability gradient.

## Unattended-run rules
- Before the first LLM-dependent stage, `probe_models_activity` probes each configured model id with one tiny request and resolves every role along the chains in `swesim/roles.py` `ROLE_CHAINS` (configured id first; the chains and `TIERS` are the brief's ids verbatim — edit them there, never in a doc). Judge and draft must resolve to different ids: on a collision advance the draft chain, or the judge chain when the draft chain is exhausted, and record the reasoning. The solver is not a role and is never substituted. The resolved ids go to `evidence/model-probe.json` in the workspace (`Layout.model_probe`), to every later stage (every trial's `RunConfig.stakeholder.model_id` is the resolved stakeholder model), to `difficulty.judged.model_id`, and any substitution is recorded in `docs/ops.md`.
- Nothing graded depends on the laptop's Docker: all builds and runs go through Harbor's Modal backend. Local Docker is only for optional smoke tests.
- `gh auth token` supplies the GitHub token for mining; write it to the ignored env file (`GH_TOKEN`), never into tracked files. Hosted workers read it from Secret Manager (`make push-secrets`).
- No step may wait for a human. If something genuinely needs a person (a provider outage, an exhausted quota, a missing permission), write it to `docs/ops.md` with the evidence and continue with independent work.

## Temporal
- Temporal Cloud: namespace `swesim.abcde`, endpoint `us-central1.gcp.api.temporal.io:7233`, API-key auth (`TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_API_KEY` in the env file; the Makefile's `verify-cloud` proves the connection and the pollers). There is no development server in the hosted path.
- Four task queues, one Cloud Run worker pool each (`swesim/workflows/queues.py`): `swesim-authoring` (LLM and discovery activities, workflow tasks), `swesim-build` (probe, compile, control validation), `swesim-trials` (launch/await/cancel/cleanup of trial Jobs), `swesim-control` (index, cost/spend, frontier gate, plan pages, audits, operator actions). Every activity is routed in `QUEUE_OF`; an unrouted activity fails the worker at start-up.
- Six Keyword search attributes (`swesim_scenario_id`, `swesim_model`, `swesim_condition`, `swesim_status`, `swesim_version`, `swesim_purpose`) are registered by `make bootstrap-namespace`; Temporal Cloud needs the printed `tcld` step once.
- Deploys are guarded by `swesim workflows open-types`; a command-sequence change while executions are open needs a `workflow.patched` marker, and `OVERRIDE_OPEN_EXECUTIONS=1` is legitimate only then (`docs/runbook.md`, "Deploy a commit").

## Execution rule (user directive, 2026-09-13)
- Task creation and graded runs go through Temporal only: `swesim create`, `swesim run-tasks`, `swesim workflows ...`, or the console's Create/Run pages, executed by the Cloud Run worker pools. Never launch trials or validations with the stage commands directly. The stage commands (`mine`, `judge`, `probe`, `draft`, `approve`, `compile`, `validate`, `run`) are the building blocks the workflows call; they are hidden from `--help` and refuse to run unless `SWESIM_DIRECT=1` is set, which is for debugging a single stage only. The 28 laptop trials recorded on 2026-09-12/13 predate this rule and are labelled as CLI-run demonstrations in the docs.

## Subagent model rule (user directive, 2026-09-12)
- Code work, design work, and review work run on `fable` (the session model): implementers, integrators, debuggers, task and final reviewers, anyone editing or judging code, specs, or plans. Pass `model: fable` explicitly.
- Reconnaissance and simple mechanical work run on `opus` or `sonnet`: reading files or docs to answer a question, searching the codebase or web, collecting logs and evidence, copying artifacts, formatting reports, running a documented command and reporting its output.
- Never dispatch `haiku` for anything on this project. When a task mixes recon with code changes, it is code work.

## Skills to use during execution (when the superpowers plugin is present)
`superpowers:subagent-driven-development` supplies the process (task briefs, a review gate per task, the ledger at `.superpowers/sdd/2026-09-12-swesim-implementation/progress.md`, rulings, a final whole-branch review). **Its serial-dispatch rule is overridden by PLAN.md's working model**: dispatch the six Phase 1 stream implementers concurrently, each in its own worktree with its disjoint file list; dispatch each stream's reviewer as soon as that stream reports; integrate serially (merge, contract test, CLI wiring); run the Phase 3 cloud paths concurrently (SWE-bench path, pypdf path, VM and Temporal, docs). Sequential execution is only the fallback when subagents are unavailable. Also use `superpowers:test-driven-development` inside every task, `superpowers:verification-before-completion` before checking any PLAN.md box, `superpowers:systematic-debugging` on any failing Modal run or test, and `superpowers:using-git-worktrees` for stream isolation. No skill is a prerequisite.
