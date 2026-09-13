# Overnight working model — 2026-09-13 (until 08:00 PDT)

Companion to the brief `docs/superpowers/specs/2026-09-13-hosted-pipeline-brief.md` (binding on *what*). This document is
binding on *how*: roles, design bar, frozen interfaces, streams, cadence, verification. The integrator follows it; every
stream brief links to it.

## 1. Roles

- **Integrator** (this session, `fable`): freezes interfaces, dispatches streams, merges, runs contract tests, deploys,
  rules on conflicts, keeps the ledger (`.superpowers/sdd/2026-09-13-hosted-pipeline/progress.md`) and pushes `main`.
- **Stream implementers** (`fable`, one per stream, own worktree, disjoint file ownership, no subagents of their own):
  implement from a brief file, tests first for every contract, commit on their branch, write a report file, return
  status + commits + one-line test summary + concerns.
- **Stream reviewers** (`fable`): a task review per stream report (spec compliance + code quality against §2), then one
  whole-branch review before the final push.
- **Recon and mechanical helpers** (`opus`/`sonnet`): reading docs, collecting evidence, formatting, running documented
  commands. Never `haiku`.
- **Operator stream** (`fable`, later in the night): submits the creation and run workflows through the console and the
  CLI, watches the funnel and spend, tunes bands and concurrency from data, records everything in the ledger.

Parallelism is the point: streams run concurrently in worktrees; the integrator never implements a stream itself while
streams are running, it integrates. Dispatch prompts carry only the stream brief path, the frozen interfaces, the global
constraints and the report path; never the session history.

## 2. Design bar (what "well designed" means here)

**Code.** Minimal is not the requirement; correct, clear and durable is.
- One responsibility per module; module docstrings state the contract; public functions have typed signatures and short
  docstrings that say what is guaranteed, not what the code does.
- Errors are classified at the boundary: invalid input and business rejections raise typed, non-retryable errors;
  transient failures propagate as retryable; nothing is swallowed. Every `except` names a type and a reason.
- No hidden state: configuration enters through typed settings objects built from the environment once; no module reads
  `os.environ` in the middle of a function.
- Idempotency by construction: every activity can run twice; every write is write-then-rename or an upsert keyed by id.
- Tests before implementation for every contract in the interfaces spec; tests assert behaviour, not strings.
- No cleverness that a reviewer would need explained; a comment answers "why", never "what".

**Data models.** Every entity has exactly one Pydantic model (strict, `extra="forbid"`), one Postgres table, one bucket
location, and they agree field for field.
- Ids: content-addressed where the artifact is immutable (spec hash, compiled hash, trial id), ULIDs where it is not
  (workflows already have Temporal ids). Versions are explicit integers, never implied by timestamps.
- Every row carries provenance: `created_at`, `updated_at`, `workflow_id`, `run_id`, `worker_image_digest`, `origin`.
- Nullable means "unknown", never "false" or "not applicable"; enums are enums; money is `numeric(12,6)` USD; times are
  UTC `timestamptz`.
- Migrations are forward-only Alembic revisions; the index is rebuildable from the bucket, so a bad migration is fixed by
  a new revision plus a rebuild, never by editing data by hand.
- The bucket layout is part of the data model (brief §2.3): paths are derived by one function, never assembled ad hoc.

**Decisions.** Every non-obvious choice is written down where it is made: a `Ruling:` line in the ledger (what, why, cost
if wrong) for the integrator, a `decisions` section in each stream report for implementers. The right decision is the one
that keeps the record honest and the reward trustworthy, then the one that keeps the operator's surface simple, then the
one that is cheapest.

## 3. Phase 0 — freeze the interfaces (integrator, first 45 minutes)

Published as code stubs with tests plus `docs/superpowers/specs/2026-09-13-interfaces.md`; streams start only after this
commit is on `main`:

1. `swesim/storage/layout.py` — the bucket layout (brief §2.3) as one `Layout` class: `scenario_dir(id, version)`,
   `task_dir(id, version, condition)`, `trial_dir(trial_id)`, `export_path`, `candidates_dir(source_key, date)`,
   `evidence_dir(kind, date)`, `calibration_dir(judge, date)`; `Workspace` becomes a thin adapter over it. Tests pin
   every path string.
2. `swesim/index/models.py` + `swesim/index/schema.py` — Pydantic rows and SQLAlchemy tables for `scenarios`,
   `candidates`, `trials`, `exchanges`, `scores`, `workflows`, `costs`, `operator_actions`; `swesim/index/store.py` with
   `upsert_*` and `rebuild_from_layout()`; Alembic initialised.
3. `swesim/workflows/launchers/base.py` — the launcher protocol (`launch_once`, `read_evidence`, `decide_launch`,
   `cancel`) with the systemd implementation moved behind it unchanged; `cloudrun.py` is a stream deliverable.
4. `swesim/workflows/queues.py` — queue names and the activity→queue map; `swesim/workflows/retries.py` — retry classes.
5. `swesim/workflows/search_attributes.py` — the six keys and the setter used at every workflow start.
6. `swesim/costs.py` — `CostRecord` and the provider price table with a `price_source` field.
7. `swesim/dashboard/data.py` — the console's data-access interface (functions returning the Pydantic rows above),
   implemented first over the files, then over Postgres by the storage stream; the console stream codes against it.
8. `swesim/settings.py` — one typed `Settings` (env file path, bucket, database URL, Temporal, IAP evaluators, spend
   cap), replacing scattered `os.environ` reads.

## 4. Streams (parallel worktrees; owner files disjoint)

| Stream | Owns | Depends on | Deliverable check |
|---|---|---|---|
| infra | `infra/**`, `scripts/cloud/**`, `Makefile` cloud targets | — | `terraform apply` twice, second clean; outputs file |
| image+deploy | `Dockerfile.worker`, `cloudbuild.yaml`, `infra/run/**`, deploy target | infra outputs | pools + console deployed at a digest; `verify_cloud.sh` |
| launcher+temporal | `swesim/workflows/launchers/cloudrun.py`, queue routing in `workflows.py`/`activities.py`, retries, `patched`, continue-as-new, cancellation, split of mine/validate | Phase 0 | unit tests with fake Cloud Run API; restart proof on the deployed stack |
| storage+index | `swesim/storage/**`, `swesim/index/**`, `swesim/paths.py` adapter, `scripts/cloud/import_vm.sh`, `swesim index` CLI | Phase 0 | rebuild from bucket equals files; import of the VM record labelled |
| console | `swesim/dashboard/**` (UI/UX per brief §2.5 and §2.5.1: history, browsers, actions, leaderboard, design system) | Phase 0 data interface | screenshots both modes; IAP gate test; no evaluator leak test |
| review-fixes | `swesim/stakeholder/**`, `swesim/stages/draft_spec.py`, `swesim/sources/github_repo.py` (probe reuse), `swesim/stages/score.py` grouping | — | tests per Codex row (brief §7.1) |
| models | `swesim/llm.py`, `swesim/stages/run.py` provider table, OpenRouter in the solver path, judge/stakeholder switch, calibration with per-instance predictions | — | probe file; one OpenRouter smoke trial |
| task-sense | `swesim/audit/**` (rubric, LLM-assisted transcript audit, question-quality and disclosure-timing labels, report) | trial records | report on all v2 transcripts; labels in the index |
| docs | `docs/setup.md`, `docs/runbook.md`, README quickstart, `.env.example`, architecture/ops/grading updates | everything, late | fresh-clone run recorded |
| operator | ledger, sweep submissions, tuning | smoke passed | funnel and leaderboard live |

Conflicts are avoided by ownership; a stream that needs a change in another stream's file asks the integrator, who rules
and either edits `main` or reassigns.

## 5. Cadence and integration

- Streams report at 30-minute checkpoints (a line in their report file); the integrator reconciles live streams every
  checkpoint and chases silent ones.
- Merge rule: a stream merges when its own tests pass, the contract suite passes on the merged tree, and its task review
  is clean or its findings are ruled on. `main` is never red; every merge is pushed.
- Deploy rule: any merge touching worker or console code deploys to Cloud Run through `make deploy` after the
  open-executions check; the digest is recorded in the ledger.
- Fix loops: up to three resumes of the same implementer, then a fresh implementer; never more than five rounds; parked
  findings are ruled on in the ledger.

## 6. Timeline (PDT)

| Time | Milestone |
|---|---|
| 01:15 | Phase 0 interfaces on `main`; streams dispatched |
| 01:15–03:30 | Round 1: infra applied; image built; launcher + queues; storage + index; console skeleton over files; review fixes; OpenRouter |
| 03:30–04:00 | Integration 1; smoke create + run on the new stack; console cutover behind IAP |
| 04:00–05:30 | Creation sweep (repositories first); round 2: console pages, task-sense audit, docs drafts |
| 05:30–07:00 | Difficulty screen; Tier A–C runs; leaderboard live; Codex-row fixes verified in the sweep |
| 07:00–08:00 | Final whole-branch review, verify scripts, docs with measured numbers, ledger, push |

If the smoke on the new stack has not passed by 04:00, the integrator rules on the fallback (brief §8) and records it.

## 7. Task sense, disclosure timing and question quality (the audit that gates the leaderboard)

A leaderboard is only as good as its tasks. The task-sense stream builds and runs an audit over every scenario and every
interactive transcript, evaluator-only, LLM-assisted with `claude-opus-5`, with a rubric and per-item labels stored in
the index and summarised in `docs/grading.md`:

- **Does the task make sense?** The natural brief plus the stakeholder-visible facts must be a coherent ask; every hidden
  test must be a fair consequence of the brief and the facts, not of the oracle's incidental choices; withheld spans must
  be things a reporter would plausibly omit and a developer would plausibly ask about. Labels: `coherent`,
  `fair-tests`, `plausible-withholding`, each with a reason.
- **Does the stakeholder reveal the right information at the right time?** For each reply: was the disclosed fact
  targeted by the question (not volunteered), was anything test-derived or identifier-level disclosed, did a confirmation
  stay at behaviour level, was patience spent sensibly. Labels per reply; counts per model and per scenario.
- **Are the agents asking the right questions?** For each question: specific and decision-oriented, asked before the
  relevant implementation, not fishing, not redundant with the brief. Labels per question; the interaction metrics
  (asked-before-implementing rate, questions per trial, useful-question rate) join the leaderboard as columns.
- **Reward hacking signals** (brief §7) are joined per trial; any trial with a signal is listed under the model's row.

Findings feed back the same night into the drafter prompt (withholding selection), the stakeholder prompt, the judge's
gap criteria, and the difficulty labels. Nothing on the leaderboard skips this audit.

## 8. Verification and honesty

- No claim without the command and its output; evidence files under the bucket's `evidence/` prefix and referenced by path.
- The offline suite, the contract suite, `verify.sh 10` and `verify_cloud.sh` run before the final push; failures are
  reported as failures.
- Numbers in docs come from the index at the time of writing, with the query or script that produced them.
- Secrets never enter prompts, reports, logs, payloads or git.
