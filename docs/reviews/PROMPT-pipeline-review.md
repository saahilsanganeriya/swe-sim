# Independent review prompts for the swesim pipeline

Two prompts, meant for two separate reviewer sessions (Codex or any reviewer), both run from the repository root `swesim/` on `main`. Part A is a code-reading review of the grader and the stakeholder service. Part B covers the rest of the pipeline, operations and documentation. Keep them separate: each is smaller, and a finding in one does not depend on the other.

Vocabulary note for the reviewer: this is a correctness and quality review of a grading system. The questions are "does the code do what the docs say" and "which conditions does the grader handle". Nothing here asks you to build or run anything against a system you do not own; the only executable targets are the project's own unit tests and its own Docker images on your machine.

---

## Part A: grader and stakeholder code review

You are reviewing **swesim**, a system that builds simulated-stakeholder software-engineering tasks for post-training. A coding agent works on a repository inside a sandbox and may ask a simulated stakeholder (an LLM grounded in the real GitHub issue thread) clarifying questions; afterwards a separate offline container runs the pull request's own tests and writes a reward. The reward is only meaningful if (1) the grader awards credit exactly when the tests pass on the submitted code, and (2) the stakeholder tells the agent only what the original issue reporter would have known.

Read the code and answer, with file:line citations, the questions below. Where a question is answered by a test, cite the test. Where the code does not handle a condition, say "not handled" and describe the smallest change. Rank findings P1 (the reward or the disclosure boundary is wrong), P2 (a measurement is wrong), P3 (quality). Do not modify the repository. Run only the offline test suite (`uv run --frozen --offline python -m pytest -q tests --ignore=tests/integration`), `bash scripts/verify.sh 10`, and, if Docker is available, the project's own verifier image against the tracked example under `examples/mwaskom--seaborn-3190/`. The suite contains one test that opens a localhost socket; if your sandbox blocks it, note that and move on.

Start from `CLAUDE.md` (contracts), `docs/grading.md`, `docs/architecture.md`, and the ledger `.superpowers/sdd/2026-09-12-swesim-implementation/progress.md`, which records every design ruling and every live failure in order.

### A1. Grader (`swesim/verifier/grader.py`, `policy.py`, `parsers.py`, `run_pytest.py`; collector `swesim/templates/collect.py`)

The grader runs in a container without network, using only the standard library. For each condition below, cite where it is handled or record "not handled":

1. Terminal output that contains pytest-style summary lines that pytest itself did not produce (the grader is supposed to require JUnit, terminal and exit status to agree, with exact test ids).
2. A JUnit file that disagrees with the terminal summary or the exit status.
3. Submitted changes to `conftest.py`, `sitecustomize.py`, `.pth` files, `pytest.ini`, `pytest.toml`, `pyproject.toml`, or `*.egg-info` entry points, and how the policy file treats each.
4. Symbolic or hard links inside the submitted tree.
5. Output above the size cap (the ledger says 64 MiB, incremental read, outcome code 4).
6. A pytest plugin registered from the repository under test.
7. An `atexit` handler in submitted code that prints after the test run.
8. Submitted modules that alter pytest internals on import.
9. Two paths that differ only by case or Unicode normalization.
10. A run that exceeds the time budget.
11. The base-status file: what happens when it is missing or malformed.

Then read `swesim/tamper.py`, the project's list of control patches that validation runs against every scenario, and list which of the eleven conditions above have a control and which do not. Read `docs/grading.md`'s statement of what is and is not defended and say whether the code matches it.

### A2. Stakeholder disclosure (`swesim/stakeholder/prompts.py`, `session.py`, `service.py`, `view.py`; `swesim/stages/draft_spec.py`; `swesim/stages/run.py`)

The stakeholder is meant to speak as the person who filed the issue: it knows the behavior it wants and observed, and nothing about the tests, the merged fix, or the code layout. The docs describe these rules; for each, find the code that enforces it and the test that covers it, or record that it rests on the prompt text alone:

1. Facts labelled author-only never enter the stakeholder's context.
2. Facts derived from the tests are rephrased as observable behavior before they become stakeholder-visible (`draft_spec.unsafe_fact_text`, `tests_resolved_facts`).
3. Replies that contain identifiers found only in the test assets are regenerated and then redacted (`forbidden_terms`, `redacted_terms`).
4. The stakeholder does not choose among implementation options the agent proposes.
5. `confirm-behavior` answers only "does this match what I wanted" and "which requirement still seems unmet".
6. Diff review is unavailable unless `review_mode` is `diff-only`.
7. `disclosed_fact_ids`, the patience tier internals and the tool audit are worker-only and do not appear in any response the agent receives.
8. A session token is issued for exactly one trial and stops working after freeze; request ids are bound to request bodies; the patience counter only decreases.

Then read every `transcript.json` under `workspace/trials/` and list every stakeholder reply that mentions a test, a verification step, a function, class or file name that is not in the brief, or that picks one of the agent's options. Note which trials predate the current stakeholder prompt (the ledger and `docs/grading.md` label them).

### Output for Part A

A markdown report: verdict paragraph; findings P1 to P3 with file:line, how you established it, consequence, smallest fix; the two checklists (A1 conditions, A2 rules) as tables with "handled at / not handled"; what you could not check and why.

---

## Part B: pipeline, operations and documentation review

You are reviewing **swesim** (see the one-paragraph description at the top of Part A). This part covers how tasks are created, how runs are orchestrated, how results are scored and shown, and whether the documentation matches what is on disk. Every finding needs file:line, how you established it, the consequence, and the smallest fix; rank P1 (a result is wrong or unattended operation stops), P2 (a measurement is wrong or operation is fragile), P3 (quality). Do not modify the repository; do not start cloud jobs (`harbor run`, `modal`, `gcloud`, `swesim create`, `swesim run-tasks`). Everything else in the repository and `workspace/` you may read and run.

Ground truth: `CLAUDE.md`, `PLAN.md`, `docs/architecture.md`, `docs/grading.md`, `docs/ops.md`, `README.md`, the ledger `.superpowers/sdd/2026-09-12-swesim-implementation/progress.md`, and the artifacts: `registry.json`; `workspace/scenarios/*/{scenario.json,judge.json,draft.meta.json,approval.json,compile.json,validation.json,scores.json}`; `workspace/trials/*/{run_config,reward,report,transcript,record}.json`; `workspace/exports/*.episode.json`; `workspace/jobs/**`; `workspace/evidence/`; `examples/mwaskom--seaborn-3190/`.

### B1. Contract measurement
SWE-bench's published FAIL_TO_PASS/PASS_TO_PASS lists were produced with network access and did not reproduce offline, so the contract is measured by a probe (`swesim/sources/github_repo.py:probe_tests`, `swesim/stages/mine.py:probe_candidates`). Check that F2P means "fails on base with the test patch applied and passes with the gold patch" under the grader's exact command and test order; that order-dependent and environment-dependent tests are dropped rather than accepted; that the probe binding (hash of `base_commit` and `test_patch`) is checked before a measurement is reused; and that `protected_paths` never contains a path the gold patch modifies.

### B2. Withholding
The withheld brief must be exactly the natural issue minus code-selected spans; every removal must appear in the ledger with a verbatim excerpt; no requirement may be added or silently dropped. Read `draft_spec.py` (`withhold`, `_deletion_range`, `ledger_violations`) and `ScenarioSpec._integrity` in `swesim/models.py`. Check the grounding rules for facts (excerpt length, blockquote normalization, casefold, provenance labels) and say whether a model output could change the task without a ledger entry.

### B3. Judge and approval gates
`swesim/stages/judge.py` decides which issues count as underspecified; `swesim/stages/approve.py` approves unattended. Check that `ground_gaps`/`accept()` require verbatim evidence, that the calibration numbers in `workspace/judge_calibration.json` (agreement 0.655, precision 0.644, recall 0.460 on 145) are computed as `docs/grading.md` describes, and whether any approval gate is satisfied trivially (for example an on-request fact linked to every F2P test).

### B4. Control patches
Plausible-wrong and alternative-correct controls are LLM-authored and verified at draft time (`draft_spec.py`: difflib-built diffs, `git apply --check` against fetched base files, a code fallback). Check that a "plausible-wrong" control cannot be functionally identical to the gold patch (a change inside a docstring or comment, a no-op removal), that verification results are recorded as they happened, and that files skipped by the fetch bound are never treated as absent.

### B5. Temporal and the VM
`swesim/workflows/{workflows,activities,client}.py`, `scripts/vm/*`, `docs/ops.md`. Claims to check: each trial launches exactly once into a named user systemd unit; a worker restart never launches a second solver; unknown launch state is reconciled, never retried; credentials never appear in a workflow payload, command line or log. Read `decide_launch`, `launch_once`, `await_trial_activity`, `ALLOW_DUPLICATE_FAILED_ONLY`, the concurrency limit in `CreateTasksWorkflow`, the signal-based approval path, and how the VM scripts obtain credentials. Compare `workspace/evidence/ops.json` and `workspace/evidence/vm/` against `docs/ops.md`, including the stated caveat on the "laptop-off" run (the lid stayed open; the claim is that no SSH session or tunnel existed during the window).

### B6. Task compilation and network policy
`swesim/stages/compile.py`, `swesim/templates/*.j2`, `references/modal-smoke-task/`. The agent runs as an unprivileged user with a host allowlist (model API hosts and the stakeholder host); the verifier has no network; collection is a root-owned hook writing to a root-only directory with a receipt. Check that the session token is placed only in the interactive task, that the test patch and the gold patch are absent from the agent image, that `compiled_hash` covers every input the agent sees (templates, scaffold config, per-profile instructions, shims), and that the base-manifest and submodule checks fail closed.

### B7. Scoring and exports
`swesim/stages/{score,export,register}.py`. Denominators count outcomes 1 to 4 only; 5 to 7 are excluded; unresolved attempts are kept but not counted; `policy_visible` is built from the agent's own observations; evaluator-only data never appears in `policy_visible`. Recompute one score table by hand from the records and compare. Check that trials with different agent scaffolds (`RunConfig.agent_config`) never pool.

### B8. Console
`swesim/dashboard/*`. Run it locally:
```
SWESIM_WORKSPACE=$PWD/workspace SWESIM_REGISTRY=$PWD/registry.json uv run --frozen uvicorn swesim.dashboard.app:app --port 8099
```
Check that agent and stakeholder text is rendered as text and never as markup, that the file route serves only the known artifact names, that evaluator-only data needs the `?evaluator=1` gate, that state-changing requests need a confirmation step and are accepted only from the console's own pages, and that no response body contains a credential.

### B9. Docs versus disk
For every number and claim in `README.md`, `docs/grading.md`, `docs/architecture.md` and `docs/ops.md`, find the artifact or mark it unsupported. Pay attention to the interaction rate, the calibration, the laptop-off window, and which trials predate which fixes (instruction wording v1/v2, the scaffold, stakeholder v1 versus v2).

### B10. Reproducibility for a stranger
Follow `README.md` as a new user with only the listed prerequisites. Every command that needs a credential, a private hostname, or a file that exists only on the author's machine is a finding.

### Output for Part B
A markdown report: verdict paragraph; findings P1 to P3 with file:line, how you established it, consequence, smallest fix; a table "claim → artifact → verified / unsupported / false" for the docs; and the three changes that would most improve the trustworthiness of the reward. Say what you could not verify and why.
