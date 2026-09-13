# swesim

swesim turns naturally underspecified GitHub issues into interactive, gradable software-engineering tasks. SWE-bench hands an agent the issue as the reporter wrote it and grades a patch with the fix's tests; SWE-bench Verified removed the instances humans rated underspecified. swesim keeps those issues as the brief, moves the information that actually resolved them (maintainer decisions from the PR thread and review, plus details withheld from the reporter's own text) into a simulated stakeholder the agent can ask, and grades the submitted tree offline with the fix's tests in a container the agent never enters. Every stakeholder fact quotes a verbatim span of the real thread; asking changes what the agent knows, never the reward. Tasks are [Harbor](https://harborframework.com) 0.23 task directories run on Modal, Temporal orchestrates creation and runs from a VM so nothing waits for a laptop, and a FastAPI dashboard is the operator console.

## Pipeline

```
swebench-annotated (pool A)  \                                                +-> full         natural brief + every stakeholder-visible fact
github-repo URL (merged PRs)  +-> mine -> judge -> probe -> draft -> approve -> compile -+-> hidden       natural or withheld brief, no stakeholder
                              /          (LLM)   (Modal:    (LLM)   (6 code-   (3 Harbor    +-> interactive  same brief + ask-user / confirm-behavior
                                                 base/gold)          owned)     tasks)             |
validate (nop, oracle, plausible-wrong, tamper, reachability; all on Modal) -> register -> run ----+  harbor run -e modal
   |                                                                                               v
   |   agent sandbox (Modal, user `agent`) <-- ask-user / confirm-behavior over HTTPS --> stakeholder service (Modal, patience budget)
   |   root collect hook: kill agent processes, tar the tree, POST /freeze, write receipt                    |  GET /transcript
   +-> separate verifier (Modal, no network): reward.json + report.json ----------------> worker: bind transcript hash, record.json
                                       -> score (pass@k, pass^k, mean reward) -> export (policy-visible vs evaluator-only) -> dashboard
```

## Prerequisites

- `uv`; it installs Python 3.12 from `.python-version`. Harbor 0.23.0, Modal 1.5.5 and the Temporal SDK are project dependencies. No Docker: every build, sandbox and verifier runs on Modal.
- A Modal token in `~/.modal.toml` (`uv run modal setup`).
- `gh auth login`: mining reads issue and PR threads through the GitHub API.
- A git-ignored dotenv file, mode 0600, at `~/Documents/.env` or the path in `SWESIM_ENV_FILE`, read with python-dotenv (never shell-sourced). Keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `SWESIM_WORKER_SECRET` (a long random string shared with the stakeholder deployment), `SWESIM_STAKEHOLDER_URL` (filled after deploying), and for the VM path `GH_TOKEN` (read-only, from `gh auth token`), `GCP_PROJECT`, `GCP_ZONE`.
- `gcloud` authenticated for that project (VM path only).

## Quick start on a laptop

> **Runs and task creation go through Temporal, never the stage commands.** The operator entry points are `swesim create`
> (task creation from repo URLs or the SWE-bench pool), `swesim run-tasks` (graded trials on selected scenarios and models),
> `swesim workflows list|progress|cancel|approve`, and the console's Create/Run/Jobs/Review pages, all executed by the VM
> worker with launch-once trial identity and reconciliation. The stage-by-stage commands shown below with `SWESIM_DIRECT=1`
> are the building blocks those workflows call; they are hidden from `--help` and refuse to run without that variable. Use
> them only to debug one stage. The trials recorded during the first night were run this way and are labelled as such.

```bash
uv sync --extra dev --frozen
uv run --frozen --offline python -m pytest -q tests      # offline suite
bash scripts/verify.sh 3                                  # cumulative acceptance for milestones 1..N (1-10); never launches cloud jobs
uv run swesim --help
bash scripts/deploy_stakeholder.sh                        # Modal Secret swesim-llm from the dotenv file, deploy, write .stakeholder_url (0600), curl /health
```

`verify.sh N` runs the milestone's tests plus evidence checks: stdlib-only verifier imports (2), consistent Modal-backed `validation.json` files (4), an interactive trial with a paired question/reply and bound transcript (5), a validated GitHub-source scenario (6), registry binding, reproducible scores and exports (7), laptop-off and restart evidence (8), tracked example, docs and a credential scan (10).

### SWE-bench path

Scenario ids are the instance id lower-cased with `__` replaced by `--`.

```bash
SWESIM_DIRECT=1 uv run swesim mine --source swebench-annotated --pool A --instance scikit-learn__scikit-learn-13584
SWESIM_DIRECT=1 uv run swesim judge workspace/candidates/swebench-annotated.jsonl --model openai/gpt-5.5
SWESIM_DIRECT=1 uv run swesim probe --judged workspace/candidates/swebench-annotated.judged.jsonl --instance scikit-learn__scikit-learn-13584
SWESIM_DIRECT=1 uv run swesim draft workspace/candidates/swebench-annotated.judged.probed.jsonl scikit-learn__scikit-learn-13584 --model anthropic/claude-sonnet-5
SWESIM_DIRECT=1 uv run swesim approve --auto scikit-learn--scikit-learn-13584
SWESIM_DIRECT=1 uv run swesim compile scikit-learn--scikit-learn-13584
SWESIM_DIRECT=1 uv run swesim validate scikit-learn--scikit-learn-13584
uv run swesim register scikit-learn--scikit-learn-13584
SWESIM_DIRECT=1 uv run swesim run scikit-learn--scikit-learn-13584 --condition interactive --attempt 0 --model anthropic/claude-haiku-4-5-20251001
uv run swesim score scikit-learn--scikit-learn-13584 --k 3
uv run swesim export scikit-learn--scikit-learn-13584
```

`mine` writes `workspace/candidates/swebench-annotated.jsonl` (issue verbatim, published test ids, gold and test patches, image, thread); omit `--instance` for the whole pool. `judge` writes `.judged.jsonl`, keeping a row only when a gap is non-derivable, consequential and resolved in the thread. `probe` measures FAIL_TO_PASS and PASS_TO_PASS offline on Modal (base versus gold) into `.judged.probed.jsonl`; the drafter reads the measured lists, and an empty list ends the candidate as not reproducible offline. `approve --auto` exits 1 with reasons. `compile` takes the stakeholder URL from `--stakeholder-url`, `SWESIM_STAKEHOLDER_URL` or `.stakeholder_url` and allowlists its host; `run` refuses an interactive trial without it and reads `SWESIM_WORKER_SECRET` from the dotenv file. `validate` exits 0 only when every control holds; `register` accepts only a valid scenario. Repeat `run` with a fresh `--attempt`; `--condition full|hidden` and `--brief-profile natural|withheld` select the other arms.

### Repository-URL path

```bash
SWESIM_DIRECT=1 uv run swesim mine --source github-repo --repo https://github.com/py-pdf/pypdf --since 2026-06-01 --max-prs 400 --max-builds 3 --kind bugfix
SWESIM_DIRECT=1 uv run swesim judge workspace/candidates/github-repo--py-pdf__pypdf.jsonl
SWESIM_DIRECT=1 uv run swesim probe --judged workspace/candidates/github-repo--py-pdf__pypdf.judged.jsonl --instance py-pdf__pypdf-3826
SWESIM_DIRECT=1 uv run swesim draft workspace/candidates/github-repo--py-pdf__pypdf.judged.probed.jsonl py-pdf__pypdf-3826
```

Then `approve --auto`, `compile`, `validate`, `register`, `run`, `score`, `export` on `py-pdf--pypdf-3826`. Discovery lists merged PRs, resolves linked issues (closing keyword, timeline reference or plain mention, tier recorded), keeps code-plus-test PRs in band (`feature`: 200 to 1,500 code lines, `bugfix`: 20 to 600), writes a rejection histogram to `.rejects.json`, and builds the first `--max-builds` candidates on Modal to derive test lists from a real Dockerfile. `--pr N` pins one PR (recorded as `repo_only=false`); `probe-tests` re-probes one candidate.

### Orchestrated path

These commands talk to Temporal at `TEMPORAL_ADDRESS` (default `localhost:7233`), on the VM or through the tunnel.

```bash
uv run swesim create --source https://github.com/py-pdf/pypdf --source swebench:A --pr-kind bugfix --approval-mode auto --max-candidates 20 --max-parallel 8
uv run swesim workflows list create                    # create | candidate | run | pipeline | matrix | trial | all
uv run swesim workflows progress create-<hex>
uv run swesim workflows approve create-<hex> <scenario_id> <spec_hash> --decision approve --note "..."
uv run swesim run-tasks --scenario-ids <id1>,<id2> --models anthropic/claude-haiku-4-5-20251001 --conditions full,hidden,interactive --k 3 --stakeholder-model <resolved id>
uv run swesim workflows cancel <workflow_id>
```

`create` starts one `CreateTasksWorkflow` that fans out a `CandidateWorkflow` per candidate (judge, probe, draft, approval, compile, validate, register); `swebench:A:<id1>,<id2>` restricts a pool. `run-tasks` starts a `RunTasksWorkflow` whose `TrialWorkflow` children use the trial id as workflow id and reject duplicates, then scores and exports. `swesim worker` is the Temporal worker (`--launcher subprocess` is local smoke only). `pipeline`, `approve-signal`, `pipeline-progress` and `matrix` are older single-candidate compositions of the same activities.

### Dashboard

`uv run swesim dashboard --host 127.0.0.1 --port 8080`. Pages: `/` overview; `/scenarios` and `/scenario/<id>` (briefs with removed spans highlighted, ledger by discovery, controls and validation, judge verdict); `/runs` (filterable attempts, pass@k and pass^k per frozen run configuration); `/trial/<trial_id>` (merged timeline of agent steps and stakeholder conversation, verifier outcome, lineage, reviewer label); `/create` and `/run` (forms that start the two workflows and print the CLI equivalent); `/jobs` and `/jobs/<wid>` (progress, cancel); `/review` and `/review/<id>` (the review queue). Append `?evaluator=1` for evaluator-only material. Approval modes: `auto` lets the six gates decide and records `approved_by: auto-validators, review_needed: true` for a later human pass; `hold` parks each draft as `awaiting-approval` for up to seven days until someone approves or rejects it from the queue or with `workflows approve`. Decisions are bound to the draft's spec hash; a stale hash is refused. The queue also lists local drafts without an `approval.json`.

## Authoring a new simulation

The pipeline authors automatically. Code extracts the issue-to-contract delta and a judge model classifies each gap; code measures the contract offline. A second model (never the judge's) drafts the fact ledger, persona, requirements, checks, groups and a plausible-wrong patch; code writes the natural brief, computes the withheld brief from model-selected spans (removing reproduction code, tracebacks, literal outputs, file references, version pins and fix proposals, never summarizing), generates the oracle, test and tamper assets, and checks that every non-authored fact quotes a verbatim span of the text its provenance names. One correction round, then the draft fails honestly.

A human reviews `workspace/scenarios/<id>/scenario.draft.json` or `/review/<id>`: both briefs and what was removed; each fact's text, kind, provenance, `source_excerpt`, `source_url`, discovery (`given`, `on-request`, `author-only`) and linked tests; persona; requirements and group weights; test ids and protected paths; each control's rationale. `approve --auto` checks six gates: strict schema; assets with declared bytes and sha256 inside the scenario directory; judge acceptance; an on-request fact linked to a FAIL_TO_PASS test (otherwise asking is not load-bearing); no withheld excerpt surviving in the withheld brief; controls present (a plausible-wrong patch and an incorrect-exploit tamper; structural tests also need an alternative-correct patch). `compile` refuses an unpinned image digest, a non-HTTPS stakeholder URL or an unpinned agent version.

To reject, send `--decision reject --note` in hold mode; locally the queue writes `review.json`. To re-draft, run `swesim draft` again or take the next candidate. Drafts are never hand-edited to pass a gate.

Controls decide whether a task exists. `validate` applies each on Modal: `nop` must earn 0 with FAIL_TO_PASS failing and PASS_TO_PASS passing on base; `oracle` must earn `reward 1, success 1`; `plausible-wrong` must fail below its declared reward ceiling; `alternative-correct` must pass; each `incorrect-exploit` tamper (root conftest no-op, skip marker, sitecustomize) must earn exactly 0; the reachability probe must find no gold lines. An infrastructure outcome is never a passing control.

## Running on the VM

```bash
bash scripts/vm/create.sh     # create or reuse the GCE VM, ship a rendered worker env and ~/.modal.toml, run setup.sh at the pushed HEAD
bash scripts/vm/tunnel.sh     # forward 8233 (Temporal UI), 7233 (gRPC), 8080 (dashboard) to localhost
bash scripts/vm/update.sh     # fast-forward /opt/swesim to origin/main, uv sync, restart worker and dashboard only
```

`create.sh` copies only `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `SWESIM_WORKER_SECRET`, `SWESIM_STAKEHOLDER_URL` and `GH_TOKEN` into `/etc/swesim/env` (0600). `update.sh` refuses while HEAD has commits `origin/main` lacks and never restarts the Temporal server or a trial. User units with lingering: `swesim-temporal` (dev server, SQLite, loopback only), `swesim-worker` (queue `swesim`), `swesim-dashboard`, plus one transient `swesim-trial-<trial_id>` per trial created once by `systemd-run`; a worker restart cannot kill a trial and never relaunches one. Evidence lands under `/opt/swesim/workspace/` (`trials/`, `exports/`, `evidence/` with `model-probe.json` and `ops.json`) and in `journalctl --user -u swesim-trial-<trial_id>`. Retries are new attempt indices; `docs/ops.md` has the reconciliation rules.

## Layout

```
swesim/            cli.py, models.py, stages/, sources/, verifier/, stakeholder/, workflows/, dashboard/, templates/
scripts/           verify.sh, deploy_stakeholder.sh, vm/ (create, setup, update, tunnel, three unit files)
data/annotations/  SWE-bench Verified human annotations (pool A: underspecified >= 2, valid tests)
references/        DeepSWE task example, Harbor swebench adapter template, early Modal smoke task
docs/              architecture.md, grading.md, ops.md, superpowers/specs, superpowers/plans, notes/
workspace/         ignored: candidates/ (.jsonl, .judged, .probed, .rejects.json), scenarios/<id>/ (scenario.draft.json, judge.json,
                   assets/, scenario.json, approval.json, compile.json, validation.json, scores.json), tasks/<id>__<condition>/,
                   probes/, jobs/, trials/<trial_id>/, exports/, evidence/, judge_calibration.json
registry.json      tracked at the root once a scenario validates: per scenario id, version, spec hash, validation status
examples/          tracked snapshot of seaborn-3190 v2: manifest.json with digests, three task dirs, three sanitized runs
```

`registry.json` is tracked and holds the three valid scenarios; `examples/mwaskom--seaborn-3190/` is the tracked example package (manifest with sha256 per file, three compiled task variants, three sanitized runs including the submitted patch: two v2 interactive runs with a question and a behavior confirmation, and a hidden run).

## Docs

`docs/architecture.md` (components, data flow, conditions, stakeholder, trust boundaries, Temporal), `docs/grading.md` (reward contract, outcome codes, pass@k and pass^k, evidence reconciliation, protected paths, control sweep, measured results), `docs/ops.md` (VM topology, deploy, laptop-off and restart procedures, retry lineage). `PLAN.md` lists milestones.

## Status and honesty

Measured so far, from artifacts on disk (2026-09-13 UTC; the ledger is `.superpowers/sdd/2026-09-12-swesim-implementation/progress.md`, the numbers are in `docs/grading.md`):

- Three scenarios are `valid` and registered in `registry.json` and `workspace/dataset.json`: `mwaskom--seaborn-3190` (SWE-bench pool A; 1 FAIL_TO_PASS, 84 PASS_TO_PASS), `scikit-learn--scikit-learn-13584` (pool A; 6 and 3) and `py-pdf--pypdf-3826` (repository URL, discovered without a PR override; 2 and 95). Each passed the judge, the offline probe, all six auto-approval gates (`approved_by: auto-validators`, `review_needed: true`) and an executed sweep on Modal: oracle `reward 1.0, success 1`, `nop` 0, plausible-wrong 0 with `success 0`, each of the three incorrect-exploit tampers exactly 0 with one `protected-path-edit` signal, gold commit unreachable, no filtered gold-line hits.
- 25 trials exist on the laptop and all have completed: 15 `valid-correct`, 6 `valid-incorrect`, 4 `evaluator-failure`; three more Haiku trials ran on the VM (all reward 1.0) and are mirrored under `workspace/evidence/vm/`, outside the laptop's `scores.json`. seaborn-3190: 13 trials, 12 correct (the one failure is `hidden|withheld`, F2P passed and five PASS_TO_PASS regressions). sklearn-13584: 4 trials, 3 correct (interactive failed). pypdf-3826: 4 launch failures before the agent ran (the harness needed `gcc`/`make`; `build-essential` is now in the image), then 4 completed attempts, all `valid-incorrect` (0 of 2 FAIL_TO_PASS, 95/95 PASS_TO_PASS). pass@3 and pass^3 are defined for two seaborn groups (both 1.0); every other group has n below 3.
- The question/reply episode: 14 interactive trials reached the agent (12 on the laptop, 2 on the VM) and 3 asked. Sonnet 5 under the rewritten instruction (trial `e49349d80b190bf73f14a66f`) asked one question, received a reply drawn from the on-request ledger fact, requested two reviews, spent patience 6 → 1, and earned 1.0 with its transcript hash bound to the collection receipt. Opus 5 under the same instruction asked two questions and one review (6 → 2, reward 1.0). Haiku 4.5 asked nothing in 9 of 9 trials under either instruction wording and asked two questions in 1 of 2 trials with the mini-swe-agent scaffold config (recorded as `agent_config_sha256`). Every interactive transcript is frozen and `transcript_binding_valid` is true.
- Temporal on the VM: a `CreateTasksWorkflow` ran judge, probe, auto-approve, compile and validate unattended (03:19:28Z to 03:24:06Z) and ended `invalid` on the plausible-wrong control defect that was fixed afterwards; a `RunTasksWorkflow` ran trial `26f3304d9caedfe10cd75ebf` to reward 1.0 with exactly one `Started swesim-trial-…` journal line across a worker restart at 03:26:04Z (worker PID 5096 before, 5647 after, in the Temporal history). The laptop-off window (`workspace/evidence/laptop_off_window.txt`: last SSH 03:46:03Z, next SSH 04:10:46Z) contained a complete `RunTasksWorkflow` (`run-42874b1b…`, 03:46:06Z to 03:55:38Z) that launched, polled and ingested two Haiku trials on seaborn-3190 (interactive and hidden, attempt 40, both reward 1.0, one `Started` line each); `workspace/evidence/ops.json` marks both checks passed and notes that the laptop's lid stayed open while no SSH session or tunnel existed. Evidence is mirrored under `workspace/evidence/vm/`.
- Judge calibration (`workspace/judge_calibration.json`, `judge-v3`, `openai/gpt-5.5`, 145 of 150 rows judged, 5 too large for the authoring budget): agreement 0.655, precision 0.644, recall 0.460 against the human `underspecified >= 2` label (tp 29, fp 16, tn 66, fn 34; the always-no baseline would agree 0.566). The judge's criterion is not the human label, so this is a sanity check, not a target. On pool A it kept 24 of 45 rows.
- The gates rejected, in order: `psf--requests-1944` (network tests; repository excluded), `sphinx-doc--sphinx-9467` (published FAIL_TO_PASS passes on base offline; this created the probe stage), the first sklearn-13584 spec (test-order pollution; definition-order runner and grading-faithful probe), the first pytest-7283 spec (the drafter protected the gold source file; protected paths are now code-owned), the first pypdf-3826 spec and the VM's seaborn job (corrupt model-authored plausible-wrong diffs; controls are now verified at draft time), `sphinx-doc--sphinx-10097` (its alternative-correct control was not correct), `matplotlib--matplotlib-25547` (verifier image build guard on the base image; every control an infrastructure outcome, never a pass), and `matplotlib-24189` and `pytest-7283` a second time (unreliable exact-id probe). Repository discovery on pypdf: the feature band found no valid candidate; the bugfix band probed three PRs and the judge kept one.
- Deployed and passing: the stakeholder service on Modal (`/health`), the VM with its three units, the operator console, a tracked `examples/mwaskom--seaborn-3190` package with two sanitized runs, 490 offline tests, `verify.sh 7` (PLAN.md M1 to M7 checked).

Stakeholder v2 rebuild (2026-09-13, after the goal run): the stakeholder was found to describe the hidden regression test in a review reply, so it was rewritten to speak as the reporter, filter replies against hidden-test identifiers, and confirm described behavior instead of reviewing diffs (`confirm-behavior`). seaborn-3190 and sklearn-13584 were rebuilt as version 2 through `swesim create --rebuild` and run again through Temporal: 12 trials, all reward 1.0; Sonnet 5 asked with intent and confirmed behavior in 4 of 4 interactive trials, Haiku 4.5 asked in 2 of 4; no reply was regenerated or redacted. The v2 run exposed two bugs only a real Temporal run could show (planned trial ids without the scaffold digest; the GitHub source defaulting to its feature band), both fixed. Details in `docs/grading.md` ("Stakeholder versions") and `docs/ops.md`.

Not measured or still open: PLAN.md M1 to M10 are checked and `bash scripts/verify.sh 10` passes (recorded in the ledger at commit `722e422`). The `hidden` arm has one or two trials per profile, so the cost of withholding and the effect of interaction are unestimated; the three asking trials are on one scenario (seaborn-3190) under two wordings and one scaffold, so the interaction rate is not yet a per-model measurement. The judge calibration is one 150-instance sample. Trials recorded before the compile-time binding of instructions and the scaffold (all trials to date) carry the compiled hash of the build they ran on in `run_config.json`; later builds hash differently, which is why score tables group by `run_config_hash`.
