# SWE Simulator: design spec

Date: 2026-09-12. Status: draft for review. Author: Saahil Sanganeriya (with Claude).


## 1. Thesis

SWE-bench, SWE-bench Verified, Terminal-Bench and DeepSWE grade a single shot
against a fully specified instruction. Real issues arrive underspecified, and
the discussion thread is where a maintainer resolves what they actually wanted.
SWE-bench Verified filtered underspecified issues out as noise. We treat them as
the signal: the original issue is the brief the agent sees, the resolution is
held by a simulated stakeholder the agent can talk to, and the PR's tests are the
contract. The product is a **pipeline** that turns such material, from several
sources, into runnable, gradable, multi-turn Harbor tasks, plus the tooling to
run them, grade them, grow a dataset, and inspect what agents did.

## 2. Scope for the first version and non-goals

In scope by the presentation:

1. One normalized scenario record shared by all pipeline stages (section 5).
2. The full pipeline as CLI stages: mine, judge, spec, compile, validate,
   register, run, score, view (section 7).
3. Two to three tasks run end to end through those commands, each in three
   conditions (full, hidden, interactive), with controls executed and recorded:
   at least one from the SWE-bench pool and at least one discovered
   automatically from a GitHub repository URL.
4. A simulated stakeholder service with a fact ledger, budgets owned by code,
   and review grounded in the agent's current work (section 8).
5. Grading in a separate clean verifier with a correctness-only reward and
   integrity signals, following the DeepSWE v1.1 pattern (section 10).
6. A dataset registry that grows by construction and saved traces (section 16).
7. A dashboard that explains a run, alongside `harbor view` (section 17).
8. README, grading document, architecture and realism document.

Explicit non-goals for the first version: multi-step (multi-PR) episodes, Harbor's
native user-agent bridge mode, SWE-bench Pro and SWE-rebench importers, a Trac
fetcher for django issues, and any claim that a model has not seen a task.
DeepSWE import is a bonus, attempted only after the two primary sources work.

## 3. Prior art and positioning

| Work | What it does | What it does not do |
|---|---|---|
| SWE-bench / Verified | Issue text to patch, FAIL_TO_PASS and PASS_TO_PASS tests; Verified removed underspecified issues and overly specific tests | No user, no conversation, discards the thread |
| Ambig-SWE (Vijayvargiya et al., ICLR 2026) | LLM removes information from Verified issues; simulated user holds the original issue | Synthetic underspecification; agent-side evaluation only |
| Ask or Assume? (Edwards and Schuster, 2026) | Agent-side scaffold for deciding when to ask; Full 70.8 vs Hidden 54.8 for Sonnet 4.5 | Same synthetic variant; no environment or pipeline contribution |
| tau-bench (Sierra) | LLM user with hidden scenario; reward on final state; pass^k | Customer service, not code |
| DeepSWE v1.1 (2026) | Harbor-format tasks; separate verifier; patch collected by hook; git history stripped; per-test reports | Single shot, fully specified, no user |
| SWE-smith / SWE-Gym / R2E-Gym | Synthetic task generation from commits and injected bugs | Fully specified by construction |
| Harbor 0.23 | Task format, runners, 44 agent adapters, viewer, native user-agent bridge | Bridge only for claude-code and gemini-cli, not with multi-step |

Our contribution, stated narrowly: natural underspecification as the task
source with human labels of what is missing; a stakeholder grounded in a
provenance-tagged fact ledger rather than a prompt; the generator pipeline and
registry; three-condition evaluation with evidence-based failure labels.
Grading plumbing is adopted from DeepSWE v1.1 with attribution.

## 4. Architecture overview

```
sources ──▶ mine ──▶ judge ──▶ spec ──▶ compile ──▶ validate ──▶ register
 (swebench-annotated,          │          │            │             │
  github-repo, deepswe,    ScenarioSpec  Harbor task  controls    dataset.toml
  swe-rebench*, pro*,       (normalized)  directory   outcomes    + registry.json
  authored*)                                                          │
                                                                      ▼
                     view ◀── score ◀── run (harbor run, local Docker / GCP VM)
                  dashboard    pass@k,      │
                  + harbor     pass^k,      ├── main container: repo at base, shims
                    view       labels       ├── user sidecar: ledger, persona, LLM
                                            └── separate verifier: hidden tests
```
`*` = interface defined, importer stubbed or gated for the first version.

Everything after `spec` consumes only the normalized record. Sources differ
only in where the ledger comes from, and that provenance is a recorded field.

The pipeline is orchestrated by **Temporal** (section 7a): each stage is an
activity, each scenario's journey is a workflow, and the human edit of the
drafted ledger is a signal the workflow waits on. Stages remain plain
functions so they can also be run directly for debugging.

## 5. The normalized scenario record (`ScenarioSpec`)

Stored as `scenario.json` in each task's source directory. Fields:

- `id`, `version`, `source` (enum), `source_ref` (instance id, PR URL, task id),
  `created_at`, `pipeline_version`.
- `environment`: `kind` (`prebuilt-image` | `dockerfile`), `image`,
  `repo_url`, `base_commit`, `language`, `setup_notes`.
- `briefs`: list of `{profile, text, provenance}` where `profile` is a named
  disclosure profile: `full` (brief plus every ledger fact), `natural` (the
  original issue text verbatim), and optional authored profiles such as
  `repro-withheld`. `provenance` is `original-issue` or `authored`.
- `ledger`: list of facts `{id, text, kind, provenance, discovery, tests}`:
  - `kind`: `behavior` | `interface` | `constraint` | `reproduction` |
    `context`.
  - `provenance`: `annotator-note` | `issue-thread` | `pr-thread` |
    `pr-review` | `requirements-field` | `instruction-bullet` | `authored`.
  - `discovery`: `brief` | `on-request` | `volunteered-at-event` |
    `repo-derivable` | `author-only`. Facts tagged `author-only` may not be
    required by the contract.
  - `tests`: contract test ids that depend on this fact, or empty.
- `operations`: list of `{type, target, rationale}` with `type` in
  `clarification` | `evaluator-repair` | `synthetic-extension`, recording every
  change the pipeline made relative to the historical material.
- `persona`: `{summary, tone, disclosure_style, derived_from}`.
- `requirements`: list mined from review comments, each linked to a ledger
  fact or a contract test; never replayed verbatim.
- `contract`: `fail_to_pass`, `pass_to_pass` (test ids), `hidden_test_files`
  (verifier-owned), `overlay_paths` (files the verifier replaces at grading),
  `review_fixtures` (a distinct, smaller set used only for review feedback),
  `runner` (command template).
- `controls`: `oracle` (gold patch), `plausible_wrong` (list of patches with a
  one-line rationale), `alternative_correct` (list), `tamper` (list).
- `difficulty`: `annotated` (from source), `judged` (LLM rubric score and
  rationale), `measured` (filled by `score`, conditional on agent, model,
  condition, budget).
- `thread`: cached issue and PR discussion (redacted of the gold diff), for
  the dashboard's requirement-to-source view.

## 6. Sources

Priority for the first version, set by Saahil on 2026-09-12: (1) SWE-bench annotated
pool as the starting point, (2) a **GitHub repository URL** from which the
pipeline discovers issues and linked merged PRs itself, (3) DeepSWE as a
lower-priority bonus. SWE-rebench and SWE-bench Pro are future sources with
the interface reserved and no importer.

| Source | Trial status | Input | Brief (`natural`) | Ledger provenance | Environment |
|---|---|---|---|---|---|
| `swebench-annotated` | primary, first tasks | pool letter, optional instance ids | `problem_statement` verbatim | `annotator-note`, `issue-thread` (`hints_text` plus API), `pr-review` | prebuilt `swebench/sweb.eval.x86_64.*` |
| `github-repo` | primary, end-state | repo URL; optional `--pr` / `--issue` to pin | issue body | `issue-thread`, `pr-thread`, `pr-review` | Dockerfile built by pipeline at the PR base commit |
| `deepswe` | bonus | task id | `display_description` (authored brief) | `instruction-bullet` | their pinned image |
| `authored` | interface only | scenario JSON | human or LLM written | `authored` | any |
| `swe-rebench`, `swebench-pro` | future | | | | |

`github-repo` discovery: list merged PRs in a date window; for each, resolve
linked issues from the PR body ("Fixes #N", "Closes #N") and from the issue
timeline cross-references; keep pairs where the PR changes both source and
tests, the source diff is inside a size band, and the combined issue plus PR
thread has at least a floor of non-bot comments; fetch threads and review
comments; emit candidates with rejection reasons. Passing `--pr` skips
discovery for that PR. Environment for this source is built by the pipeline:
clone at the PR's base commit, install with the repo's declared tooling, run
the test command once to confirm the PR's tests fail before the fix, then
apply DeepSWE-style history stripping. Repos whose tests need network or
services are rejected at `mine` with that reason.

Pool for `swebench-annotated`: the 1,699 annotated instances. Pool A
(underspecified score 2 or 3, test-validity concern 1 or less, no other major
issue) has 109 instances and is the first candidate set. Pool B (test-validity
concern exactly 2) has 245 and Pool C (clear issue, test concern 2) has 276;
these are admitted only after per-instance classification into clarification
versus evaluator repair. Note: 539 instances pass the published filter and 500
are in Verified, so set difference is not the rejection set; pools are defined
by scores. django issues live on Trac, so django instances without a GitHub
thread are deprioritized for the first version.

Per selected instance, the judge stage must answer four questions and record
them: what is actually missing; does the original discussion supply it; is it a
legitimate behavioral requirement; does the evaluator accept alternative
correct implementations.

## 7. Pipeline stages

All stages are subcommands of one CLI (`swesim`) operating on a workspace
directory. Code owns every decision that affects reward; LLMs draft text and
propose classifications that are recorded as annotations.

1. `mine --source swebench-annotated [--pool A]` or `mine --source github-repo
   --repo URL [--pr N]`: discovers issue and PR pairs (section 6), applies
   hard filters (linked issue where applicable, tests changed, source diff
   size band, thread length floor, image or build availability, merge date),
   fetches threads, writes `candidates.jsonl` with per-candidate rejection
   reasons.
2. `judge`: frontier model rubric over issue, thread, diff and tests scoring
   complexity, spec gap, test behavioralness, self-containedness; the four
   questions above; writes `judged.jsonl`. Score is a prior, not a difficulty
   measure.
3. `spec`: drafts `ScenarioSpec`: ledger facts with kind, provenance and
   discovery path; persona; requirements from review comments; controls
   (plausible-wrong and alternative-correct patches drafted by LLM, verified by
   `validate`). For `swebench-annotated`, reports recall of the annotator's
   missing-information note against the drafted ledger. Human edits the JSON
   before compile; the edit is the authoring step.
4. `compile`: emits one Harbor task directory per condition (section 9).
5. `validate`: runs the controls through Harbor (`oracle`, `nop`, each
   plausible-wrong, alternative-correct and tamper patch), confirms base fails
   and gold passes, writes `validation.json`. A task is `valid` only if oracle
   scores 1, nop scores 0, every plausible-wrong scores 0, every
   alternative-correct scores 1, and every tamper patch is either 0 or flagged.
6. `register`: adds the task to `dataset.toml` and `registry.json` with
   version, provenance, validation status and artifact paths.
7. `run`: wraps `harbor run` with our agent, model, condition, attempts and
   environment settings; syncs the job directory to storage.
8. `score`: reads job results; computes binary success, pass@k and pass^k per
   task and condition, outcome classes (section 10), interaction metrics,
   and evidence-based labels where determinable; writes `scores.json`.
9. `view`: serves the dashboard (section 17).

## 7a. Orchestration with Temporal

Decision (Saahil, 2026-09-12): run the pipeline on Temporal. Rationale: the
stages are long-running, failure-prone, and partly human-gated; durable
execution gives retries, timeouts, visibility, and a natural place for the
human-in-the-loop step, and the Temporal Web UI shows pipeline state without
us building that screen.

Mapping:

- `MineRepositoryWorkflow(repo_url, window, filters)`: activities
  `discover_pairs`, `fetch_threads`, `apply_filters`; starts one
  `ScenarioWorkflow` child per surviving candidate. For the SWE-bench source,
  `MinePoolWorkflow(pool)` does the same from the annotation CSVs.
- `ScenarioWorkflow(candidate)`: activities `judge`, `draft_spec`, then waits
  on signal `spec_approved(edited_spec)` with a timeout, then `compile`,
  `validate`, `register`. A rejected judge score or a failed validation ends
  the workflow in a recorded terminal state with the reason.
- `RunMatrixWorkflow(task_id, conditions, models, k)`: one `run_trial`
  activity per cell with heartbeats (Harbor on Modal can take many minutes),
  bounded concurrency via a task queue, then `score`.
- Activity contracts: idempotent on `(scenario_id, version, stage)`; outputs
  are files in the workspace plus a small JSON result; retries with backoff
  for network and provider errors; no retries for validation failures.
- Local: `temporal server start-dev` (Web UI on port 8233), one Python worker
  process (`python -m swesim.worker`), `temporalio` SDK. Temporal Cloud is a
  drop-in later. The `swesim` CLI stages start workflows or send signals;
  `--direct` runs the underlying function without Temporal for debugging.
- Not on Temporal: the episode itself (Harbor orchestrates the trial) and the
  stakeholder service (a plain HTTP service).

Cost acknowledged: roughly two to three hours of integration during the
trial. Paid for by moving DeepSWE import and the review sandbox to stretch.

## 8. The simulated stakeholder

Topology: a **hosted stakeholder service**, not a compose sidecar. Rationale:
Harbor's Modal and Daytona backends disable network allowlists whenever a
compose file is present, and Docker Desktop on the development Mac fails
Harbor's egress-control kernel check. A single service outside every sandbox
keeps the leak boundary (the ledger never enters the agent container), works
with any single-container provider, and lets one deployment serve many trials
keyed by a per-trial session token. For local development the same service
runs on localhost and the agent reaches it through Harbor's agent-host
allowance.

The service holds, per trial session: the ledger, persona, requirements,
budgets, and a verbatim transcript. `main` gets two shell commands installed
by compile:

- `ask-user "<question>"`: POSTs to the service with the session token;
  prints the reply.
- `request-review`: commits the working tree on the agent's branch and sends
  the diff. The service runs the review fixtures (never the hidden contract
  tests) in an isolated **review sandbox** (a Modal Sandbox on the same
  provider, no access to the ledger), and writes a review grounded in the diff
  and those results. If the review sandbox is not built in time, the fallback
  is diff-only review with results marked `unverified` in the transcript.

Code owns: turn and question budgets, review-round budget, token validation,
verbatim transcript, event records, and refusal to disclose `author-only`
facts. The LLM owns: wording in persona, deciding which `on-request` facts a
question legitimately unlocks, and a `disclosed_fact_ids` annotation per
reply. The annotation is a diagnostic, not a reward input. Provider is
configurable (Anthropic or OpenAI), temperature 0, seed recorded. Historical
review comments are never replayed; their mined requirements drive review of
the current work only.

Transcripts and ledgers are written by the service to the storage bucket per
session and joined to Harbor trial results by trial id in `score`; they are
not collected as Harbor artifacts from the agent container.

Leak boundary: the ledger never enters `main`. Replies can still leak content
by construction, which is intended; the service API exposes only `ask` and
`review`, executes no submitted code itself, and requires a session token.

## 9. Compilation to Harbor tasks

Per scenario and condition, a task directory `tasks/<id>__<condition>/`:

- `task.toml`: separate verifier environment; `[[verifier.collect]]` hooks
  that commit the working tree and write `git diff --binary <base> HEAD` to an
  artifact, and copy the sidecar ledger and transcript to a second artifact;
  `artifacts` declared; `[agent] network_mode = "allowlist"` with only the
  agent model hosts and the stakeholder service host, `[verifier]
  network_mode = "no-network"`; metadata with source, provenance summary,
  annotated difficulty, condition.
- `instruction.md`: the brief for the condition. `full` includes every ledger
  fact. `hidden` is the natural brief; no session token is issued and the
  shims report that no stakeholder is available. `interactive` is the natural
  brief plus one paragraph describing the two shell commands and the budgets.
  One environment image serves all three conditions; only the instruction and
  the session configuration differ.
- `environment/Dockerfile`: FROM the pinned prebuilt image or the pipeline's
  build; DeepSWE-style git time travel (real branch at base commit, origin
  removed, non-ancestor branches and tags deleted, reflog expired, gc); shims
  installed; project tests left in place; PR test files that the contract
  overlays are left at their base-commit state.
- `tests/Dockerfile`: FROM the same pinned image; hidden test files, overlay
  files, runner config and `grader.py` baked in.
- `solution/solve.sh`: applies the gold patch and commits, so Harbor's
  `oracle` agent validates the task.

Unresolved: whether Harbor's local Docker egress control activates on this
Mac's Docker Desktop kernel, and whether prebuilt x86 images run acceptably
under emulation on Apple Silicon. Both are checked in the first hour of
implementation; the GCP VM is the fallback for either.

## 10. Grading and outcomes

Verifier steps, in order:

1. Load `model.patch`; if absent or empty, record outcome
   `agent-invalid-submission` (not an infrastructure failure) and reward 0.
2. Apply to a pristine checkout at `base_commit`; failure to apply is
   `agent-invalid-submission`.
3. Overlay verifier-owned test files and runner configuration; remove
   agent-added `conftest.py` or equivalent hooks inside overlaid directories.
4. Run FAIL_TO_PASS and PASS_TO_PASS with a per-test structured report
   (JUnit or CTRF), as an unprivileged user, no network, with a timeout.
   Cross-check report against exit status; on disagreement, re-run
   FAIL_TO_PASS tests individually in fresh processes.
5. Record integrity signals as diagnostics: edits to runner config or
   import-time hooks outside the gold patch's scope, edits outside the expected
   fix scope, dependency lockfile changes, skip or xfail markers added to
   project tests.
6. Write `reward.json`: `reward` (1 only if every FAIL_TO_PASS and
   PASS_TO_PASS test passed), `f2p_pass_fraction`, `p2p_pass_fraction`,
   `outcome` (below), `integrity_signals` (list). Interaction metrics go to
   `interaction.json` from the sidecar artifact: questions asked, budget used,
   facts annotated disclosed, review rounds, simulator token cost.

Outcome classes, mutually exclusive, all counted in the dashboard:
`valid-correct`, `valid-incorrect`, `agent-invalid-submission`,
`collection-failure`, `simulator-failure`, `evaluator-failure`. Only the last
three are retried, by a fixed policy (one retry), and excluded from pass
rates with their counts shown.

Threat model, bounded: the agent controls `main` and everything the collect
hook reads there, so the diff is taken against the compiler-recorded base
revision and the verifier never trusts tools from `main`. The package under
test executes inside the test process; that exposure is inherent to unit-test
grading and is documented. Named attacks exercised by the tamper controls:
edit a contract test, add a skip marker, add a `conftest.py` that exits,
monkeypatch the runner, read the gold patch from git history, call the
sidecar for the answer to a test.

## 11. Experimental design

Three conditions on a fixed contract and grader:

| Condition | Brief | User | Purpose |
|---|---|---|---|
| `full` | natural brief plus all ledger facts | none | baseline with requirements upfront |
| `hidden` | natural brief | none | effect of withholding without a recovery channel |
| `interactive` | natural brief | sidecar, budgets recorded | how interaction recovers from incomplete information |

k attempts per cell (k = 3 for the first version, labeled a pilot). `score` computes
pass@k and pass^k itself; Harbor's helper requires a single binary key and is
not used. Full-spec performance is a baseline, not a ceiling. Simulator cost
and budgets are reported alongside.

Evidence-based labels assigned by `score` from ledger and transcript, with
`unresolved` allowed:

| Observation | Label |
|---|---|
| Relevant question asked; required answer withheld or wrong | simulator failure |
| Clear ambiguity; agent assumed incorrectly without asking | potential agent failure |
| Requirement delivered clearly; implementation violates it | likely implementation or retention failure |
| Correct result without clarification | success without clarification |
| Trace cannot establish cause | unresolved |

## 12. Difficulty and selection

Hard filters and annotated difficulty select candidates; the judge adds a
prior; measured pass rates per condition are the only difficulty claim, and
they are reported as conditional on agent, model, harness, simulator and
budget. A task where the weak tier passes `full` too often, or the frontier
tier never passes `full`, is flagged for re-profiling or removal.

## 13. Contamination stance

We claim only what we record: source, dates, image provenance, and whether
the gold patch is reachable from the agent's repository (it is not, by
construction). A strong `full` baseline does not measure memorization. Public
tasks from 2024 to 2025 are not contamination-controlled for models evaluated
in September 2026. The `authored` source and private task bundles are the path
to genuinely unseen tasks, and their interface is defined now.

## 14. Infrastructure

Primary execution provider: **Modal**, through Harbor's `modal` backend,
single-container tasks with per-phase network allowlists (verified in Harbor
0.23 source: allowlists are supported on Modal when no compose file is
present). The stakeholder service and the review sandbox are deployed on Modal
as well, so one account covers execution, stakeholder, and review. Fallback:
one x86 VM on GCP running Docker and Harbor's Docker backend, which supports
compose and egress control together; gcloud is installed locally for this.

Local development: Docker Desktop on the Mac runs x86 images under emulation
and has about 7.6 GB allocated; it fails Harbor's egress-control kernel
check, so local runs are for smoke tests only and never for graded results.

Storage: a GCS bucket receives each job directory after `run` and each
stakeholder session transcript. Secrets: `~/Documents/.env` for model keys,
`gh` for GitHub, Modal token via `modal token new`. Sweep duration is
measured, not promised.

## 15. Agents

Reference agent: `mini-swe-agent` via Harbor, bash-only, provider-agnostic
through litellm, the same harness DeepSWE fixes across models. Comparison
agents as time allows: `claude-code`, `codex`. Model tiers for the pilot:
Haiku 4.5, the GPT small tier named by the user (identifier to be supplied),
Sonnet 5, Opus 5. "Works with any shell-capable agent" is claimed only for
agents actually run.

## 16. Registry and traces

`dataset.toml` (Harbor) plus `registry.json` (ours) list every registered
task with version, source, provenance summary, validation status, conditions
available, and artifact paths. Each `run` produces a Harbor job directory
containing trajectories in Harbor's interchange format, verifier outputs,
collected artifacts, the sidecar transcript and ledger, and our `scores.json`.
Job directories are synced to storage and indexed in `registry.json`.

## 17. Viewing

Three surfaces. The Temporal Web UI shows every workflow, its stage, retries,
and pending human approvals. `harbor view` shows raw trajectories. Our
dashboard, a small Python web app reading the workspace, job directories and
Temporal's visibility API, adds four screens:

1. Pipeline funnel: candidates, judged, specified, compiled, validated,
   registered, with rejection reasons, linked to the Temporal workflow.
2. Task card: brief per condition, ledger with provenance and discovery
   paths, persona, requirements, controls and their outcomes.
3. Run matrix: task by condition by model by attempt, reward components,
   outcome classes, pass@k and pass^k.
4. Trial explainer, the primary screen: for each requirement, the source
   discussion excerpt, the user reply that disclosed it (if any), the agent's
   relevant change, and the per-test result, with the evidence label.

## 18. Repository layout

```
swesim/
  swesim/            CLI package: mine, judge, spec, compile, validate, register, run, score, view
  swesim/workflows/  Temporal workflows and activities; worker entry point
  sources/           one module per source producing ScenarioSpec
  stakeholder/       hosted stakeholder service, review sandbox, shims
  verifier/          grader.py, overlay logic, tamper scans, report parsing
  templates/         Harbor task templates (task.toml, Dockerfiles, shims, test.sh)
  dashboard/         web app
  workspace/         candidates, scenarios, tasks, validation, jobs (gitignored except registry)
  data/annotations/  SWE-bench Verified annotation CSVs and pools
  references/        DeepSWE task example, Harbor swebench adapter template
  docs/              README, grading.md, architecture.md, this spec
```

## 19. Deliverables mapped to the brief

- Runnable system: the CLI and services above.
- Fully authored simulation: at least one `swebench-annotated` task with all
  three conditions, controls executed, threads cached, persona and ledger
  reviewed by hand.
- README: install, run one task, author a new one from a PR URL.
- Grading document: reward definition, components, outcome classes, the
  attack-versus-defense table with executed control outcomes, example runs.
- Architecture and realism document: this spec's sections 4 to 13 condensed,
  with the prior-art table and the claims we do and do not make.

## 20. Schedule

Evening of 2026-09-12: scaffold CLI and `ScenarioSpec`; Temporal dev server,
worker, and the `ScenarioWorkflow` skeleton with stub activities;
`swebench-annotated` mine and thread fetch on Pool A; compile one instance to
a `full` task and run `oracle` and `nop` on Modal; stakeholder service and
shims; first `interactive` run with mini-swe-agent.

Morning of 2026-09-13: verifier with overlay, report parsing and integrity
signals; controls and `validate`; `github-repo` discovery and environment
build on one repository, producing the second task; a third task from either
source; `register`; `run` and `score` with three conditions and k attempts on
Modal.

Afternoon of 2026-09-13: dashboard, docs, presentation. Stretch, only if the
above is done: DeepSWE import on one task, review sandbox.

## 21. Unresolved integration details

- Harbor `modal` backend: image build from our Dockerfiles, allowlist entries
  for the stakeholder host, and `--allow-agent-host` behavior for local runs.
- Modal Sandbox as the review sandbox: startup latency and cost per review.
- Collection is best-effort in Harbor, so the verifier validates artifact
  presence itself.
- SWE-rebench score direction and per-candidate image availability.
- Trac fetcher for django issue threads.
- Model identifiers for the GPT small tier.
- Temporal: `temporalio` SDK version pin, worker concurrency for `run_trial`,
  and whether Modal job polling needs activity heartbeats or a child workflow.

## 22. Claims we make and do not make

We make: tasks are built from naturally underspecified issues with recorded
provenance; the hidden material never enters the agent's container; grading
runs in a clean container on a patch against a recorded base; controls are
executed and their outcomes shown; interaction is measured and reported
separately from reward.

We do not make: that any model has not seen a task; that interaction reward
is solved; that the simulator is realistic beyond what the transcripts show;
that the pipeline works on sources whose importers are stubs; that sweep
durations or pass rates generalize beyond the recorded configuration.
