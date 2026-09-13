# swesim architecture

## Purpose and thesis

swesim turns naturally underspecified GitHub issues into interactive software-engineering tasks. SWE-bench hands an agent the issue as the reporter wrote it and grades a patch with the fix's tests; SWE-bench Verified removed the instances humans rated underspecified. swesim keeps those issues as the brief, moves the information that resolved them (maintainer decisions from the PR thread and review, plus details withheld from the reporter's own text) into a simulated stakeholder the agent can ask, and grades the submitted tree offline with the fix's tests in a container the agent never enters. The product is a pipeline: from a SWE-bench annotated pool or a GitHub repository URL it mines candidates, judges the specification gap, drafts a provenance-tagged scenario record, approves it through code-owned gates, compiles three Harbor task variants, validates them with executed controls on Modal, and runs, scores and exports trials, orchestrated thinly by Temporal from a VM so nothing waits for the laptop.

## Components

```
 sources ── swebench-annotated (pool A CSV, dataset, GitHub threads) | github-repo (merged PRs, built Dockerfile)
    │
    ▼  PipelineWorkflow (Temporal, thin; payloads are ids, hashes, paths)
 mine → judge → draft → approve → compile → validate → register → plan_trials → TrialWorkflow → score → export
                                                                        │ one systemd unit per trial
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
 └────────────────────────────┘                           └────────────────────────────┘
```

## Data flow

Every stage is a plain function writing under `workspace/`; Temporal activities wrap them and the CLI calls them directly. After `draft`, the only handoff is the validated `ScenarioSpec` (`swesim/models.py`, strict Pydantic) plus a hashed `RunConfig`.

| Stage | What it does | Output |
|---|---|---|
| mine | `swebench-annotated`: pool A is `underspecified >= 2`, `false_negative <= 1`, `other_major_issues == 0` over the ensembled annotation CSV (109 ids), filtered to pytest-run repositories whose tests do not need the network; rows carry the issue verbatim, F2P/P2P ids, gold and test patches, image name, annotation and PR thread. `github-repo`: lists merged PRs since a date, resolves linked issues in three tiers (closing keyword, timeline cross-reference, plain mention, recorded as `link_kind`), keeps pairs with code and test changes whose code-only size is in band (feature: 200 to 1,500 lines, 3 to 30 files, at least 8 comments, a feature-type label; bugfix: 20 to 600 lines, at least 4 comments) and whose issue predates the PR, writes a reject histogram, and probes up to `--max-builds` candidates on Modal to derive F2P/P2P from a real build. | `candidates.jsonl`, `.rejects.json`, `.status` |
| judge | Code extracts candidate gaps the tests or fix require but the issue never mentions; a model classifies each gap and scores the Verified rubric; acceptance is code-owned (one gap non-derivable, consequential and thread-resolved; task self-contained). | `.judged.jsonl` |
| draft | A second model (never the judge's) authors facts, persona, requirements, checks, groups and a plausible-wrong patch. Code owns the natural brief, oracle and test assets, tamper controls, manifest, runner command, verbatim grounding of every fact, the withholding leak check and the schema. One correction round, then failure. | `scenario.draft.json`, `assets/`, `judge.json` |
| approve | Six code-owned gates (see `docs/grading.md`); records `approved_by: auto-validators, review_needed: true`. A leak rejection re-drafts once; anything else takes the next candidate. Drafts are never hand-edited. | `scenario.json`, `approval.json` |
| compile | Three Harbor 0.23 task directories from one spec: pinned image digest (a mutable tag is refused), both Dockerfiles, `task.toml`, `instruction.md`, collector, shims, grader and config, oracle and control diffs. `compiled_hash` covers spec, digest, agent version, templates, verifier and shims. | `tasks/<id>__{full,hidden,interactive}/`, `compile.json` |
| validate | Control sweep and reachability probe on Modal; base and gold evidence recorded; any mismatch is `invalid`. | `validation.json` |
| register | Only a `valid` scenario with matching hashes enters the registry and dataset manifest. | `registry.json`, `dataset.json` |
| run | Per trial: stakeholder session (interactive only), a per-trial task copy with token and trial id, Harbor launch, verifier files, transcript fetch and hash binding. | `trials/<trial_id>/*.json` |
| score | Outcome counts, pass@k and pass^k, mean reward, interaction counts, labels; attempts without a record are listed as unresolved and never enter `n`. | `scores.json` |
| export | Episode manifest separating policy-visible from evaluator-only material, with lineage and eligibility. | `exports/<trial_id>.episode.json` |

## Conditions and brief profiles

| Condition | Brief | Stakeholder | Measures |
|---|---|---|---|
| `full` | natural issue plus a "Requirements (complete)" list of every stakeholder-visible fact; author-only facts excluded by construction | none | the ceiling: the agent with all disclosable information |
| `hidden` | the run's profile (`withheld` when present, else `natural`) | none | the floor: the issue alone |
| `interactive` | the same brief as `hidden` | `ask-user`, `request-review`, patience budget | the treatment: obtaining and using facts through interaction |

`natural` is the issue exactly as filed and is always kept. `withheld` removes specific categories from it (reproduction code, error messages and tracebacks, literal expected outputs, file and line references, version pins, explicit fix proposals) and never summarizes; every removal is a ledger fact carrying the removed text verbatim. `hidden` and `interactive` share a brief, so their difference is the stakeholder alone; running `hidden` on both profiles reports the cost of withholding separately, which is wired but not yet measured.

## The stakeholder

A hosted FastAPI service on Modal (`swesim/stakeholder/`): one deployment, one JSON snapshot per trial committed before any reply is returned. The transcript is never an agent artifact; the worker fetches it after the trial and checks its hash against the receipt.

- Knowledge. The worker builds the visible view from the spec: natural brief, non-author-only facts, requirement texts, persona, check descriptions. It never touches assets, controls or the contract; the service rejects a view containing an author-only fact.
- Patience budget. Code-owned, part of the `RunConfig` (`start 6, question_cost 1, review_cost 2, terse_below 3, refuse_at 0` by default), spent per reply. Above `terse_below` the model answers in full; at or below it the prompt asks for one or two sentences and deflects repo-derivable facts to the code; at or below `refuse_at`, or when the cost is unaffordable, a fixed refusal is returned without a model call. Asking changes availability, never reward.
- Disclosure. Any on-request fact may be given when a related question is asked, at the specificity asked: a broad question gets the requirement list, an exact exception type needs an exact question. `disclosed_fact_ids` is a model self-report kept worker-only; the agent sees reply text, remaining patience and tier.
- Diff-only review. `request-review` sends the agent's `git diff HEAD` plus bounded changed-file text. The service reads the diff and up to `max_tool_calls_per_activation` files (default 2, maximum 4), labels everything unverified, tells the model it executed nothing, and replies against the recorded requirements.
- Session binding. Sessions are created by the worker with a worker secret, only for `interactive`, bound to trial id, spec hash, condition and run-config hash; the token is an HMAC over that binding. Request ids bind to bodies: a repeat returns the cached reply; a different body is a 409. Agent input errors are 4xx, never service failures; model failures are 503 and recorded.
- Freeze. The root collector calls `POST /freeze` after killing the agent's processes; the hash over the canonical event list goes into the receipt, and the worker later marks `transcript_binding_valid`. One lock per session covers ask, review and freeze.

Deliberately not done: no `run_check` sandbox (review is `diff-only` and says so), no source debugging, no patch or algorithm suggestions (prompt-enforced, audited in the transcript), no influence on reward, no verification of the diff the agent chose to send, no forced drip beyond the granularity rule, and no claim of determinism; interactive episodes stay `unadjudicated` until a human labels them.

## Trust boundaries and network policy

| Boundary | Identity | Network | Sees |
|---|---|---|---|
| agent container | `agent` (unprivileged) | `[environment]` baseline and `[agent]` phase both `allowlist`: exactly the run's model API host plus the stakeholder hostname | repository at base with a base-only git object database, brief, root-owned shims, session token (interactive only) |
| root collect hook | `root`, `env -i`, absolute executables | same baseline; its only call is `POST /freeze` | the working tree; writes 0700 `/var/swesim-collect`; runs no git and no repository code |
| separate verifier | grader as `root`, tests as `tester` | `no-network` for image baseline and verify phase | `app.tar`, receipt, `/pristine`, hidden test patch, grader config |
| worker (VM) | service user | outbound to GitHub, model providers, Modal, stakeholder; listeners on loopback only | secrets from a 0600 env file; never enters a container |
| stakeholder service | single Modal container | inbound HTTPS from agent and worker; outbound to its model provider | the visible view only |

The agent toolchain is pre-baked because the strict baseline blocks package mirrors; collection outputs are file artifacts because Harbor's directory extraction aborts on absolute symlinks.

## Temporal and the VM

Temporal is thin. `PipelineWorkflow` probes the configured models (judge, draft and stakeholder fall back along per-role chains; the solver is never substituted), then mines, judges and, per candidate, drafts, auto-approves, verifies the approval hash, compiles, validates, registers, plans trials, runs child `TrialWorkflow`s, scores and exports, bounded by `max_scenarios` and `max_candidates_tried`. A supervised mode waits up to 24 hours for an `approve_spec` signal. `TrialWorkflow`'s id is `trial_id = sha256(spec_hash | condition | run_config_hash | attempt_index)[:24]`; a duplicate start is rejected and a retry is a new attempt index, never a Temporal retry.

Launch-once is decided from evidence: the launch activity (one attempt, one launch at a time) reads `record.json`, `runner-error.json`, `status.json`, `launch.json` and the systemd unit state, and only on `launch` writes the request and an intent record, runs exactly one `systemd-run --user --unit swesim-trial-<id>` with `RemainAfterExit`, and records the acknowledgement. The unit re-derives the trial id from the frozen spec and exact `RunConfig`. The await activity is read-only and heartbeating and reports `completed`, `runner-error`, `launch-failed`, `unit-failed` or `unresolved`; unknown launch state is reconciled by a person, never relaunched.

Topology: one GCE VM (Debian 12) with user units for the Temporal dev server (SQLite file, loopback), the worker (queue `swesim`, four concurrent activities) and the dashboard, plus one transient unit per trial; UI, gRPC and dashboard reached through SSH forwards; no Docker on the VM. Sandboxes, builds and verifiers run on Modal through Harbor. The VM exists and its units are active; the laptop-off run and worker-restart check are not yet measured.

## File layout

```
workspace/                        ignored by git
  candidates/<source>.jsonl       + .judged.jsonl, .rejects.json, .status
  scenarios/<id>/                 scenario.draft.json, assets/, judge.json, scenario.json, approval.json,
                                  compile.json, validation.json, scores.json
  tasks/<id>__<condition>/        task.toml, instruction.md, environment/{Dockerfile,collect.py,shims/},
                                  tests/{Dockerfile,test.sh,grader.py,policy.py,parsers.py,run_pytest.py,
                                  test.patch,grader_config.json}, solution/{solve.sh,oracle.diff}, controls/*.diff
  jobs/{validate,runs,probe}/     Harbor job directories
  trials/<trial_id>/              request.json, launch.json, task/, status.json, run_config.json, spec.json,
                                  reward.json, report.json, transcript.json, record.json, label.json
  exports/<trial_id>.episode.json
registry.json                     tracked at the repo root; keyed by scenario id; valid scenarios only
```

`registry.json` and a tracked `examples/` package do not exist yet because no scenario has passed validation.

## Code-owned versus model-generated

| Step | Model | Code |
|---|---|---|
| mine | none | discovery, filters, thread fetch, F2P/P2P probe |
| judge | gap classification, rubric, complexity, behavioralness | candidate-gap extraction, acceptance rule, model and prompt ids recorded |
| draft | facts, persona, requirements, checks, groups, plausible-wrong and alternative-correct patches, withheld spans and text (validated) | natural brief, oracle and test assets, tamper controls, manifest, runner, grounding and leak checks, schema |
| approve, compile, validate, register | none | everything |
| run | solver agent; stakeholder replies | session protocol, patience, transcript, freeze, collection, binding |
| grade | none | everything; submitted code runs only inside pytest as `tester` |
| score, export | none (optional human labels) | metrics, eligibility, lineage |

## Known limitations and residual risks

- In-process report forging (CLAUDE.md; verifier review). Code under test runs inside pytest and could forge the collected-id manifest, JUnit, terminal summary and exit code coherently, including from an `atexit` handler after the real summary. Isolated startup, protected hooks and configs, the four-way cross-check and per-process reruns are tested defenses, not a proof.
- Hard-link edge case (verifier fix-round review). In in-place grading a base tree containing a hard link would let a write to one name change the other; git cannot produce one and submitted hard links are rejected, and the suggested hardening is not applied.
- Base repositories with symlinks. The verifier image build rejects them for the pilot; a base that carried them would emit `symlink-traversal` or `rejected-archive-member` signals on every submission.
- Stakeholder durability. One container keeps the in-process locks authoritative; a redeploy during an episode could interleave, Modal's web-request buffering against slow replies is untested, and Volume commit/reload under concurrent trials is not yet measured.
- On stream branches, not yet merged: a reachability probe that ignores base copies of added lines (the merged probe flagged `build/lib` copies on requests-1944), a withheld brief computed by code from model-selected spans, a submodule guard in both images, and a judge that verifies model-proposed gaps against the thread.
- Yield. Over the 44 pytest-eligible pool A rows on disk the judge kept none (one model error); an earlier shortlist pass kept one, which then failed validation. The first bounded `github-repo` discovery over one repository ended in `no-valid-candidate` (188 PRs: 160 without a linked issue, 22 too small, 6 other rejections); the plain-mention tier was added afterwards and has not been re-run.
- Outcome 6 (`simulator-failure`) is never assigned automatically; the run stage records service-side evidence for adjudication.
- Nothing graded has run end to end: no trial in any condition, no interactive episode, no laptop-off or restart check. The spikes under `references/modal-smoke-task/` proved the topology, not the hardened collector; the requests-1944 sweep is the first evidence that the root-only collection directory and file artifacts reach the verifier on Modal.

## How this differs from prior art

SWE-bench Verified (OpenAI with the SWE-bench authors) filtered out issues humans rated underspecified; swesim starts from those labels and keeps the natural issue as a measured profile. Ambig-SWE (Vijayvargiya et al., ICLR 2026) removed information from Verified issues with an LLM summary and gave a simulated user the full issue, hints and file locations; swesim removes by category with each removal recorded as a verbatim fact, grounds every stakeholder fact in a quoted thread span with a URL, and gives the stakeholder no file locations or evaluator material. "Ask or Assume?" (Edwards and Schuster, 2026) is an agent-side clarification scaffold; swesim is the environment side: a hosted stakeholder with a code-owned patience budget, a frozen transcript bound to collection, and exports separating what the policy observed from what the evaluator knows. DeepSWE v1.1 (2026) supplies the grading pattern swesim follows (separate verifier, collected tree, stripped history, per-test reports); swesim adds the protected-path policy, exact-id evidence reconciliation, partial credit by requirement group, and executed controls that gate whether a task exists. tau-bench (Sierra) supplies pass^k. No categorical novelty is claimed.
