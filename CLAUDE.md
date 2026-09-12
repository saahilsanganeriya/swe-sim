# swesim implementation conventions

Plan: `docs/superpowers/plans/2026-09-12-swesim-implementation.md`.
Checklist: `PLAN.md`.
Execute the working model in PLAN.md: an integrator owns models.py and cli.py; six parallel streams in separate worktrees own disjoint files; cloud phases run from integrated main. Fall back to one sequential stream if subagents are unavailable.
The repository-URL source discovers PRs itself. SWE-bench demonstrates the
interaction mechanism; the GitHub feature task demonstrates implementation
complexity. Diff size is a filter, never measured difficulty.

## Commands

- Bootstrap Task 1 dependencies, then `uv sync --extra dev --frozen`.
- Fast checks: `uv run --frozen --offline python -m pytest -q tests --ignore=tests/integration`.
- Acceptance: `bash scripts/verify.sh N`. It does not run cloud jobs or start servers.
- CLI: `uv run swesim --help`.
- Secrets: python-dotenv reads `SWESIM_ENV_FILE` or `~/Documents/.env`; never `export $(...)`, shell-source an untrusted file, or print secret values.
- Cloud: Harbor 0.23.0 and Modal 1.5.5 are project dependencies, not merely tools on PATH. `harbor run --max-retries 0 -e modal`.
- Stakeholder: `bash scripts/deploy_stakeholder.sh`; keep `.stakeholder_url` and credential files ignored.
- Temporal uses a persistent development server on the VM; UI, API and dashboard are reached through SSH forwards 8233, 7233 and 8080.

## Contracts

- Hidden and interactive share the selected brief profile; full uses natural plus stakeholder-visible facts. Each category-targeted removal has an original-issue excerpt and a withholding operation. Preserve and measure the natural profile; validate provenance/leaks before human approval.

- `reward.json` has finite numbers only. `success` is whole-contract correctness; `reward` is partial credit by fixed positive-weight requirement groups. Any regression sets reward to zero.
- No-op grading still runs tests. Missing base statuses cannot validate a task. Incorrect-exploit controls must apply and earn zero; infrastructure errors do not count as defeated attacks.
- Diff the submission against the untouched base, apply allowed source changes, then overlay hidden assertions. Preserve `/testbed` or `/app` so editable installs import submitted code.
- Start the trusted pytest launcher with the task's own interpreter and `-I`; import pytest before adding submitted roots. Require exact IDs, JUnit/terminal/exit agreement. Disagreement earns zero; reruns are diagnostics.
- Collector is root-owned, invoked with a cleared environment and absolute executables. Freeze in `finally`, including oversized/invalid submissions. Root-only `/var/swesim-collect` contains file artifacts and a JSON receipt. No git commands run during collection.
- Baseline and agent network policy are strict allowlists; separate verifier is offline. Agent toolchain is pinned and pre-baked. Compiler checks actual asset hashes and consumes the approved Dockerfile asset in both images.
- Stakeholder state is persisted before replies, request IDs bind to bodies, and exchange/freeze use the same lock. Author-only facts, oracle and hidden tests are excluded from its context. Fact labels are worker-only annotations.
- Pilot review mode is diff-only and explicitly unverified. No sandbox reviewer, source debugging or patch suggestions. Asking changes availability, never grader reward.
- Policy exports use actual agent observations. A generated service reply is not proof the policy received it. Keep audit annotations under evaluator-only; unadjudicated simulator traces are not labeled training-ready.
- Trial identity includes spec hash, condition, actual model/config/build hash, purpose and attempt index. Save frozen spec/config with each run. Never pool calibration and comparison.
- A per-trial systemd unit owns Harbor; the Temporal worker launches once and polls. Persisted workflow IDs alone do not make an activity exactly-once. Unknown launch state is reconciled, not retried blindly.
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
- Before the first LLM-dependent stage, probe each configured model id with one tiny request. If a provider or id is unavailable, fall back: judge and draft to `anthropic/claude-sonnet-5`, stakeholder to `anthropic/claude-sonnet-5`, solver stays `anthropic/claude-haiku-4-5-20251001`; record the substitution in `docs/ops.md` and in `difficulty.judged.model_id`.
- Nothing graded depends on the laptop's Docker: all builds and runs go through Harbor's Modal backend. Local Docker is only for optional smoke tests.
- `gh auth token` supplies the GitHub token for mining; write it to the ignored env file the VM scripts copy, never into tracked files.
- No step may wait for a human. If something genuinely needs a person (a provider outage, an exhausted quota, a missing permission), write it to `docs/ops.md` with the evidence and continue with independent work.

## Subagent model rule (user directive, 2026-09-12)
- Code work, design work, and review work run on `fable` (the session model): implementers, integrators, debuggers, task and final reviewers, anyone editing or judging code, specs, or plans. Pass `model: fable` explicitly.
- Reconnaissance and simple mechanical work run on `opus` or `sonnet`: reading files or docs to answer a question, searching the codebase or web, collecting logs and evidence, copying artifacts, formatting reports, running a documented command and reporting its output.
- Never dispatch `haiku` for anything on this project. When a task mixes recon with code changes, it is code work.

## Skills to use during execution (when the superpowers plugin is present)
`superpowers:subagent-driven-development` supplies the process (task briefs, a review gate per task, the ledger at `.superpowers/sdd/2026-09-12-swesim-implementation/progress.md`, rulings, a final whole-branch review). **Its serial-dispatch rule is overridden by PLAN.md's working model**: dispatch the six Phase 1 stream implementers concurrently, each in its own worktree with its disjoint file list; dispatch each stream's reviewer as soon as that stream reports; integrate serially (merge, contract test, CLI wiring); run the Phase 3 cloud paths concurrently (SWE-bench path, pypdf path, VM and Temporal, docs). Sequential execution is only the fallback when subagents are unavailable. Also use `superpowers:test-driven-development` inside every task, `superpowers:verification-before-completion` before checking any PLAN.md box, `superpowers:systematic-debugging` on any failing Modal run or test, and `superpowers:using-git-worktrees` for stream isolation. No skill is a prerequisite.
