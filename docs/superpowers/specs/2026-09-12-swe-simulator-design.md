# SWE Simulator (swesim): design spec, v2

Date: 2026-09-12 (v2, after the Codex adversarial review in
`docs/reviews/2026-09-12-codex-adversarial-review.md`). Status: for review.
Author: Saahil Sanganeriya, with Claude.
v1 is archived beside this file.

## 0. What changed from v1

Blocking fixes: `reward.json` is numeric-only with a separate `report.json`
(B1); the agent's working tree is collected as a tarball by Harbor under an
allowlist baseline, with no git execution in the agent container (B2); any
control designated an incorrect exploit must score zero or the task is
rejected (B3). Major fixes: one topology (Modal single-container, hosted
stakeholder, separate verifier, transcript joined on the worker) (M1); `full`
brief excludes author-only material by construction (M2); hardened record
with asset manifest, run config, excerpt provenance, and strict validation
(M3, M7, N4); base and gold expected-status evidence (M4); attribution rules
for outcomes (M5); stakeholder session protocol (M6); simulator labeled
stochastic, labels adjudicated by humans in the pilot (M8); frozen budgets and
diff-only review for all compared runs (M9, M10); path policy at patch
application (M11); calibration separated from comparison (M12); tracked
example task (M13); episode manifest export (M14); bounded repo-only
discovery (M15); Temporal kept thin with stable trial ids and no automatic
launch retries (M16, M17, N5); positioning corrected (N1); counts qualified
(N2). Cut list applied (section 2).

## 1. Thesis

SWE-bench hands an agent the issue text as the reporter wrote it and grades a
patch with the fix's tests. Many of those issues are underspecified; the
`hints_text` discussion exists in the dataset but is not shown to the agent.
SWE-bench Verified removed instances whose issue was rated underspecified or
whose tests were rated unfair. DeepSWE and Terminal-Bench author fully
specified instructions. In all of them there is nobody to ask.

swesim keeps the underspecified issue as the brief, puts the resolution with
a simulated stakeholder the agent can talk to, and grades with the fix's
tests in a clean container. The product is a pipeline that produces such
tasks from two sources for the first version, SWE-bench's human-annotated
underspecified instances and any GitHub repository URL, packaged as Harbor
tasks, run on Modal, orchestrated by Temporal, and inspectable per episode.

## 2. Scope for the first version and non-goals

In scope by the presentation, in critical-path order:

1. The separate verifier with path policy, expected-test manifest, numeric
   reward, report, and outcome attribution, exercised by real controls on one
   SWE-bench task (sections 9, 10).
2. The stakeholder service with the session protocol, the limited tool loop
   (`read_diff`, `read_changed_file`, and `run_check` in a Modal Sandbox),
   exercised by one interactive episode with a real agent (section 8).
   If `run_check` is not working by the morning of 09-13, compared runs use
   `diff-only` and say so.
3. The full `swebench-annotated` path: mine, judge, spec (human-edited),
   compile to three conditions, validate, register, run, score, export, on a
   task that passes the non-derivability requirement (section 6).
4. The `github-repo` path from a repository URL with bounded discovery, one
   task discovered, built, validated and run through the same stages.
5. Temporal orchestration, thin: one pipeline workflow, one trial workflow,
   persistent dev server on the VM, worker under systemd.
6. Tracked example task package and one sanitized example run in the repo.
7. Dashboard: a runs table and a trial page with the live stakeholder chat
   while running and the full interleaved episode after completion (section
   17), plus `harbor view` and the Temporal UI.
8. README, grading document, architecture and realism document.

Model configurations for the pilot: two, one weaker tier and one frontier
tier, k = 3 attempts, three conditions. Expansion to more tiers only after
everything above exists.

Non-goals for the first version: DeepSWE, SWE-bench Pro, SWE-rebench importers;
Trac fetcher; stakeholder source-debugging tools;
multi-step episodes; Harbor's native user-agent bridge; GKE; Temporal Cloud;
GCS auto-sync (an `export` command and durable job directories suffice);
automated semantic failure labeling; four polished dashboard screens; any
claim that a model has not seen a task.

## 3. Prior art and positioning

| Work | What it does | What it leaves for us |
|---|---|---|
| SWE-bench | Issue text to patch; FAIL_TO_PASS and PASS_TO_PASS; ships `hints_text` but does not show it to the agent | no interlocutor; underspecified issues are unsolvable by design |
| SWE-bench Verified | Human filter on underspecification and test fairness; 500 kept of 1,699 annotated | the rejected instances and their human notes |
| Ambig-SWE (Vijayvargiya et al., ICLR 2026) | A benchmark variant of Verified with information removed by an LLM, plus a simulated user, plus agent evaluation | natural rather than synthetic underspecification; provenance for what the user knows |
| Ask or Assume? (Edwards and Schuster, 2026) | Agent-side clarification scaffold on that variant; Full 70.8 vs Hidden 54.8 for Sonnet 4.5 in its setup | environment-side pipeline, grading integrity |
| tau-bench (Sierra) | LLM user with hidden scenario, final-state reward, pass^k | domain transfer to code |
| DeepSWE v1.1 (2026) | Harbor tasks, separate verifier, collected patch, stripped git history, per-test reports | interaction; generation from public threads |
| SWE-Gym (real PRs) and SWE-smith (procedural modification), R2E-Gym | Task generation at scale | naturally underspecified briefs with a grounded interlocutor |
| Harbor 0.23 | Task format, 44 registered agents, viewer, native user bridge (claude-code and gemini-cli targets only, not with multi-step) | the stakeholder protocol and grading contract |

Our contribution, stated narrowly: an authoring and validation workflow that
turns naturally underspecified issues with human labels into interactive
tasks whose stakeholder holds excerpt-backed facts, graded by a verifier
whose controls are executed and recorded. No categorical novelty claim.

## 4. Architecture

```
 sources:  swebench-annotated | github-repo | (authored: interface only)
              │
              ▼
 Temporal PipelineWorkflow ─ activities: mine → judge → draft_spec → [signal: human approves] → compile → validate → register
              │                                    (all read/write the workspace; ScenarioSpec is the only handoff after draft_spec)
              ▼
 Temporal TrialWorkflow(trial_id) ─ activities: ensure_session → launch_harbor (no retry) → await_result → fetch_transcript → score → export
              │
              ▼
 Harbor (modal backend): [agent sandbox] ──tar of /app──▶ [separate verifier] ──reward.json/report.json──▶ job dir
        agent sandbox ◀──HTTPS (allowlisted)──▶ [stakeholder service on Modal]  ──transcript──▶ fetched by worker, joined by trial_id
```

One topology. The stakeholder transcript is never an agent artifact; the
worker fetches it from the service after the trial and stores it beside the
Harbor result. Everything after `draft_spec` consumes only the validated
`ScenarioSpec` plus a versioned `RunConfig`.

## 5. The normalized record

### 5.1 `ScenarioSpec` (per scenario, versioned, strict)

Pydantic model with `extra="forbid"`. Validators reject: duplicate fact or
requirement ids; dangling references; empty `fail_to_pass`; empty oracle
diff; missing `natural` brief; ids not matching `^[a-z0-9][a-z0-9._-]{2,63}$`;
author-only facts with test links; facts without excerpt or authored
rationale; asset references not present in the manifest.

- `id`, `version` (>= 1), `source`, `source_ref`, `created_at`,
  `pipeline_version`, `spec_hash` (computed over the canonical JSON).
- `environment`: `kind` (`prebuilt-image` | `dockerfile`); for
  `prebuilt-image`, `image` and `image_digest` (resolved at compile);
  for `dockerfile`, `dockerfile_asset` (manifest ref) and `build_context_assets`;
  `repo_url`, `base_commit`, `language`, `test_command`, `install_notes`.
- `assets`: manifest `{name: {path, sha256, bytes}}`, paths relative to the
  scenario directory; covers hidden test files, protected-path overlays,
  reproducer files, control patches, the Dockerfile.
- `briefs`: `[{profile, text, provenance}]`; `natural` required with
  provenance `original-issue`; other profiles are `authored`. A brief must
  keep a recognizable symptom or request, enough context to begin, and a
  desired outcome; withheld material must be discoverable through the
  repository, questions, or review. Lossiness is selected for, not targeted
  to a percentage; extra omissions are separate authored profiles whose
  effect is measured.
- `ledger`: `[Fact]` with `id`, `text`, `kind` (`behavior` | `interface` |
  `constraint` | `reproduction` | `context`), `provenance`
  (`issue-thread` | `pr-thread` | `pr-review` | `annotator-note` | `authored`),
  `source_excerpt` and `source_url` (required unless `authored`, then
  `authored_rationale` required), `discovery` (`brief` | `on-request` |
  `volunteered-at-review` | `repo-derivable` | `author-only`), `tests`
  (contract test ids; must be empty for `author-only`).
- `operations`: `[{type: clarification | evaluator-repair | synthetic-extension,
  target, rationale}]`, one entry for every change relative to the historical
  material.
- `persona`: `{summary, tone, disclosure_style, derived_from_urls}`.
- `requirements`: `[{id, text, fact_id?, tests, source_url}]`, mined from
  review; used by the stakeholder's review reply; never replayed verbatim.
- `contract`: `fail_to_pass`, `pass_to_pass` (exact test ids), `test_assets`
  (manifest refs for files the verifier owns), `protected_paths` (globs whose
  changes are never applied, see 10.2), `expected_base` and `expected_gold`
  (per-test statuses recorded by `validate`), `runner` (command template
  producing JUnit XML).
- `checks`: predefined stakeholder reproducer checks `[{id, description,
  command, asset?, expected_observation}]`, disjoint from the contract tests;
  derived from the reporter's reproducer or ordinary user workflows.
- `controls`: `oracle` (asset), `plausible_wrong` (>= 1, each with rationale,
  `expected_success: 0` and `expected_reward_max`), `alternative_correct`
  (>= 1 when the judge flags structural tests, else optional,
  `expected_success: 1`), `tamper` (>= 1 of class `incorrect-exploit` with
  `expected_reward: 0`; optional `benign-edit` class with `expected_success:
  1` and expected signals).
- `groups`: requirement groups for partial credit (10.5): `{id, weight,
  tests}` covering every FAIL_TO_PASS id exactly once.
- `difficulty`: `annotated`, `judged` (score, rationale, model id, prompt
  version), `measured` (filled by `score`, keyed by run config hash).
- `thread`: cached discussion with URLs, redacted of the gold diff.

### 5.2 `RunConfig` (per run, versioned, hashed)

`agent` (Harbor agent name, version), `model` (provider id), `condition`,
`solver_timeout_sec`, `stakeholder` (`model_id`, `prompt_version`,
`temperature`, `patience_profile` (section 8), `review_mode: checks | diff-only`,
`max_tool_calls_per_activation`),
`k`, `harbor_version`, `environment_provider: modal`, `run_config_hash`.
Every trial id embeds it: `trial_id = h(spec_hash, condition, run_config_hash,
attempt_index)`.

## 6. Sources

Priority set by Saahil: SWE-bench annotated pool first, GitHub repository URL
second, DeepSWE a bonus after both work (not in trial scope).

| Source | Input | `natural` brief | Ledger provenance | Environment | Trial status |
|---|---|---|---|---|---|
| `swebench-annotated` | pool letter, optional instance ids | `problem_statement` verbatim | `issue-thread` (`hints_text` plus GitHub API), `pr-thread`, `pr-review`; `annotator-note` only when corroborated by the thread, else `authored` | prebuilt `swebench/sweb.eval.x86_64.<id>`, digest resolved at compile | primary |
| `github-repo` | repository URL; optional `--pr N` | issue body | same, all from GitHub API | Dockerfile built by the pipeline at the PR base commit | primary |
| `authored` | scenario directory | human or LLM text | `authored` | any | interface only |

Pools for `swebench-annotated` (exact predicates over the ensembled CSV,
`other_major_issues == 0` throughout): A: `underspecified >= 2 and
false_negative <= 1` (109); B: `underspecified >= 2 and false_negative == 2`
(245); C: `underspecified <= 1 and false_negative == 2` (276). A and B are
both candidate pools for the first version; B instances are admitted only after the
judge classifies the test's unstated assumption as a stakeholder choice
(clarification) rather than an evaluator defect (repair). 539 rows pass the
published filter and 500 are in Verified; pools are defined by scores, not by
set difference. django instances are deprioritized (Trac tracker).

Non-derivability requirement (added after review of the SymPy `Mod`
example, which a strong model solves from mathematics alone): a candidate is
kept for the interactive set only if at least one contract-required fact is a
**choice among reasonable alternatives** that neither the repository nor
domain knowledge determines: which exception type and message, which of two
output shapes, whether to warn or raise, what to do for an input the reporter
could not share, which public name to expose. The judge records this fact
and its alternatives. Facts a capable engineer could infer from the code or
the domain are tagged `repo-derivable` and do not qualify. Non-derivability
alone does not make a good task: prefer missing **decisions with engineering
consequences** (backward compatibility, failure handling, output semantics,
migration behavior) over arbitrary strings such as an exact message that is
unknowable but inconsequential. Empirical screen:
after calibration runs, a task the frontier tier passes in `hidden` on all
attempts is moved out of the interactive comparison set (its underspecification
is not load-bearing) and kept as a calibration record. Prefer annotated
difficulty above fifteen minutes and multi-file fixes.

Annotator notes describe both missing intent and evaluator defects; a note is
never a fact's sole provenance. If a thread references a reproducer that is
not publicly available, the candidate is rejected or the reconstruction is
marked `authored` with rationale.

`github-repo` discovery, bounded: parameters `--since`, `--max-prs` (default
200), `--max-candidates` (default 20), `--max-builds` (default 3),
`--build-profile python-pytest` (the only supported profile: Python package
installable with `uv pip install -e .` or the repo's declared extras, tests
run with pytest). Steps: list merged PRs in the window; resolve linked issues
from PR body keywords and issue timeline cross-references; keep pairs where
the PR changes both source and test files, source diff is within the size
band, the issue plus PR thread has at least `--min-comments` (default 4)
non-bot comments, and the issue predates the PR; fetch threads; write
`candidates.jsonl` with a reason per reject. If no candidate survives, the
workflow ends in `no-valid-candidate` with the reject histogram. Environment
build: clone at base, install, run the PR's test files once and require that
at least one fails on base and passes after the gold patch; then strip
history (9.3). Build failures are recorded per candidate and count against
`--max-builds`.

## 7. Pipeline stages

Each stage is a plain Python function `stage(workspace, id, run_config?) ->
StageResult` writing files under `workspace/<scenario_id>/` and a small JSON
result. Temporal activities wrap them; `swesim <stage> --direct` calls them
without Temporal.

1. `mine`: as in section 6. Output `candidates.jsonl`.
2. `judge`: LLM rubric with fixed prompt version over issue, thread, diff,
   tests; scores complexity, spec gap, test behavioralness, self-containment;
   answers the four questions (what is missing; does the thread supply it; is
   it a legitimate behavioral requirement; does the evaluator accept
   alternative correct implementations). Output `judged.jsonl`. A prior, not
   a difficulty measure.
3. `draft_spec`: LLM drafts `ScenarioSpec` with excerpts; drafts control
   patches. Output `scenario.draft.json`. The workflow then waits for the
   `approve_spec` signal carrying the human-edited JSON, or times out into
   `awaiting-approval`. The human edit is the authoring step and is recorded.
4. `compile`: validates the spec, resolves image digest, emits three Harbor
   task directories, writes `compile.json` with hashes.
5. `validate`: runs controls through Harbor on Modal once per spec version
   (the contract is identical across conditions): base expected statuses via
   `nop` (must show every F2P failing and every P2P passing, recorded as
   `expected_base`), oracle (`expected_gold`, reward 1, success 1), each
   plausible-wrong (success 0 and reward at most its declared maximum), each
   alternative-correct (success 1), each tamper (`incorrect-exploit` must earn
   reward 0; `benign-edit` must succeed with its expected signals). Any
   mismatch marks the scenario `invalid` with the failing control. Output
   `validation.json`.
6. `register`: appends to `registry.json` and Harbor `dataset.toml` with
   version, hashes, validation status, asset paths.
7. `run`: per `TrialWorkflow`, launches `harbor run` for one trial id; on
   completion fetches the stakeholder transcript by trial id; stores both.
8. `score`: per scenario and run config: outcome classes, pass@k and pass^k
   over attempts with `outcome in {valid-correct}` as success, code-exact
   interaction counts, and a placeholder for human-adjudicated labels.
9. `export`: writes the episode manifest (section 16).
10. `view`: serves the minimal dashboard.

## 7a. Orchestration with Temporal (thin)

- Server: `temporal server start-dev --db-filename /var/lib/swesim/temporal.db
  --ip 0.0.0.0` on the GCE VM under systemd; UI on 8233 via SSH tunnel.
- Worker: one process, task queue `swesim`, activity concurrency 4, launch
  concurrency 2 (Modal jobs), under systemd with restart.
- `PipelineWorkflow(source, params)`: activities `mine`, then per candidate
  `judge`, `draft_spec`, wait for `approve_spec` signal (timeout 24h),
  `compile`, `validate`, `register`. Fan-out bounded by `--max-candidates`.
- `TrialWorkflow(trial_id)`: workflow id equals `trial_id`, so a second start
  is rejected by Temporal and a trial cannot be launched twice by a retry of
  the parent. Activities: `ensure_session` (idempotent, keyed by trial id),
  `launch_harbor` (retry policy: maximum attempts 1; on failure the trial is
  marked `launch-failed` and reported), `await_result` (heartbeating poll of
  the job directory), `fetch_transcript`, `score`, `export`. A new
  experimental attempt is a new `attempt_index`, never a Temporal retry.
- Payloads carry ids and hashes only; files stay in the workspace.
- Restart check before the presentation: kill the worker mid-trial and confirm
  exactly one recorded attempt after recovery.

## 8. The stakeholder service

Form (decided 2026-09-12, revised after discussion): a **limited,
event-driven agent**, cooperative about requirements and limited about
implementation. It is activated by events (`ask`, `review`) and runs a small
bounded tool loop (at most 4 tool calls per activation) before replying.
Its tools are code-controlled and read-only:

- `read_diff()`: the diff submitted with the current `review` request.
- `read_changed_file(path)`: a file from the submitted snapshot, only for
  paths present in the diff.
- `run_check(check_id)`: one of the scenario's **predefined reproducer
  checks** (user-level workflows or the reporter's reproducer, never the
  hidden contract tests), executed in an isolated Modal Sandbox built from
  the task image with the submitted diff applied, no network, no secrets,
  bounded time and output. Returns observed behavior and output.

Four boundaries: knowledge (stakeholder-visible view only; no oracle, no
hidden tests); tools (the three above; no editing, no shell, no source
debugging); feedback (expected versus observed behavior and requirement
clarifications; never prescribed edits or algorithms, enforced by prompt and
audited in the transcript); availability (the patience budget, identical
across compared runs). The stakeholder's "looks good" is feedback, not
reward. Every tool call and its result is recorded in the transcript and
shown on the trial page beside the reply, so a reviewer can judge whether the
stakeholder clarified or effectively solved.

Review modes, frozen per `RunConfig` and identical across compared runs:
`checks` (tools available, the default once `run_check` works) or
`diff-only` (only `read_diff` and `read_changed_file`; replies state that
nothing was executed). Prior art: SWE-Interact gives its simulated user tools
to inspect the workspace; our difference is evidence-backed generation of the
stakeholder's knowledge and inspectability of its actions.

Hosted on Modal as a web endpoint, one deployment, many sessions. Holds per
session: the stakeholder-visible view of the spec (facts with discovery in
`{brief, on-request, volunteered-at-review, repo-derivable}`, persona,
requirements; never `author-only` facts, never the oracle, never test files),
the `RunConfig` stakeholder block, budgets, counters, and the transcript.

Session protocol:
- `POST /sessions` (worker only, authenticated by a worker secret): body
  `{trial_id, spec_hash, condition, run_config_hash}`; returns a session
  token bound to the trial id. `hidden` and `full` conditions get no session.
- `POST /ask` and `POST /review` (agent, via shims, session token, and a
  client `request_id`): requests for one session are serialized; a repeated
  `request_id` returns the cached reply; counters are server-owned; over
  budget returns a fixed refusal. Replies record `generated_at`,
  `delivered: true` on successful HTTP response.
- `POST /freeze` (worker): closes the session; later agent requests get 410;
  the terminal transcript with `transcript_hash` is returned to the worker.
  The worker calls freeze before starting the verifier.
- The LLM context contains only the stakeholder-visible view; author-only and
  evaluator material are excluded structurally, not by instruction.

Disclosure policy: any `on-request` fact may be given when a related question
is asked; broad legitimate questions may receive broad answers (no forced
drip). The reply carries `disclosed_fact_ids` as a self-report annotation.
The simulator is stochastic and is labeled so; temperature 0 and the
recorded model id are for reproducibility of configuration, not of output.

Patience (decided 2026-09-12): asking is neither rewarded nor penalized by
the grader. Its cost is modeled as a **patience budget owned by code**, part
of the `RunConfig` as `patience_profile = {start, question_cost,
review_cost, terse_below, refuse_at}`. Budget is spent per stakeholder
**reply**, not per question mark: each `ask` exchange spends `question_cost`,
each `review` exchange spends `review_cost`. The instruction states the
availability truthfully ("I can answer a few follow-ups; investigate what you
can from the repository first"). While patience is above `terse_below`
the stakeholder answers in full. Between `terse_below` and `refuse_at` the
service instructs the model to answer in one or two sentences and to deflect
questions whose facts are `repo-derivable` toward the code. At or below
`refuse_at` the service returns a fixed refusal without calling the model.
The patience value and its trajectory are recorded per request and exported;
profiles are a difficulty knob and are frozen per run config. Post-training
consumers may compute a shaped penalty from the exported counts; the
environment's reward never includes one.

Review: the agent's `request-review` shim sends the current diff (hashed);
the stakeholder may read it, read changed files, and run predefined checks
per the review mode, then replies against the recorded requirements and what
it observed; it never introduces requirements outside the ledger. The
reviewed diff hash is recorded so `score` can report whether the submitted
patch matches the last reviewed diff.

Shims: `ask-user` and `request-review` are small root-owned executables in
the image that read the session token from a root-owned file readable by the
agent user, so the agent can use them but not alter them. The hosted service
hostname is in the allowlist.

## 9. Compilation to Harbor tasks

### 9.1 `task.toml` (per condition)

- `[environment] network_mode = "allowlist"` is the **baseline**, which
  governs agent setup and collection (Harbor restores the baseline after the
  agent phase; agent setup runs before it in `trial.py` `_prepare`). The
  baseline is **strict**: model API hosts plus the stakeholder host only.
  Harbor's installed agents normally fetch their toolchain at setup
  (mini-swe-agent's installer runs `apt-get`, the uv bootstrap, `uv python
  install`, `uv tool install`), and on Modal even an allowlist containing the
  package mirrors did not let `apt-get` through (HTTP mirrors; observed
  `NetworkConnectionError`). Therefore compile **pre-bakes the reference
  agent's toolchain** into the image and setup becomes a no-op. Verified
  recipe (spike `smoke6-prebaked`, 2026-09-12, mini-swe-agent 2.4.6 with
  Haiku 4.5, reward 1, 1m29s):
  - as root: `apt-get install curl bash build-essential git ca-certificates
    python3`; `useradd -m -u 1001 agent`; install uv system-wide
    (`UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1`);
  - as `agent`: `uv python install 3.12 && uv tool install --python 3.12
    mini-swe-agent --with litellm --with orjson --with fastapi`;
  - as root: `chown -R agent:agent /home/agent`;
  - `[environment.env]`: `UV_OFFLINE = "1"`, `UV_NO_PROGRESS = "1"`,
    `UV_CACHE_DIR = "/tmp/uv-cache"` so Harbor's setup commands succeed
    offline and fail loudly if something is missing.
  The Python version constant comes from Harbor's adapter
  (`_MINI_SWE_AGENT_PYTHON_VERSION`, currently 3.12) and is read at compile.
- `[agent] user = "agent"`, `network_mode = "allowlist"` with **only** the
  model API hosts and the stakeholder host (the agent phase policy; verified:
  github.com and example.com blocked, api.anthropic.com reachable),
  `timeout_sec` from `RunConfig`.
- `[verifier] network_mode = "no-network"`, `environment_mode = "separate"`,
  `[verifier.environment]` builds from `tests/Dockerfile`.
- Collection (verified on Modal 2026-09-12, spike `smoke3-tar-collect`):
  one `[[verifier.collect]]` hook, run by Harbor as root in the agent
  container after the agent phase, under the allowlist baseline:
  `du` size check (over 500 MB writes `oversized` and stops), then
  `tar --numeric-owner --exclude=./.git --exclude=./.venv ... -cf
  /logs/artifacts/app.tar -C /app .`, then writes `/logs/artifacts/collect.status`
  with tar's exit code, file count, size, sha256 and the executing user. Only
  root-owned binaries run; no agent code executes. `/logs/artifacts` is
  collected by Harbor by default and transferred to the separate verifier
  reliably (manifest status `ok`). Harbor's own directory-artifact path is
  **not** used: its host-side `extractall(filter="data")` aborts the whole
  download on the first absolute symlink and silently truncates the tree
  (observed: manifest `failed`, partial files delivered).
- Metadata: source, spec hash, condition, run config hash, annotated
  difficulty, image digest.

### 9.2 `instruction.md`

`full`: natural brief plus the stakeholder-visible facts (never author-only),
plus "no stakeholder is available". `hidden`: natural brief plus "no
stakeholder is available". `interactive`: natural brief plus one paragraph on
`ask-user`, `request-review`, and the budgets. All three state the same
solver time budget.

### 9.3 `environment/Dockerfile`

FROM the resolved image digest (or the pipeline's build). Then: create user
`agent`; repository at `/app` owned by `agent`; git time travel as DeepSWE
v1.1 (real branch at base, origin removed, non-ancestor refs and tags
deleted, reflog expired, gc), then `.git` made read-only to `agent` is not
attempted (the agent may use git locally); `core.hooksPath=/dev/null` in the
repo config; root-owned `/app/.swesim/` containing `receipt` (image digest,
spec hash) which the agent cannot remove because the directory is root-owned
and non-empty; shims in `/opt/swesim/bin` root-owned; token file
`/opt/swesim/session` root-owned, mode 0644, written at container start by
the harness environment variable (empty in `full`/`hidden`); dependencies
prebuilt so no network is needed after build. A known-reference reachability
check (`git cat-file -e <gold_sha>` must fail, and a grep for the gold patch's
distinctive lines across the filesystem outside `.git` must be empty) runs in
`validate` inside the built image.

### 9.4 `tests/Dockerfile` and verifier contents

FROM the same image digest. Copies: pristine checkout at `/pristine` (the
same tree as the agent's `/app` at build), verifier-owned test assets,
`expected_tests.json` (exact ids, expected statuses), `grader.py`, runner
config, `policy.json` (protected paths, allowed path types). No network.

### 9.5 `solution/solve.sh`

Applies the oracle asset with `git apply` as the agent user; used by Harbor's
`oracle` agent for validation. Control patches are applied the same way by
`validate` through a `patch-agent` Harbor agent (a tiny installed agent that
applies a named asset).

## 10. Grading

### 10.1 Inputs and outputs

Input: `/logs/artifacts/app.tar` and `/logs/artifacts/collect.status`
(the receipt), `/pristine`, `expected_tests.json`, `policy.json`, and
`groups.json` (requirement groups, 10.5). Outputs in `/logs/verifier/`:

- `reward.json`, numeric only (Harbor rejects any non-numeric value):
  `reward` (partial credit in [0, 1], 10.5), `success` (0 or 1: the whole
  contract passed), `f2p_passed`, `f2p_expected`, `p2p_passed`,
  `p2p_expected`, `groups_passed`, `groups_total`, `integrity_signal_count`,
  `outcome_code` (integer enum), `patch_applied` (0 or 1).
- `report.json`: `outcome` (string), `integrity_signals` (list with paths and
  kinds), `per_test` (id, group, expected, observed), `per_group` (id,
  weight, passed), `patch_sha256`, `reviewed_diff_sha256` (filled by the
  worker from the transcript), `dropped_paths`, `rejected_members` (tar
  entries refused: symlinks, devices, absolute or parent paths), `receipt`
  (parsed `collect.status` or `missing`), timings.

### 10.2 Steps

1. Receipt check: `collect.status` missing or unparsable, or `app.tar`
   missing while the receipt reports `rc=0`, is `collection-failure`
   (infrastructure). Receipt `oversized` is `agent-invalid-submission`.
   A receipt sha256 that does not match `app.tar` is `collection-failure`.
2. Extract member by member with `tarfile` `filter="data"` into a scratch
   directory, refusing symlinks, hard links, devices, and any member whose
   normalized path escapes the root; each refusal is recorded in
   `rejected_members`. Compute the change set against `/pristine`: added,
   modified, deleted regular files, compared by content hash. Policy: drop
   changes to `protected_paths` (contract test files, `conftest.py` at any
   depth, `pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml`, CI config,
   `.gitattributes`, `.gitignore`, dependency lockfiles) and record each drop
   as an integrity signal; globs are matched on normalized, case-folded
   paths. If nothing remains, outcome `agent-invalid-submission`.
3. Apply the remaining change set onto `/pristine` as a fresh copy; record
   `patch_sha256` over the canonical change set.
4. Chown the applied tree to the unprivileged test user (the verifier runs
   as root on Harbor's Modal backend; files arrive owned by the host uid).
   Run the contract as the unprivileged user with the verifier-owned runner
   config: one pytest invocation producing JUnit XML at a path chosen per run
   and not known to the code under test in advance; parse per-test statuses;
   require every expected id exactly once; missing, skipped, xfailed, or
   errored count as failed. Cross-check the process exit code with the parsed
   result; on disagreement re-run each F2P test in a fresh process and take
   the per-process result.
5. `success = 1` iff every F2P id observed `passed` and every P2P id observed
   `passed`, else 0. `reward` is the partial credit of 10.5. Integrity signals
   never change either; controls (7.5) decide whether a task whose exploit
   control earns credit is accepted at all.
6. Write both files; exit 0.

### 10.5 Partial reward

Fraction of tests passed is a poor reward: doing nothing passes every
PASS_TO_PASS test, one requirement may have a hundred parameterized tests and
another two, and counts are not comparable across tasks. Instead, credit is
awarded per **requirement group**: `groups.json` maps each FAIL_TO_PASS test
to exactly one group with a weight, derived at `spec` time from the
requirement-to-test links in the record (tests not linked to any requirement
form a residual group; weights default to equal and are human-editable). A
group passes only if every test in it passes. Regression rule for the pilot,
fixed in the contract before any model runs: any PASS_TO_PASS failure sets
`reward = 0`. Then `reward = sum(weight_j * passed_j)`, and `success = 1` iff
`reward == 1` and no regression. `success` feeds pass@k and pass^k; `reward`
is exported for training and for explaining near misses. Controls under this
rule: oracle `reward 1, success 1`; nop `reward 0`; each plausible-wrong
control declares `expected_success: 0` and `expected_reward_max` (a
legitimate partial solution may earn its declared partial credit, an
evaluator-bypass attempt must earn 0); each `incorrect-exploit` tamper must
earn `reward 0`; `benign-edit` controls earn 1 with their expected signals.

### 10.3 Outcome classes and attribution

| Outcome | Rule | In pass-rate denominator | Retry |
|---|---|---|---|
| `valid-correct` | reward 1 | yes | no |
| `valid-incorrect` | reward 0 with a valid patch | yes | no |
| `agent-invalid-submission` | receipt present, no applicable changes or patch fails to apply | yes | no |
| `agent-runtime-failure` | tests could not complete because of submitted code (crash, resource exhaustion, timeout during tests) | yes | no |
| `collection-failure` | receipt missing or tree absent | no | once |
| `simulator-failure` | stakeholder returned 5xx or timed out on a within-budget request, evidenced by service logs | no | once |
| `evaluator-failure` | verifier image failed to start or grader crashed before step 4 | no | once |

The original attempt's record is kept with lineage to its retry. Solver
timeouts before submission are `agent-invalid-submission`. Attribution of
`simulator-failure` requires service-side evidence, not a transcript label.

### 10.4 Threat model, bounded

The agent controls `/app` and its own user's processes. Defended: editing or
skipping contract tests (protected paths, exact-id manifest), runner or
discovery tampering via config and conftest (protected paths, pristine
tree), forging the reward file (verifier-owned), fetching the fix from GitHub
during solving (allowlist), fetching it during collection (allowlist
baseline, no program executed at collection), recovering it from history
(stripped, checked), asking the stakeholder for it (structural exclusion),
budget evasion (server-owned counters, serialized sessions, idempotent
request ids), late transcript mutation (freeze before verify), denominator
gaming (attribution table). Residual and documented: code under test executes
in the pytest process and can in principle forge its own JUnit output; the
random report path, exit-code cross-check, and per-process re-run raise the
bar but do not close it. Model API calls from the agent are not metered by
swesim; solver budgets are time-based in the pilot.

## 11. Experimental design

Three conditions on a fixed contract, grader, image, solver time budget, and
`RunConfig`. `interactive` adds the stakeholder and review; its effect is
reported as the whole interaction treatment, with stakeholder token counts
and review counts alongside. `full` is a baseline, not a ceiling.

Calibration versus comparison: runs used to select or re-profile tasks are
labeled `calibration` in the registry; accepted tasks and configs are frozen;
comparison numbers come from fresh attempts after freezing. Reported: per
task, condition and model, counts of each outcome, pass@k and pass^k over
valid attempts with k = 3 labeled a pilot, and uncertainty as exact counts.
No ranking claim across models.

Labels for interactive failures are assigned by a human in the pilot from
the transcript and ledger, with `unresolved` allowed; LLM suggestions may
assist but never change outcomes or denominators. Code-exact metrics
(questions asked, reviews requested, reviewed-diff equals submitted-diff) are
reported separately from labels.

## 12. Selection and difficulty

Hard filters, annotated difficulty, and the judge's prior select candidates.
Measured pass rates are conditional on agent, model, harness, simulator, and
budget, and are reported as such. A task where the weaker tier passes
`hidden` in all attempts or the frontier tier passes `full` in none is
flagged for re-profiling; the flag and any change are recorded as
calibration history.

## 13. Contamination stance

Recorded: source, dates, image digest, and the outcome of the known-reference
reachability check in the built image. Not claimed: that any model has not
seen a task. A strong `full` baseline measures performance under that spec.
The `github-repo` path with recent merges narrows exposure; private
repositories and authored tasks are the path to unseen material, and the
`authored` interface exists for that.

## 14. Infrastructure

Goal: the pipeline runs with the laptop closed.

| Component | Where | Notes |
|---|---|---|
| Temporal server | GCE VM `e2-small`, persistent disk, `--db-filename` | UI via SSH tunnel |
| Worker | same VM, systemd, restart on failure | runs `harbor run -e modal`; no Docker needed on the VM |
| Agent sandboxes, image builds, separate verifiers | Modal via Harbor | verified 2026-09-12: build, oracle 1, nop 0, agent allowlist (anthropic 401, example.com and github.com blocked), verifier no-network |
| Stakeholder service | Modal web endpoint | worker secret for session creation |
| Artifacts, transcripts, scores | job directories on the VM disk; `swesim export` bundles | GCS sync optional, not in scope |
| Dashboard | VM, behind the same SSH tunnel | reads job directories |
| Laptop | thin client: start workflows, approve specs, browse | |

Acceptance test: submit a repository URL from the laptop, close it, return
later, and verify from the Temporal UI and job directories that discovery,
approval wait, compile, validate, one trial, grading, and export progressed
remotely.

Local development: Docker Desktop on this Mac failed Harbor's egress kernel
probe on 2026-09-12 (`_egress_control_kernel_support()` returned False; the
probe also returns False on execution errors), and has about 7.6 GiB
allocated. Local runs are for developing the grader with the shared verifier
only, never graded results.

## 15. Agents and models

Reference agent: `mini-swe-agent` through Harbor. Pilot models: one weaker
tier (Haiku 4.5) and one frontier tier (Sonnet 5 or Opus 5, chosen by cost
after a first run). The GPT small tier and additional agents are added only
after the scope in section 2 exists. Claims about agent compatibility are
limited to agents actually run.

## 16. Registry, traces, tracked example, export

- `registry.json` (tracked at repo root, not under `workspace/`): scenarios
  with version, spec hash, source, validation status and evidence path,
  conditions compiled, calibration and comparison run ids.
- `examples/<scenario-id>/`: a complete compiled task package for one
  SWE-bench scenario (all three conditions, assets, controls), plus one
  sanitized run: transcript, patch, `reward.json`, `report.json`, and the
  episode manifest. A reviewer can run it with the README alone.
- `.gitignore` ignores `workspace/*` only; `examples/` and `registry.json`
  are tracked.
- Episode manifest (`swesim export`): `policy_visible` (instruction, agent
  trajectory as recorded by Harbor, stakeholder replies delivered),
  `evaluator_only` (ledger, oracle path, hidden test asset names, report),
  `lineage` (spec hash, run config hash, image digest, patch sha, transcript
  hash, harbor job id), `family_id` (source instance, for split grouping),
  `simulator_validity` (`ok` | `failure` | `unadjudicated`), `interaction`
  (questions asked, reviews requested, patience start and end, refusals).
  Trainer integration and training are out of scope.

## 17. Viewing

Three surfaces. The Temporal UI shows pipeline and trial workflow state,
pending approvals, retries. `harbor view` shows raw trajectories. The swesim
dashboard has two pages:

1. **Runs table**: scenarios by condition by model with live status from
   Temporal, outcome counts, pass@k and pass^k, calibration versus comparison
   labels, links to each trial.
2. **Trial page**, the primary screen. While the trial runs: status, elapsed
   solver time, and the stakeholder chat streamed live from the service
   (questions, replies, disclosed-fact annotations, patience remaining,
   review rounds). Agent actions are shown live only if Harbor's output
   subscription hooks (`trial.py` logging callbacks) can be wired to the
   worker in time; otherwise the page says the agent trajectory arrives at
   the end of the run. After the trial: the complete timeline interleaving agent
   actions (from Harbor's trajectory), questions and replies by timestamp,
   the patch with dropped protected paths marked, per-test expected versus
   observed, outcome, integrity signals, the human label field, and links to
   the episode manifest and raw Harbor directory.

Implementation: a small FastAPI app on the VM reading the workspace, job
directories, Temporal's visibility API, and the stakeholder service's
transcript endpoint; polling, no websockets.

## 18. Repository layout

```
swesim/
  CLAUDE.md                 build, test, run commands; conventions; pitfalls (kept under 150 lines)
  PLAN.md                   implementation plan with milestones and acceptance checks
  scripts/verify.sh         runs the milestone checks; exit 0 when the current milestone is met
  swesim/                   package: cli, models, stages/, workflows/, stakeholder/, verifier/, templates/, dashboard/
  sources/                  swebench_annotated.py, github_repo.py, authored.py
  examples/                 tracked example task package and run
  registry.json
  data/annotations/
  references/
  docs/                     spec, reviews, explainer, README, grading.md, architecture.md
  workspace/                ignored except .gitkeep
```

## 19. Deliverables mapped to the brief

Runnable system: the package, worker, service, templates. Fully authored
simulation: the tracked example with three conditions and executed controls.
README: install, run the example, author from a SWE-bench instance and from a
repository URL. Grading document: sections 10 and 11 with executed control
outcomes and one real episode. Architecture and realism: sections 1 to 8, 13,
22.

## 20. Schedule (critical path first)

Evening 2026-09-12: hardened models and validators; verifier image and
`grader.py` with policy, manifest, numeric reward; `patch-agent`; compile one
SWE-bench scenario's `full` task; run nop, oracle, one plausible-wrong, one
incorrect-exploit tamper on Modal; record `expected_base` and
`expected_gold`. Stakeholder service on Modal with sessions, freeze, shims;
one `interactive` episode with mini-swe-agent.

Morning 2026-09-13: Temporal server and worker on the VM; `PipelineWorkflow`
and `TrialWorkflow`; `swebench-annotated` mine, judge, draft, approve,
register; `github-repo` discovery and build on one repository, one task
through validate and run; `score` and `export`; restart check.

Afternoon 2026-09-13: fresh comparison attempts on both tasks (2 models, 3
conditions, k = 3); tracked example; dashboard table and episode page; docs;
presentation. A third task only if everything above is done.

## 21. Unresolved, with the smallest verification

- Resolved 2026-09-12 by spikes `smoke2*` and `smoke3-tar-collect`: the
  separate verifier receives `/logs/artifacts` reliably at the same path;
  Harbor's directory artifact with `exclude` is unreliable (aborts on absolute
  symlinks); `[agent] user = "agent"` works for the oracle agent and the
  agent cannot remove a root-owned directory; the verifier runs as root;
  multi-key numeric `reward.json` is accepted.
- Resolved 2026-09-12 by spike `smoke6-prebaked`: mini-swe-agent runs as
  the `agent` user on Modal under the strict baseline with a pre-baked
  toolchain; ATIF trajectory (`agent/trajectory.json`) and the native
  trajectory are written; the collect hook and separate verifier work with a
  real agent run.
- Modal web endpoint reachable from a Harbor Modal sandbox under allowlist
  by hostname: one probe from the smoke task.
- Temporal `TrialWorkflow` id dedup and worker restart: the restart check in
  7a.
- Token file injection at container start under Harbor's Modal backend:
  environment variable path versus a mounted file; one probe.
- SWE-bench image sizes and pull times on Modal for django and sympy.

## 22. Claims, and the presentation's questions

Claims we make: tasks come from naturally underspecified issues with
excerpt-backed provenance and recorded operations; hidden material never
enters the agent container; grading runs on a policy-filtered change set
against a recorded base in a container the agent never enters; controls are
executed with recorded outcomes; interaction is measured and reported
separately from reward; the pipeline runs unattended on the VM plus Modal.

Claims we do not make: that a model has not seen a task; that the simulator
is deterministic or realistic beyond its transcripts; that stubs work; that
pass rates generalize beyond the recorded configuration; that in-process
report forgery is impossible.

Questions the presentation answers, with the section that answers each: what
the first task measures and which trace shows it (11, 16); which requirement
came from whom (5.1 provenance, operations); why each scored requirement is
discoverable and how simulator omission is distinguished from agent
assumption (5.1 discovery, 11); what keeps `full` from exposing author-only
material (5.1, 9.2); which program writes reward and whether submitted code
can influence it (10.1, 10.4); what happened when controls ran (7.5,
`validation.json`); whether collection executes hooks or restores networking
(9.1, 10.2); which outcome applies when code crashes the runner or the
stakeholder times out (10.3); the budgets and the interaction treatment
(5.2, 11); what is calibrated versus generalizable (11, 12); what resets
between attempts (8, 5.2); which exports are policy-visible (16); whether a
reviewer can run one task from the repository alone (16, 19); whether task
two came from a repository URL without a PR (6, 7); what the Modal smoke
proved and what remains untested (14, 21); why this topology (4, 14); the
accepted limitation (10.4); how a worker crash cannot double-count a trial
(7a).
