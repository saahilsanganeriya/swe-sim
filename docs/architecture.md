# swesim architecture

## Purpose and thesis

swesim turns naturally underspecified GitHub issues into interactive software-engineering tasks. SWE-bench hands an agent the issue as the reporter wrote it and grades a patch with the fix's tests; SWE-bench Verified removed the instances humans rated underspecified. swesim keeps those issues as the brief, moves the information that resolved them (maintainer decisions from the PR thread and review, plus details withheld from the reporter's own text) into a simulated stakeholder the agent can ask, and grades the submitted tree offline with the fix's tests in a container the agent never enters. The product is a pipeline: from a SWE-bench annotated pool or a GitHub repository URL it mines candidates, judges the specification gap, measures the test contract offline, drafts a provenance-tagged scenario record, approves it through code-owned gates, compiles three Harbor task variants, validates them with executed controls on Modal, and runs, scores and exports trials, orchestrated thinly by Temporal from a VM so nothing waits for the laptop.

## Components

```
 sources ── swebench-annotated (pool A CSV, dataset, GitHub threads) | github-repo (merged PRs, built Dockerfile)
    │
    ▼  CreateTasksWorkflow (Temporal, thin; payloads are ids, hashes, paths) ── one CandidateWorkflow per candidate
 mine → judge → probe → draft → approve → compile → validate → register
                (LLM)  (Modal:   (LLM +   (6 gates,  (3 Harbor  (controls on Modal)
                        base/gold) code)   auto|hold)  tasks)
                                                                  │ registry.json, dataset.json
    ▼  RunTasksWorkflow ── one TrialWorkflow per (scenario, condition, model, attempt)
 plan_trials → launch (one systemd unit per trial) → await → score → export
                                                                        ▼ harbor run -e modal
 ┌────────────────────────────┐  HTTPS, allowlisted host  ┌────────────────────────────┐
 │ agent container (Modal)    │ ◀──────────────────────▶ │ stakeholder service (Modal)│
 │  user agent, repo at base  │  ask-user/request-review │  sessions, patience,       │
 ├────────────────────────────┤ ── POST /freeze ───────▶ │  transcript, freeze hash   │
 │ root collect hook          │                          └─────────────┬──────────────┘
 │  pkill agent, tar, receipt │                                        │ GET /transcript (worker secret)
 └─────────────┬──────────────┘                                        ▼
    app.tar + collect.status ▼ (file artifacts)           ┌────────────────────────────┐
 ┌────────────────────────────┐  reward.json/report.json  │ worker (VM)                │
 │ separate verifier (Modal)  │ ────────────────────────▶ │  binds transcript hash,    │
 │  no network, /pristine     │                           │  record, scores, exports   │
 └────────────────────────────┘                           └─────────────┬──────────────┘
                                                                        ▼
                                                          dashboard + operator console (loopback; SSH forward)
```

## Data flow

Every stage is a plain function writing under `workspace/`; Temporal activities wrap them and the CLI calls them directly. After `draft`, the only handoff is the validated `ScenarioSpec` (`swesim/models.py`, strict Pydantic) plus a hashed `RunConfig`.

| Stage | What it does | Output |
|---|---|---|
| mine | `swebench-annotated`: pool A is `underspecified >= 2`, `false_negative <= 1`, `other_major_issues == 0` over the ensembled annotation CSV (109 ids), filtered to pytest-run repositories whose tests do not need the network (psf/requests is excluded after its sweep); rows carry the issue verbatim, the published F2P/P2P ids, gold and test patches, image name, annotation and PR thread. `github-repo`: lists merged PRs since a date, resolves linked issues in three tiers (closing keyword, timeline cross-reference, plain mention, recorded as `link_kind`), keeps pairs with code and test changes whose code-only size is in band (feature: 200 to 1,500 lines, 3 to 30 files, at least 8 comments, a feature-type label; bugfix: 20 to 600 lines, at least 4 comments) and whose issue predates the PR, writes a reject histogram, and builds up to `--max-builds` candidates on Modal to derive F2P/P2P from a real Dockerfile. A thread whose pagination exceeds the bound is a `fetch-error`, never truncated. | `candidates.jsonl`, `.rejects.json`, `.status` |
| judge | `judge-v3`: the model proposes the gaps the tests or fix require but the issue never states, each with a verbatim evidence span that code re-checks against the test files or patch (at least 3 tokens and an identifier from test-file lines; hunk trailers attribute excerpts; a source label never exempts a gap); `resolved_by` is `thread` or `tests`; acceptance is code-owned (one gap non-derivable from the repository, consequential and resolved; task self-contained). The judge model must differ from the draft model. Calibration against the human labels is in `docs/grading.md`. | `.judged.jsonl`, `judge.json` |
| probe | Measures the contract offline on Modal before any draft, for both sources: stages `base` (pristine tree), `test` (test patch applied, whole test files run), `gold` (oracle applied), then the exact-id stages `test_exact` and `gold_exact`, which run the grader's own launcher command on the contract ids. F2P is what fails on base and passes on gold; a contract test whose status differs between the whole-file run and the exact-id run is dropped as `order_dependent`; network and environment failures are dropped and counted. An empty measured F2P ends the candidate as `not-reproducible-offline`; an exact-id stage that cannot be trusted ends it as `probe-failed` ("unreliable exact-id probe … keep evidence and reject"). Probe metadata is bound to `base_commit` plus `test_patch`, and `draft` refuses a row whose binding does not match or that has no probe. The probe controller runs under the image's `swesim-python`, because a testbed interpreter older than 3.7 could not import it. | `.judged.probed.jsonl`, `jobs/probe/<id>/` |
| draft | A second model (never the judge's) authors facts, persona, requirements, checks, groups, the plausible-wrong and (when the judge flags structural tests) alternative-correct patches, and selects the spans to withhold. Code owns everything else: the natural brief; the withheld brief, computed by deleting the model-selected spans from the issue at line or sentence boundaries, each removal recorded as an `original-issue` fact whose text is the verbatim span (zero removals is valid and yields natural-only briefs); verbatim grounding of every non-authored fact against the text its provenance names (diff bodies without markers for test-patch facts); F2P linkage of on-request facts with a code-owned auto-link when the contract has a single target; protected paths (the contract's test files only; a path the gold patch modifies can never be protected, and the model's `protected_paths_extra` is dropped and recorded); control patches, verified at draft time with `git apply --check` against the fetched base files, built by `difflib` from complete file texts when the model supplies them, slugified names, and a code fallback control when the model's wrong patch is unusable; oracle and test assets, tamper controls, manifest, the runner command in definition order, the leak check and the schema. One correction round (schema and integrity errors included), then failure. | `scenario.draft.json`, `draft.meta.json`, `assets/` |
| approve | Six code-owned gates (see `docs/grading.md`); records `approved_by: auto-validators, review_needed: true`. In hold mode the CandidateWorkflow parks the draft as `awaiting-approval` for up to seven days and the console's review queue approves or rejects it against the draft's spec hash. A leak rejection re-drafts once; anything else takes the next candidate. Drafts are never hand-edited. | `scenario.json`, `approval.json` |
| compile | Three Harbor 0.23 task directories from one spec: pinned image digest (a mutable tag is refused), both Dockerfiles (`build-essential` pre-installed so the agent harness never needs `apt` inside the allowlisted sandbox), `task.toml`, `instruction.md`, collector, shims, grader and config, oracle and control diffs. `compiled_hash` covers spec, digest, agent version, templates, verifier and shims, so an instruction change is a new build. Declared-but-empty submodules are allowed and recorded; populated ones are refused. | `tasks/<id>__{full,hidden,interactive}/`, `compile.json` |
| validate | Control sweep and reachability probe on Modal; base and gold evidence recorded; any mismatch is `invalid`. The probe pairs distinctive added lines with their hunk's removed lines so copies already present at base are not leak evidence (`raw_hits` versus `filtered_hits` are both recorded). | `validation.json` |
| register | Only a `valid` scenario with matching hashes enters the registry and dataset manifest. | `registry.json`, `dataset.json` |
| run | Per trial: stakeholder session (interactive only), a per-trial task copy with token and trial id, the pinned agent version passed to Harbor, the interactive scaffold config for mini-swe-agent (below) with its sha256 recorded, Harbor launch, verifier files, transcript fetch and hash binding. | `trials/<trial_id>/*.json` |
| score | Outcome counts, pass@k and pass^k, mean reward, interaction counts (questions, reviews, patience at end), labels; attempts without a record are listed as unresolved with their `status.json` state and never enter `n`. | `scores.json` |
| export | Episode manifest separating policy-visible from evaluator-only material, with lineage and eligibility. | `exports/<trial_id>.episode.json` |

### Why the offline probe exists

`sphinx-doc--sphinx-9467` passed the judge and all six gates, compiled, and then its `nop` control passed all four published FAIL_TO_PASS ids on the untouched base: SWE-bench's lists were produced with network access and do not reproduce in an offline verifier. The probe was added so that the contract is measured under the verifier's own conditions before a model spends a draft on it. Its exact-id stages exist because `scikit-learn--scikit-learn-13584` reached a fully correct sweep and was still invalid: one PASS_TO_PASS test failed on base only when the runner listed the FAIL_TO_PASS ids first (test-order pollution the whole-file run hid). The runner now emits ids in definition order and the probe runs the grader's exact command shape. Two candidates (`matplotlib-24189`, `pytest-7283`) were rejected by that stage rather than drafted.

### Why protected paths are code-owned

The first `pytest-dev--pytest-7283` draft let the model name `protected_paths_extra`, and it named the gold patch's own source file (`src/_pytest/unittest.py`); the oracle control was then dropped by the path policy and graded `agent-invalid-submission`. Protected paths are now computed from the contract's test files, and the invariant that no gold-touched path is protected is enforced at draft time; the model's list is recorded as `protected_paths_dropped` and never applied.

### Why control patches are verified at draft time

The first `py-pdf--pypdf-3826` sweep and the VM's `mwaskom--seaborn-3190` create job both ended `invalid` on the plausible-wrong control: the model-authored diff was corrupt, so the control agent failed before a clean submission, and an infrastructure outcome is never a passing control. Every control patch is now checked with `git apply --check` against the fetched base files before any Modal job; when the model supplies complete file texts the diff is built by `difflib`; a path never fetched is never treated as absent; and if the model's wrong patch is unusable after the correction round, code substitutes a real incorrect change (`fallback-comment-only` on pypdf-3826) and records the substitution in `draft.meta.json`.

### Why the interactive scaffold config exists

Under the compiled instruction alone, Haiku 4.5 asked the stakeholder nothing in nine interactive trials across three scenarios (seven on the laptop, two on the VM), and Sonnet 5 asked nothing under the first wording. The instruction was rewritten to say that the brief is intentionally incomplete and that unsettled decisions should be asked before implementing (Sonnet 5 and Opus 5 then asked; Haiku still did not), and then the agent harness itself was addressed: `swesim/templates/mini_swe_agent.interactive.yaml` is layered by Harbor after the packaged `mini` config for interactive trials only and adds one step to mini-swe-agent's Recommended Workflow (list the decisions the description leaves open and ask about each with `ask-user` before implementing; optionally `request-review`). It changes nothing about the task, the brief or grading; it is recorded per trial as `report.agent_config_sha256` so trials with and without it can be separated even when they share a `run_config_hash`. With it, Haiku asked in one of two trials. The measurements are in `docs/grading.md`.

## Conditions and brief profiles

| Condition | Brief | Stakeholder | Measures |
|---|---|---|---|
| `full` | natural issue plus a "Requirements (complete)" list of every stakeholder-visible fact; author-only facts excluded by construction | none | the ceiling: the agent with all disclosable information |
| `hidden` | the run's profile (`withheld` when present, else `natural`) | none | the floor: the issue alone |
| `interactive` | the same brief as `hidden` | `ask-user`, `request-review`, patience budget | the treatment: obtaining and using facts through interaction |

`natural` is the issue exactly as filed and is always kept. `withheld` removes specific categories from it (reproduction code, error messages and tracebacks, literal expected outputs, file and line references, version pins, explicit fix proposals) and never summarizes; every removal is a ledger fact carrying the removed text verbatim, and the brief is produced by code from the model's selected spans, never taken from the model. `hidden` and `interactive` share a brief, so their difference is the stakeholder alone. Both profiles have run in the `hidden` arm on two scenarios, one or two trials each; that is not enough to estimate the cost of withholding.

## The stakeholder

A hosted FastAPI service on Modal (`swesim/stakeholder/`): one deployment, one JSON snapshot per trial committed before any reply is returned. The transcript is never an agent artifact; the worker fetches it after the trial and checks its hash against the receipt.

- Knowledge. The worker builds the visible view from the spec: natural brief, non-author-only facts, requirement texts, persona, check descriptions. It never touches assets, controls or the contract; the service rejects a view containing an author-only fact.
- Patience budget. Code-owned, part of the `RunConfig` (`start 6, question_cost 1, review_cost 2, terse_below 3, refuse_at 0` by default), spent per reply. Above `terse_below` the model answers in full; at or below it the prompt asks for one or two sentences and deflects repo-derivable facts to the code; at or below `refuse_at`, or when the cost is unaffordable, a fixed refusal is returned without a model call. Asking changes availability, never reward. The Sonnet 5 episode spent 6 → 5 → 3 → 1 and received its last review at the `terse` tier.
- Disclosure. Any on-request fact may be given when a related question is asked, at the specificity asked: a broad question gets the requirement list, an exact exception type needs an exact question. `disclosed_fact_ids` is a model self-report kept worker-only; the agent sees reply text, remaining patience and tier.
- Diff-only review. `request-review` sends the agent's `git diff HEAD` plus bounded changed-file text. The service reads the diff and up to `max_tool_calls_per_activation` files (default 2, maximum 4), labels everything unverified, tells the model it executed nothing, and replies against the recorded requirements. It is a model with the requirement texts, not the oracle: in the Opus 5 episode it called the gold patch's own line "broader than the bug requires".
- Session binding. Sessions are created by the worker with a worker secret, only for `interactive`, bound to trial id, spec hash, condition and run-config hash; the token is an HMAC over that binding. Request ids bind to bodies: a repeat returns the cached reply; a different body is a 409. Agent input errors are 4xx, never service failures; model failures are 503 and recorded.
- Freeze. The root collector calls `POST /freeze` after killing the agent's processes; the hash over the canonical event list goes into the receipt, and the worker later marks `transcript_binding_valid`. One lock per session covers ask, review and freeze, so a slow reply for one trial never blocks another.

Deliberately not done: no `run_check` sandbox (review is `diff-only` and says so), no source debugging, no patch or algorithm suggestions (prompt-enforced, audited in the transcript), no influence on reward, no verification of the diff the agent chose to send, no forced drip beyond the granularity rule, and no claim of determinism; interactive episodes stay `unadjudicated` until a human labels them.

## Trust boundaries and network policy

| Boundary | Identity | Network | Sees |
|---|---|---|---|
| agent container | `agent` (unprivileged) | `[environment]` baseline and `[agent]` phase both `allowlist`: exactly the run's model API host plus the stakeholder hostname | repository at base with a base-only git object database, brief, root-owned shims, session token (interactive only) |
| root collect hook | `root`, `env -i`, absolute executables | same baseline; its only call is `POST /freeze` | the working tree; writes 0700 `/var/swesim-collect`; runs no git and no repository code |
| separate verifier | grader as `root`, tests as `tester` | `no-network` for image baseline and verify phase | `app.tar`, receipt, `/pristine`, hidden test patch, grader config |
| worker (VM) | service user | outbound to GitHub, model providers, Modal, stakeholder; listeners on loopback only | secrets from a 0600 env file; never enters a container |
| stakeholder service | single Modal container | inbound HTTPS from agent and worker; outbound to its model provider | the visible view only |

The agent toolchain is pre-baked because the strict baseline blocks package mirrors (the pypdf image had to gain `build-essential` after four trials failed at agent setup); collection outputs are file artifacts because Harbor's directory extraction aborts on absolute symlinks.

## Temporal and the VM

Temporal is thin and split in two so that making tasks and running them never share a workflow.

- `CreateTasksWorkflow` takes sources (`https://github.com/<owner>/<repo>` or `swebench:A[:<ids>]`), an approval mode (`auto` or `hold`), `max_candidates` and `max_parallel`. It probes the configured models (judge, draft and stakeholder fall back along per-role chains; judge and draft never resolve to the same id; the solver is never substituted), mines each source once (duplicate sources are refused), and fans out one `CandidateWorkflow` per candidate: judge, probe, draft, auto-approval or a review-queue wait of up to seven days for an `approve`/`reject` signal bound to the spec hash, compile, validate, register. A terminal outcome (`valid`, `invalid`, `rejected`, `not-reproducible-offline`, `probe-failed`) completes the child; a failed candidate can be rerun.
- `RunTasksWorkflow` takes scenario ids, solver models, conditions, `k` and the resolved stakeholder model, plans trials, and runs one child `TrialWorkflow` per attempt whose id is `trial_id = sha256(spec_hash | condition | run_config_hash | attempt_index)[:24]`; a duplicate start is rejected (`REJECT_DUPLICATE`, covered by an explicit Temporal integration test) and a retry is a new attempt index, never a Temporal retry. It then scores and exports.
- `PipelineWorkflow` and `RunMatrixWorkflow` are the older single-candidate compositions of the same activities and remain callable.

Launch-once is decided from evidence: the launch activity (one attempt, one launch at a time) reads `record.json`, `runner-error.json`, `status.json`, `launch.json` and the systemd unit state, and only on `launch` writes the request and an intent record, runs exactly one `systemd-run --user --unit swesim-trial-<id>` with `RemainAfterExit`, and records the acknowledgement. The unit re-derives the trial id from the frozen spec and exact `RunConfig`. The await activity is read-only and heartbeating and reports `completed`, `runner-error`, `launch-failed`, `unit-failed` or `unresolved`; unknown launch state is reconciled by a person, never relaunched.

The operator console is the dashboard (`swesim/dashboard/`): `/create` and `/run` start the two workflows through the Temporal client and print the CLI equivalent; `/jobs` and `/jobs/<wid>` show progress and cancel; `/review` and `/review/<id>` are the hold-mode queue (approve or reject against the spec hash; local drafts without an `approval.json` are listed too); `/scenarios`, `/scenario/<id>`, `/runs` and `/trial/<id>` are the product pages (briefs with removed spans highlighted, ledger by discovery, executed controls, pass@k per frozen run configuration, the merged agent-and-stakeholder timeline with the patience meter). Agent tool calls appear only after a trial completes, because Harbor writes the trajectory at the end. Temporal's own UI shows workflow state; the two are not duplicated.

Topology: one GCE VM (Debian 12) with user units for the Temporal dev server (SQLite file, loopback), the worker (queue `swesim`) and the dashboard, plus one transient unit per trial; UI, gRPC and dashboard reached through SSH forwards; no Docker on the VM. Sandboxes, builds and verifiers run on Modal through Harbor. `scripts/vm/update.sh` fast-forwards the checkout to `origin/main` and restarts only the worker and dashboard.

Measured on the VM (`workspace/evidence/vm/`): a `CreateTasksWorkflow` for seaborn-3190 ran judge, probe, auto-approve, compile and validate unattended from 03:19:28Z to 03:24:06Z (2026-09-13) and ended `invalid` on the then-unfixed plausible-wrong control; a `RunTasksWorkflow` ran from 03:25:35Z to 03:29:58Z, its trial `26f3304d9caedfe10cd75ebf` (Haiku 4.5, interactive, withheld) completing with reward 1.0. The worker was restarted mid-trial at 03:26:04Z: the run history shows worker identity PID 5096 on every task before the restart and PID 5647 on every task after it, and the trial unit's journal holds exactly one `Started swesim-trial-26f3304d…` line (03:25:35) and one completion line (03:29:54). The laptop-off window is `workspace/evidence/laptop_off_window.txt`: last SSH connection 03:46:03Z, first connection afterwards 04:10:46Z. Inside it, `RunTasksWorkflow` `run-42874b1b…` (history mirrored beside the others) started at 03:46:06Z, launched, polled and ingested two `TrialWorkflow` children (seaborn-3190 Haiku 4.5, `interactive|withheld` attempt 40 from 03:46:06Z to 03:50:54Z and `hidden|withheld` attempt 40 from 03:50:57Z to 03:55:31Z, both reward 1.0, one `Started` journal line each) and completed at 03:55:38Z. `workspace/evidence/ops.json` records both checks as passed and states the caveat honestly: the controlling session ran on the laptop, so its lid stayed open; the claim is that no SSH session or tunnel to the VM existed during the window.

## File layout

```
workspace/                        ignored by git
  candidates/<source>.jsonl       + .judged.jsonl, .judged.probed.jsonl, .probed.summary.json, .rejects.json, .status
  scenarios/<id>/                 scenario.draft.json, draft.meta.json, assets/, judge.json, scenario.json, approval.json,
                                  compile.json, validation.json, scores.json
  tasks/<id>__<condition>/        task.toml, instruction.md, environment/{Dockerfile,collect.py,shims/},
                                  tests/{Dockerfile,test.sh,grader.py,policy.py,parsers.py,run_pytest.py,
                                  test.patch,grader_config.json}, solution/{solve.sh,oracle.diff}, controls/*.diff
  jobs/{validate,runs,probe}/     Harbor job directories
  trials/<trial_id>/              request.json, launch.json, task/, status.json, run_config.json, spec.json,
                                  reward.json, report.json, transcript.json, record.json, label.json
  exports/<trial_id>.episode.json
  evidence/                       stage logs, judge_calibration.json, vm/ (mirrored VM evidence)
registry.json                     tracked at the repo root; keyed by scenario id; valid scenarios only (three today)
examples/<scenario_id>/           tracked, sanitized snapshot: manifest.json with digests, the three task dirs, executed
                                  controls, two completed runs (seaborn-3190 today)
```

## Code-owned versus model-generated

| Step | Model | Code |
|---|---|---|
| mine | none | discovery, filters, thread fetch, build |
| judge | proposed gaps, rubric, complexity, behavioralness | evidence verification against test files and patch, acceptance rule, model and prompt ids recorded |
| probe | none | everything: base, whole-file and exact-id stages, dropping rules, binding |
| draft | facts, persona, requirements, checks, groups, plausible-wrong and alternative-correct patches, spans to withhold | natural and withheld briefs, oracle and test assets, tamper controls, control verification and fallback, control names, protected paths, F2P linkage, manifest, runner order, grounding and leak checks, schema |
| approve, compile, validate, register | none | everything |
| run | solver agent; stakeholder replies | session protocol, patience, transcript, freeze, collection, binding, scaffold config hash |
| grade | none | everything; submitted code runs only inside pytest as `tester` |
| score, export | none (optional human labels) | metrics, eligibility, lineage |

## What we learned running it

- Published test lists are not an offline contract. SWE-bench's FAIL_TO_PASS for sphinx-9467 passed on the untouched base offline; requests-1944's suite needed a network server. Measuring base versus gold in the verifier's own conditions, before drafting, is now a stage, not a check.
- Whole-file runs hide test pollution. sklearn-13584 was fully correct under every control and still invalid because one PASS_TO_PASS test failed only when FAIL_TO_PASS ids ran first. Grading order and probe order must be the same command.
- A model-authored control is a hypothesis until it applies. Two sweeps (pypdf-3826 on the laptop, seaborn-3190 on the VM) failed on corrupt plausible-wrong diffs; `git apply --check` at draft time costs seconds and each Modal sweep costs minutes.
- Models will protect the wrong files. Given a say over protected paths, the drafter shielded the gold patch's source file; the list is now computed from the contract and checked against the gold paths.
- The gates reject more than they accept, and that is the product. Of the nine candidates that reached a draft or a sweep, three are valid; the other six stopped on network tests, an unreproducible contract, a missing or incorrect alternative-correct control, a verifier image guard, an unreliable exact-id probe, or a non-verbatim excerpt. None was hand-edited past a gate.
- Asking has to be part of the agent's procedure, not only the task text. Haiku 4.5 asked nothing in nine trials under two instruction wordings; Sonnet 5 and Opus 5 asked once the instruction said the brief was intentionally incomplete; Haiku asked only after the agent scaffold listed asking as a workflow step, and then in one trial of two. The scaffold is recorded per trial so the arms stay separable.
- The diff-only reviewer is not an oracle. On the Opus 5 episode it flagged the gold patch's own line as too broad. Review is unverified by contract and never touches reward; its value is measured, not assumed.
- Infrastructure failures must be coded, not scored. Four pypdf trials failed before the agent ran (the harness needed a compiler); they are outcome 7, outside every denominator, and the image was fixed. Seven matplotlib-25547 controls failed the same way and the scenario is invalid rather than accepted by default.

## Known limitations and residual risks

- In-process report forging (CLAUDE.md; verifier review). Code under test runs inside pytest and could forge the collected-id manifest, JUnit, terminal summary and exit code coherently, including from an `atexit` handler after the real summary. Isolated startup, protected hooks and configs, the four-way cross-check and per-process reruns are tested defenses, not a proof.
- Hard-link edge case (verifier fix-round review). In in-place grading a base tree containing a hard link would let a write to one name change the other; git cannot produce one and submitted hard links are rejected, and the suggested hardening is not applied.
- Base repositories with symlinks. The verifier image build rejects them for the pilot. matplotlib-25547 is the observed case: every control failed at the verifier image build guard, so the scenario is invalid and the repository is not usable until the rule is revisited.
- Stakeholder durability. One container keeps the in-process locks authoritative; a redeploy during an episode could interleave, Modal's web-request buffering against slow replies is untested, and Volume commit/reload under concurrent trials is not yet measured. `disclosed_fact_ids` has not been audited against the ledger.
- Yield. Judge-v3 keeps 24 of 45 pytest-eligible pool A rows, but of the six SWE-bench candidates carried through probe and draft only two are valid; the repository-URL path found one valid bugfix candidate in one repository and none in the feature band. The five calibration rows that exceeded the authoring budget are the largest instances, which the pipeline cannot currently judge at all.
- Small n. Every measured arm has n between 1 and 4; pass@3 is defined for two groups. No claim about the effect of withholding or of interaction is supported by the data on disk.
- The interactive instruction and the scaffold are confounded with time and with the seaborn scenario: all three asking trials are on seaborn-3190 under the second wording. The scaffold's own effect rests on two Haiku trials.
- Outcome 6 (`simulator-failure`) is never assigned automatically; the run stage records service-side evidence for adjudication. It has not occurred.
- The laptop-off and worker-restart checks are both evidenced (`workspace/evidence/ops.json`), with the stated caveat that the laptop's lid stayed open and the claim is the absence of any SSH session or tunnel during the window. `registry.json` records the seaborn build hash from its first registration, not the recompiled build the later trials ran; `register` has not been re-run.

## How this differs from prior art

SWE-bench Verified (OpenAI with the SWE-bench authors) filtered out issues humans rated underspecified; swesim starts from those labels and keeps the natural issue as a measured profile. Ambig-SWE (Vijayvargiya et al., ICLR 2026) removed information from Verified issues with an LLM summary and gave a simulated user the full issue, hints and file locations; swesim removes by category with each removal recorded as a verbatim fact, grounds every stakeholder fact in a quoted thread span with a URL, and gives the stakeholder no file locations or evaluator material. "Ask or Assume?" (Edwards and Schuster, 2026) is an agent-side clarification scaffold; swesim is the environment side: a hosted stakeholder with a code-owned patience budget, a frozen transcript bound to collection, and exports separating what the policy observed from what the evaluator knows. DeepSWE v1.1 (2026) supplies the grading pattern swesim follows (separate verifier, collected tree, stripped history, per-test reports); swesim adds the protected-path policy, exact-id evidence reconciliation, partial credit by requirement group, and executed controls that gate whether a task exists. tau-bench (Sierra) supplies pass^k. No categorical novelty is claimed.
