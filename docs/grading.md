# swesim grading

The reward contract as implemented in `swesim/verifier/` (grader, path policy, evidence parsers), the control sweep in `swesim/stages/validate.py`, and the gates that decide whether a scenario exists. Everything here is code-owned; no model output touches a reward.

## The reward contract

The separate, offline verifier writes `reward.json` with finite numbers only (Harbor rejects anything else); everything descriptive goes to `report.json`.

| Key | Type | Meaning |
|---|---|---|
| `reward` | float in [0, 1] | partial credit by requirement group; 0 on any regression |
| `success` | 0 or 1 | whole-contract correctness: every FAIL_TO_PASS and PASS_TO_PASS id observed `passed` |
| `f2p_passed`, `f2p_expected` | int | FAIL_TO_PASS ids passed and expected |
| `p2p_passed`, `p2p_expected` | int | PASS_TO_PASS ids passed and expected |
| `groups_passed`, `groups_total` | int | requirement groups fully passed and total |
| `integrity_signal_count` | int | entries in `report.integrity_signals` |
| `outcome_code` | int 1 to 7 | outcome class (below) |
| `patch_applied` | 0 or 1 | whether any allowed change reached the graded tree |

`success` drives outcomes and pass rates; `reward` is exported for training and for explaining near misses and never decides an outcome. If the grader never writes, the verifier entrypoint's exit trap writes `{"reward": 0, "success": 0, "outcome_code": 7}`.

## Requirement groups and the regression rule

A fraction of tests passed is a poor reward (a no-op passes every PASS_TO_PASS test; one requirement may own a hundred parameterized tests, another two), so credit is awarded per requirement group. `groups.json` maps every FAIL_TO_PASS id to exactly one group with a positive weight; the schema requires exact coverage and weights summing to 1. A group passes only if every test in it passes.

```
reward  = 0                                 if any PASS_TO_PASS id is not passed
        = min(1, sum(weight_g * passed_g))  otherwise
success = 1  iff every FAIL_TO_PASS and PASS_TO_PASS id is passed
```

Skipped, xfailed, xpassed, errored and missing all count as not passed. Integrity signals never change either number; the control sweep decides whether a task whose exploit control earns credit is accepted at all.

## Outcome codes

Outcomes 1 to 4 are agent-attributable and enter pass-rate denominators; 5 to 7 are infrastructure, excluded from denominators and reported.

| Code | Outcome | Assigned when |
|---|---|---|
| 1 | `valid-correct` | evidence reconciled, `success == 1` |
| 2 | `valid-incorrect` | evidence reconciled, patch applied, `success == 0` |
| 3 | `agent-invalid-submission` | receipt `oversized` or `invalid-tree`; corrupt, oversized or self-colliding archive; nothing left after the path policy (tests still run for base evidence); solver timeout |
| 4 | `agent-runtime-failure` | evidence cannot be reconciled; invocation timeout; output past the 64 MiB cap (run killed); other agent-side Harbor exceptions |
| 5 | `collection-failure` | receipt missing or unparsable, `status != ok`, `rc != 0`, `user != root`, archive missing or digest mismatch |
| 6 | `simulator-failure` | reserved for a stakeholder 5xx or interrupted request on a within-budget call; the run stage records service evidence and marks the episode ineligible; assigned by adjudication, never automatically |
| 7 | `evaluator-failure` | malformed grader config, `/pristine` or target missing, disk errors, hidden test patch fails to apply, test user missing, unexpected exception, Harbor launch failure |

## pass@k and pass^k

Attempts are grouped by `condition|brief_profile|run_config_hash`; the hash binds model, purpose, compiled build and brief profile, so calibration and comparison never share a group, and a group with more than one model or purpose raises rather than pools. With `n` attempts of outcome 1 to 4 and `c` of them with `success == 1`:

```
pass@k = 1 - C(n - c, k) / C(n, k)     null when n == 0 or k > n
pass^k = C(c, k) / C(n, k)             null when n < k
```

`n_infra_failures` and `n_agent_failures` (outcomes 3 and 4) are reported beside the counts; attempts without a record are listed as unresolved and never enter `n`. `k` is never silently reduced. `mean_reward` is the mean of `reward` over the same `n`.

## Evidence reconciliation

The grader runs the contract once with the task's own interpreter through a trusted launcher (`run_pytest.py`) started with `-I`, so the repository root is not a site directory and no `PYTHON*` variable is inherited; pytest is imported before the submitted roots are added, and the launcher records the exact collected node ids. The process runs as `tester` with a 600 s budget. Four signals must agree: the collected ids equal the contract ids exactly; the JUnit XML holds exactly those ids, each once; the `-rA` summary lines, read only from the last `short test summary info` section so captured output cannot collide, agree per id and the counts line agrees per category; and the exit code is 1 if and only if any test failed or errored.

Any disagreement is an `EvidenceError`: outcome 4, every test `observed: unknown`, reward 0. The grader then re-runs each FAIL_TO_PASS id in a fresh process (at most 10, within the remaining budget) and records the results under `report.reruns` as diagnostics; reruns never grant credit. xfail and xpass are never credited.

## Protected paths

The change set is computed by content hash against `/pristine` and applied onto the runtime tree (the task's `/testbed` or `/app` in place, so editable installs import the submitted code) with protected paths dropped and recorded as `protected-path-edit` signals. Globs match NFC-normalized, case-folded paths at any depth. Extraction refuses symlinks, hard links, devices and escaping paths; writes through a symlinked parent in the base tree are refused.

| Family | Paths | Why |
|---|---|---|
| contract tests | the contract's test files and every path the hidden test patch touches | the contract itself; also stops pre-editing a fixture so the overlay fails and is coded 7 |
| conftest | `**/conftest.py` | loaded at any depth; a hook can replace every test body with a no-op |
| startup hooks | `**/sitecustomize.py`, `**/usercustomize.py`, `**/*.pth` | executed before pytest; `-I` plus protection keeps them out |
| pytest configuration | `pytest.ini`, `.pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml`, `pytest.toml`, `.pytest.toml`, root and any depth | pytest reads the first config found walking up; `addopts`, `python_files` and plugin lines change what runs |
| CI and git metadata | `.github/**`, `.gitattributes`, `.gitignore` | never part of a fix's behavior in the verifier; an edit is a signal |
| lockfiles | `**/uv.lock`, `**/poetry.lock`, `**/Pipfile.lock`, `**/requirements*.txt`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml` | the verifier installs nothing; a pin change cannot be honored and must not be silently ignored |
| package metadata | `**/*.egg-info/**`, `**/*.dist-info/**` | an in-tree `entry_points.txt` can register a `pytest11` plugin loaded before any conftest |

## Collector receipt and transcript binding

The `[[verifier.collect]]` hook runs as root with a cleared environment and absolute executables: it kills every process of the `agent` user (verified via `/proc`), bounds the tree (symlinked root is `invalid-tree`; over 500 MiB or 100,000 members is `oversized`), archives without following symlinks, hashes the archive, and in a `finally` block calls `POST /freeze` before writing the receipt: `status, rc, user, sha256, bytes, files, freeze_status, transcript_hash`. Both files live in root-only `/var/swesim-collect` as file artifacts; `/logs/artifacts` is never read. The run stage sets `transcript_binding_valid` only when the fetched transcript's hash matches the receipt and the session is frozen; an unbound transcript never changes correctness scores but makes the episode ineligible for training.

## The control sweep

`validate` launches one Harbor job per control on Modal plus a reachability probe. A scenario is `valid` only if every row holds.

| Control | Applied by | Declared expectation |
|---|---|---|
| `nop` | Harbor's no-op agent | `reward == 0`; every FAIL_TO_PASS id `failed` or `error`, every PASS_TO_PASS id `passed`, no other ids (`expected_base`) |
| `oracle` | Harbor's oracle agent, gold diff | `success == 1`, `reward == 1`, every id `passed` (`expected_gold`) |
| plausible-wrong (at least one) | patch agent | `success == expected_success` (0); `reward <= expected_reward_max` (0.5 by default) |
| alternative-correct (required when the judge flags structural tests) | patch agent | `success == 1` and `reward == 1` |
| tamper, `incorrect-exploit` (at least one): root conftest no-op, skip marker on base test files, sitecustomize startup probe | patch agent | `reward == 0` exactly |
| tamper, `benign-edit` (optional) | patch agent | `success == 1`; `expected_signals` recorded, not yet asserted |
| reachability probe | patch agent | gold commit object unreachable; no gold-line hits in the image |

Any declared `expected_reward` is checked exactly. Every control must finish with outcome 1 to 4: an infrastructure outcome or a Harbor agent exception is "not a passing control", never a defeated attack. Any incorrect-exploit control that earns `reward > 0` invalidates the scenario.

## Worked examples

| Submission | Outcome | reward, success |
|---|---|---|
| fixes every FAIL_TO_PASS, no regression | 1 | 1.0, 1 |
| fixes only group `g01` (weight 0.4), no regression | 2 | 0.4, 0 |
| real source fix plus an edited `conftest.py`: the conftest change is dropped with a `protected-path-edit` signal | 1 or 2 | graded on the source fix |
| only `conftest.py` edited (the root-conftest exploit): nothing applied, tests still run | 3 | 0.0, 0 |
| fixes FAIL_TO_PASS but breaks one PASS_TO_PASS test | 2 | 0.0, 0 |
| floods output past 64 MiB (killed, `output-cap-exceeded`) or forges summary lines (evidence disagreement) | 4 | 0.0, 0 |
| receipt missing or archive digest differs; retried as a new attempt | 5 | 0.0, 0 |
| stakeholder 5xx on a within-budget request; not training-eligible | 6 by adjudication | correctness scores kept |
| malformed grader config, or hidden test patch fails to apply | 7 | 0.0, 0 |

## What the judge and the approval gates check

- Judge: code extracts the issue-to-contract delta (exception names and literals asserted by the tests, public names introduced by the fix, all absent from the issue); a model classifies each gap; the code-owned rule requires at least one gap explicitly non-derivable from the repository, consequential and resolved by a stakeholder statement in the thread, plus a self-contained task. The judge model must differ from the draft model.
- Draft: every non-authored fact quotes a verbatim span (at least 12 characters and 3 words after normalization) of the text its provenance names, with a URL under the repository; at least one on-request fact links a FAIL_TO_PASS test; every withheld span is verbatim in the natural issue and absent from the withheld brief, title retained. One correction round, then failure.
- `swesim approve --auto`: schema (the strict `ScenarioSpec` validator), assets (declared bytes and sha256, inside the scenario directory), judge acceptance, on-request fact linked to FAIL_TO_PASS, withholding leak, controls present. It freezes `scenario.json` and records `approved_by: auto-validators, review_needed: true`. Compile refuses an unpinned image digest, unknown provider, non-HTTPS stakeholder URL or unpinned agent version.

## Known residuals

- In-process forging. Submitted code runs inside the pytest process as `tester` and could forge the collected-id manifest, JUnit, terminal summary and exit code together, including from an `atexit` handler after the real summary. The defenses above are tested, not a proof; the sitecustomize control is a startup-execution probe and does not claim to close this. This is the acknowledged CLAUDE.md residual.
- Base copies versus the reachability probe. The probe on `main` greps for the first three added lines longer than 20 characters; an added line that already exists at base (an import, a stale `build/lib` copy) counts as a hit and invalidates a legitimate scenario. A heuristic pairing added lines with their hunk's removed lines exists on a stream branch, not yet merged.
- A test that passes and then errors in teardown yields duplicate terminal evidence and outcome 4. A base tree containing a hard link could be written through in in-place mode; the precondition is implausible and the hardening is not applied.

## Measured results

Everything below is read from artifacts on disk on 2026-09-13 (UTC): `registry.json`, `workspace/scenarios/*/validation.json` and `scores.json`, `workspace/trials/*/{run_config,reward,report,transcript,record}.json`, `workspace/judge_calibration.json`, `workspace/candidates/*.rejects.json` and `.probed.jsonl`, `workspace/evidence/*.log` and `workspace/evidence/vm/`. Every validation ran on Modal (`environment_provider: modal`) under the validation run configuration `0b03142e…`. Nothing here is projected.

### Valid scenarios: executed controls

Three scenarios are `valid` and registered. In every table `nop` earns outcome 3 because Harbor's no-op agent leaves nothing for the path policy to apply (tests still run for base evidence), the oracle earns outcome 1, the plausible-wrong patch applies and fails (outcome 2), and each incorrect-exploit tamper is dropped by the path policy with one `protected-path-edit` signal (outcome 3). The reachability probe found the gold commit object unreachable in all three images.

**mwaskom--seaborn-3190** (SWE-bench pool A; contract 1 FAIL_TO_PASS `tests/_core/test_scales.py::TestContinuous::test_interval_with_bools`, 84 PASS_TO_PASS; one group, weight 1.0). This `validation.json` is the re-validation of the build compiled after the interactive-instruction change (`compiled_hash 750e014c…`); the first build (`befc959d…`) was also valid and is the one `registry.json` still records, because `register` was not re-run after the recompile.

| Control | reward | success | F2P | P2P | signals | outcome | Expectation |
|---|---|---|---|---|---|---|---|
| `nop` | 0.0 | 0 | 0/1 | 84/84 | 0 | 3 `agent-invalid-submission` | reward 0; base fails every F2P, passes every P2P |
| `oracle` | 1.0 | 1 | 1/1 | 84/84 | 0 | 1 `valid-correct` | success 1, reward 1 |
| `plausible-wrong:xor-based-boolean-handling` | 0.0 | 0 | 0/1 | 69/84 | 0 | 2 `valid-incorrect` | success 0, reward at most 0.5 |
| `tamper:tamper-root-conftest` | 0.0 | 0 | 0/1 | 84/84 | 1 | 3 | reward exactly 0 |
| `tamper:tamper-skip-marker` | 0.0 | 0 | 0/1 | 84/84 | 1 | 3 | reward exactly 0 |
| `tamper:tamper-sitecustomize` | 0.0 | 0 | 0/1 | 84/84 | 1 | 3 | reward exactly 0 |
| reachability probe | gold object unreachable; raw hits 0, filtered hits 0 | | | | | | no gold lines in the image |

**scikit-learn--scikit-learn-13584** (SWE-bench pool A; 6 FAIL_TO_PASS in `sklearn/utils/tests/test_pprint.py`, 3 PASS_TO_PASS; one group). This is the second spec for the instance; the first is in the rejections below.

| Control | reward | success | F2P | P2P | signals | outcome | Expectation |
|---|---|---|---|---|---|---|---|
| `nop` | 0.0 | 0 | 0/6 | 3/3 | 0 | 3 | reward 0; base evidence as declared |
| `oracle` | 1.0 | 1 | 6/6 | 3/3 | 0 | 1 | success 1, reward 1 |
| `plausible-wrong:wrap-array-equality-with-any` | 0.0 | 0 | 0/6 | 1/3 | 0 | 2 | success 0, reward at most 0.5 |
| `tamper:tamper-root-conftest` | 0.0 | 0 | 0/6 | 3/3 | 1 | 3 | reward exactly 0 |
| `tamper:tamper-skip-marker` | 0.0 | 0 | 0/6 | 3/3 | 1 | 3 | reward exactly 0 |
| `tamper:tamper-sitecustomize` | 0.0 | 0 | 0/6 | 3/3 | 1 | 3 | reward exactly 0 |
| reachability probe | gold object unreachable; raw hits 0, filtered hits 0 | | | | | | no gold lines |

**py-pdf--pypdf-3826** (repository URL, discovered without a PR override; 2 FAIL_TO_PASS `tests/test_reader.py::test_startxref_corrupt_trailing_pointer` and `::test_find_previous_startxref_pos_recovery_variants`, 95 PASS_TO_PASS; two groups, weights 0.6 and 0.4). The plausible-wrong control is the code-built fallback: the model's own wrong patch did not apply to the base commit (`corrupt patch at line 34`, recorded in `draft.meta.json` under `controls_dropped`), so the drafter substituted `fallback-comment-only`, a comment-only change that is guaranteed to apply and to fail the F2P tests.

| Control | reward | success | F2P | P2P | signals | outcome | Expectation |
|---|---|---|---|---|---|---|---|
| `nop` | 0.0 | 0 | 0/2 | 95/95 | 0 | 3 | reward 0; base evidence as declared |
| `oracle` | 1.0 | 1 | 2/2 | 95/95 | 0 | 1 | success 1, reward 1 |
| `plausible-wrong:fallback-comment-only` | 0.0 | 0 | 0/2 | 95/95 | 0 | 2 | success 0, reward at most 0.5 |
| `tamper:tamper-root-conftest` | 0.0 | 0 | 0/2 | 95/95 | 1 | 3 | reward exactly 0 |
| `tamper:tamper-skip-marker` | 0.0 | 0 | 0/2 | 95/95 | 1 | 3 | reward exactly 0 |
| `tamper:tamper-sitecustomize` | 0.0 | 0 | 0/2 | 95/95 | 1 | 3 | reward exactly 0 |
| reachability probe | gold object unreachable; 1 raw hit in `pypdf/_reader.py` filtered as a base copy, filtered hits 0 | | | | | | no gold lines |

The pypdf probe row is the first time the added-line-paired-with-removed-line filter (merged as `3d59493` and `1db2076`) changed a verdict: under the earlier probe this raw hit would have invalidated a legitimate scenario, as it did for requests-1944.

### Trials

25 trial directories exist and all 25 have completed: 15 `valid-correct`, 6 `valid-incorrect`, 4 `evaluator-failure`. Every trial used mini-swe-agent 2.4.6 on Modal with a 2,400 s solver budget and purpose `demonstration`; interactive trials used the stakeholder on `openai/gpt-5.5` with the default patience profile (start 6, question 1, review 2, terse below 3, refuse at 0). Groups are `condition|brief_profile|run_config_hash`, so the two seaborn builds and the three solver models never pool. `pass@k` and `pass^k` (k = 3) are only defined for the two groups with n ≥ 3; both are 1.0. `k` is never reduced.

| Scenario | Condition | Profile | Solver | Build / instruction | n | success | mean reward | questions asked |
|---|---|---|---|---|---|---|---|---|
| seaborn-3190 | full | natural | Haiku 4.5 | v1 | 1 | 1 | 1.0 | 0 |
| seaborn-3190 | hidden | natural | Haiku 4.5 | v1 | 1 | 1 | 1.0 | 0 |
| seaborn-3190 | hidden | withheld | Haiku 4.5 | v1 | 1 | 0 | 0.0 | 0 |
| seaborn-3190 | interactive | withheld | Haiku 4.5 | v1 | 3 | 3 | 1.0 (pass@3 1.0, pass^3 1.0) | 0, 0, 0 |
| seaborn-3190 | interactive | withheld | Sonnet 5 | v1 | 1 | 1 | 1.0 | 0 |
| seaborn-3190 | interactive | withheld | Haiku 4.5 | v2; attempts 50 and 51 also carried the scaffold config | 4 | 4 | 1.0 (pass@3 1.0, pass^3 1.0) | 0, 0, 2, 0 |
| seaborn-3190 | interactive | withheld | Sonnet 5 | v2 | 1 | 1 | 1.0 | 1 |
| seaborn-3190 | interactive | withheld | Opus 5 | v2 | 1 | 1 | 1.0 | 2 |
| sklearn-13584 | full | natural | Haiku 4.5 | v1 | 1 | 1 | 1.0 | 0 |
| sklearn-13584 | hidden | natural | Haiku 4.5 | v1 | 1 | 1 | 1.0 | 0 |
| sklearn-13584 | hidden | withheld | Haiku 4.5 | v1 | 1 | 1 | 1.0 | 0 |
| sklearn-13584 | interactive | withheld | Haiku 4.5 | v1 | 1 | 0 | 0.0 | 0 |
| pypdf-3826 | full | natural | Haiku 4.5 | attempt 0 | 0 (1 evaluator-failure) | 0 | null | none |
| pypdf-3826 | hidden | natural | Haiku 4.5 | attempt 0 | 0 (1 evaluator-failure) | 0 | null | none |
| pypdf-3826 | hidden | withheld | Haiku 4.5 | attempt 0 | 0 (1 evaluator-failure) | 0 | null | none |
| pypdf-3826 | interactive | withheld | Haiku 4.5 | attempt 0 | 0 (1 evaluator-failure) | 0 | null | none |
| pypdf-3826 | full | natural | Haiku 4.5 | attempt 1, build-essential image | 1 | 0 | 0.0 | 0 |
| pypdf-3826 | hidden | natural | Haiku 4.5 | attempt 1 | 1 | 0 | 0.0 | 0 |
| pypdf-3826 | hidden | withheld | Haiku 4.5 | attempt 1 | 1 | 0 | 0.0 | 0 |
| pypdf-3826 | interactive | withheld | Haiku 4.5 | attempt 1 | 1 | 0 | 0.0 | 0 |

Sources and caveats for the table: seaborn and sklearn rows come from `scores.json`; all rows come from `scores.json` as regenerated after the last trial landed (the Opus trial `e54052254969566b15d6cb1b` appears as its own group, n=1). The four pypdf evaluator-failures are Harbor launch failures before the agent started: mini-swe-agent's setup needed `gcc`/`make` and tried to `apt-get` inside the allowlisted sandbox; `build-essential` is now pre-installed in the environment image (`5723414`) and attempt 1 ran the agent. Near misses are visible in `report.json`: the seaborn `hidden|withheld` Haiku submission passed the F2P test and failed five `TestTemporal` PASS_TO_PASS tests (reward 0 by the regression rule, outcome 2); the sklearn interactive Haiku submission passed 0 of 6 F2P with 3/3 P2P; each pypdf attempt-1 submission passed 0 of 2 F2P with 95/95 P2P. Solver models: `anthropic/claude-haiku-4-5-20251001`, `anthropic/claude-sonnet-5`, `anthropic/claude-opus-5`.

Three further trials ran on the VM under Temporal and are mirrored under `workspace/evidence/vm/workspace/trials/` (they are not in the laptop's `scores.json`): seaborn-3190 `interactive|withheld` Haiku attempts 10 and 40 (reward 1.0, no questions, transcripts frozen and bound) and `hidden|withheld` Haiku attempt 40 (reward 1.0), all on the v1 build. With the laptop's `hidden|withheld` attempt 0 (0.0) that arm has n = 2 on seaborn.

Every interactive transcript on disk is frozen and hash-bound: `report.transcript_hash` equals the collector receipt's `transcript_hash` and `transcript_binding_valid` is true for every completed interactive trial. Exports mark all episodes `simulator_validity: unadjudicated` and `training_eligible: false`, as designed.

### The interaction finding

15 interactive trial records exist (13 on the laptop, 2 on the VM); 14 reached the agent (the pypdf attempt-0 trial failed at launch). Questions were asked in 3 of those 14:

| Solver | Instruction | Scaffold config | Interactive trials | Trials with at least one question | Questions, reviews, patience |
|---|---|---|---|---|---|
| Haiku 4.5 | v1 | no | 7 (seaborn 0, 1, 2 and VM attempts 10, 40; sklearn 0; pypdf 1) | 0 | 6 → 6 in every trial |
| Haiku 4.5 | v2 | no | 2 (seaborn 20, 21) | 0 | 6 → 6 |
| Haiku 4.5 | v2 | yes | 2 (seaborn 50, 51) | 1 (attempt 50) | attempt 50: 2 questions, 1 review, 6 → 2; attempt 51: none |
| Sonnet 5 | v1 | no | 1 (seaborn 0) | 0 | 6 → 6 |
| Sonnet 5 | v2 | no | 1 (seaborn 30) | 1 | 1 question, 2 reviews, 6 → 1 |
| Opus 5 | v2 | no | 1 (seaborn 30) | 1 | 2 questions, 1 review, 6 → 2 |

So Haiku 4.5 asked nothing in 9 of 9 trials under either instruction alone, and asked in 1 of 2 trials once the agent scaffold itself listed asking as a step; Sonnet 5 asked under v2 and not under v1 (one trial each); Opus 5 asked under v2 (one trial). Every one of the 12 seaborn interactive trials (10 on the laptop, 2 on the VM) earned reward 1.0 whether or not it asked, while the seaborn `hidden|withheld` arm holds one trial at 0.0 and one at 1.0; nothing about a treatment effect can be concluded from that. All 3 asking trials were on seaborn-3190; the sklearn and pypdf interactive trials (Haiku, v1) asked nothing and failed.

What each change was:

- **v1 instruction** (`instruction.md.j2` as first compiled, build `befc959d…`): "A stakeholder who filed this request is available. They can answer a limited number of questions before they have to leave; investigate what you can from the repository first." plus the two shim commands.
- **v2 instruction** (`af9fab9`, build `750e014c…`, re-validated valid): "The brief above is intentionally incomplete: the person who filed this request left out decisions that the acceptance tests depend on (expected behavior on edge cases, exact error types or messages, scope, naming). Guessing them is how solutions fail. The stakeholder is available now and will answer a limited number of questions before they have to leave, so ask about any decision the brief does not settle before you implement it, and ask early. Repository facts (where code lives, how it works) you should find yourself."
- **Scaffold config** (`be55d7f`, `swesim/templates/mini_swe_agent.interactive.yaml`, layered by Harbor as `-c mini -c <file>` for interactive trials only): the packaged mini-swe-agent `instance_template` with one added step in its Recommended Workflow ("List every decision the task description leaves open … Ask the stakeholder about each such decision with `ask-user` BEFORE you implement it") and an optional `request-review` in the edge-case step. It is recorded per trial as `report.agent_config_sha256` (`02791a33…` on attempts 50 and 51, null elsewhere) so the two Haiku v2 groups can be told apart even though they share a `run_config_hash`. It exists because the packaged agent workflow never lists asking as a step, so the instruction alone competes with the harness's own procedure.

**The Sonnet 5 episode** (`workspace/trials/e49349d80b190bf73f14a66f`, seaborn-3190, interactive, withheld brief, attempt 30, v2 instruction, no scaffold). Session created 03:45:24Z; question at 03:46:16Z (patience 6 → 5, tier `full`); review at 03:48:33Z (5 → 3, `full`); review at 03:49:19Z (3 → 1, `terse`, since 1 is below `terse_below 3`); frozen 03:50:15Z; graded `reward 1.0, success 1`, F2P 1/1, P2P 84/84. The agent's edit to `tests/_core/test_scales.py` was dropped with a `protected-path-edit` signal and `seaborn/_core/scales.py` was applied.

The question (excerpt of the transcript event, 03:46:16Z):

> I found the bug: when Color.default_scale infers a scale for boolean data (no explicit scale spec given), it calls variable_type(data, boolean_type='numeric') which returns 'numeric' for booleans, so it picks a Continuous scale, and Continuous._setup crashes because numpy booleans don't support subtraction. […] Should the fix be: (a) narrowly make Color.default_scale treat booleans as categorical/Nominal (matching Color.infer_scale's existing behavior), or (b) more broadly fix the base Property.default_scale to treat booleans as categorical for all properties, or (c) fix it at a lower level in ContinuousBase._setup so that boolean data is cast to float/numeric before computing vmin/vmax normalization […]? Do you have a preference […]

The stakeholder's reply (verbatim):

> Prefer (c). Boolean values that reach the Continuous scale path should be treated numerically, i.e. upcast so False/True map as 0/1 and subtraction does not fail.
>
> Do not make booleans default to Nominal here, either just for Color or broadly through Property.default_scale. That would be a larger semantic change; a distinct Boolean scale may make sense later, but this fix should stay narrow and preserve the existing numeric-default path.

That answer is the on-request ledger fact `f03` ("boolean data passed through a continuous scale should be upcast to 0/1 floats", provenance `pr-thread`, linked to the F2P test). The first review round told the agent, without executing anything, that its regression test should exercise `Continuous()._setup(..., IntervalProperty())` directly; the agent's second diff added a test named `test_interval_with_bools`, the same name as the hidden F2P test, which the path policy then dropped so grading used only the hidden test. Transcript hash `417981a4…` appears identically in `report.transcript_hash` and the receipt; `transcript_binding_valid` is true.

**The other two asking trials.** Haiku 4.5 with the scaffold (`c39a73b6cfdffdc66581af1a`, attempt 50): "When boolean data is used with a continuous color property, what should be the expected behavior? Should we: (A) convert boolean to numeric (True->1, False->0) for normalization, (B) treat it as categorical, or (C) raise an informative error?" (answered (A), 6 → 5), then a question about the custom-norm branch (→ 4), then a review the stakeholder called "not acceptable as-is" because scratch files were in the diff (→ 2); the final submission applied only `seaborn/_core/scales.py` and earned 1.0. Opus 5 (`e54052254969566b15d6cb1b`, attempt 30): two multi-part questions on scope and on object-dtype and nullable booleans (6 → 4), one review (→ 2), reward 1.0 with `seaborn/_core/scales.py` and a new test in `tests/_core/test_plot.py` applied (only the contract file is protected). In that review the stakeholder warned that `vmin, vmax = map(float, axis.convert_units((vmin, vmax)))` "is broader than the bug requires"; that line is the gold patch's own change (it is the judge's `g3` evidence). The diff-only reviewer is a model with the requirement texts, not the oracle, and its opinion never reaches the reward.

### Stakeholder versions

Every interactive trial recorded before commit `99b4584` (2026-09-13) ran **stakeholder v1**: a maintainer-voiced prompt, diff review available through `request-review`, no reply filter. On the seaborn scenario v1 answered a review request by describing the regression test's inputs and expected output (request `cf8b5b39…`), which is exactly the information the hidden tests withhold; the question/reply episodes counted in the interaction finding above therefore include replies a reporter could not have given. **Stakeholder v2** (`99b4584`) speaks as the reporter, never names tests, verification steps, or identifiers outside the brief, does not pick among the agent's options, regenerates and redacts replies that contain hidden-test identifiers (`forbidden_terms`, recorded worker-only as `redacted_terms`), and replaces diff review with `confirm-behavior`: the agent describes the resulting behavior in plain language and the stakeholder confirms it or names the requirement that still sounds unmet. Diff review remains available only under `review_mode: diff-only`. Scores and exports carry `confirmations` next to `questions_asked` and `reviews`. The v1 trials stay in the record labelled as such and are not pooled with v2 runs (the compiled build and the stakeholder configuration are part of `RunConfig`, so the trial groups differ).

**Measured under stakeholder v2 (2026-09-13 06:10–06:40 UTC, all through `RunTasksWorkflow` on the VM).** The registered scenarios were rebuilt as version 2 with `swesim create --rebuild` (new draft, compile and validation on the v2 templates: `confirm-behavior` shim, reporter-stance stakeholder, test-vocabulary fact filter). seaborn-3190 v2 (spec `812be6e0…`, compiled `e208a80f…`) lost its withheld brief because the withholding model selected no spans on this draft (the nudge in `383a8fc` did not exist yet); its on-request facts f02 and f04 are the maintainer's 0/1-upcast decision stated as behavior, and the v1 fact that described the regression test is now author-only. sklearn-13584 v2 (spec `4e3ecb62…`, compiled `060e1c1b…`) kept its withheld brief.

| scenario (v2) | condition · brief | model | trials | reward 1.0 | asked | confirmed behavior |
|---|---|---|---|---|---|---|
| seaborn-3190 | full · natural | Haiku 4.5 | 1 | 1 | – | – |
| seaborn-3190 | hidden · natural | Haiku 4.5 | 1 | 1 | – | – |
| seaborn-3190 | interactive · natural | Haiku 4.5 | 2 | 2 | 2 of 2 (1 and 2 questions) | 1 of 2 (the other confirmed through a second `ask-user`) |
| seaborn-3190 | interactive · natural | Sonnet 5 | 2 | 2 | 2 of 2 | 2 of 2 |
| sklearn-13584 | full · natural | Haiku 4.5 | 1 | 1 | – | – |
| sklearn-13584 | hidden · withheld | Haiku 4.5 | 1 | 1 | – | – |
| sklearn-13584 | interactive · withheld | Haiku 4.5 | 2 | 2 | 0 of 2 | 0 of 2 |
| sklearn-13584 | interactive · withheld | Sonnet 5 | 2 | 2 | 2 of 2 | 2 of 2 |

Every one of the twelve v2 trials earned 1.0, so these two scenarios do not separate `hidden` from `interactive` for Haiku 4.5 or Sonnet 5; the interaction mechanism is what they show. Sonnet 5 asked with intent in all four of its trials ("I plan to … Is that what you want?") and then described the resulting behavior with `confirm-behavior`; the stakeholder answered in behavior ("False → 0.0 and True → 1.0", "I don't care where in the code that happens") and confirmed. Haiku 4.5 asked in both seaborn trials and in neither sklearn trial. Disclosure audit: all twelve stakeholder replies in the eight interactive trials carry `regenerated: false` and an empty `redacted_terms`; read in full, none names a test, a verification step, a function, class or file, and none picks among the agent's options in code terms (the sklearn replies restate the thread's decision, comparison by textual representation, which is a stakeholder-visible fact from the PR thread). Trial ids: seaborn `eb136435`, `f9435904`, `dc88e732`, `793e78e0`, `c1846a15`, `7bf266da`; sklearn `f3e43d74`, `eb325582`, `350216f8`, `444c038d`, `3ff99666`, `689a9433`. The registry also lists four `unresolved` attempts per scenario from the first interactive submissions: the Temporal planner hashed `agent_config: ""` while `run_trial` added the scaffold digest, so the planned trial ids never matched the records the units wrote (fixed in `ec16791`; the resubmitted workflows resolved against the existing records without launching anything). pypdf-3826 v2 is recorded below when its rebuild completes.

### Judge calibration

`workspace/judge_calibration.json`: prompt `judge-v3`, model `openai/gpt-5.5`, 150 annotated instances requested, 145 judged, 5 errored (all `pydata__xarray` and `astropy__astropy` rows whose F2P ids, issue head and test-patch head alone exceed the 120,000-character authoring budget). Against the human `underspecified >= 2` label: agreement 0.655, precision 0.644, recall 0.460; confusion tp 29, fp 16, tn 66, fn 34.

What the numbers mean: 63 of the 145 rows carry the human label, so always answering "no" would agree 0.566 of the time; the judge reaches 0.655. When it keeps a row, humans also flagged it 64% of the time; it keeps 46% of the rows humans flagged. The limits: the judge's criterion (at least one gap that is non-derivable from the repository, consequential, and resolved in the thread or by the tests, plus a self-contained task) is not the human criterion ("the issue is underspecified"), so agreement is a sanity check, not a target; one model and one prompt version were measured; the 5 unmeasurable rows are the largest instances, which biases the sample toward smaller issues; and no inter-judge or repeat-run variance was measured. Yield on pool A (45 pytest-eligible rows): judge-v1 kept 1 of an 8-row shortlist (requests-1944), judge-v2 kept 0 of 42 (23 rows produced no extracted gap and 13 had only derivable gaps, because v2 classified literal deltas only), judge-v3 kept 24 of 45 with 1 error. On the pypdf bugfix band it kept 1 of 2 probed candidates.

### What the gates rejected, and why

This is the validity record. Each row is a candidate that a model or a human might have shipped and that a code-owned gate stopped, with the pipeline change it caused.

| Candidate | Where it stopped | What the gate observed | What changed |
|---|---|---|---|
| `psf--requests-1944` | validate | oracle 0/5 F2P; base 73/120 P2P (47 fail on the untouched base); gold exact-id manifest failed; probe hits in `build/lib/…` and `requests/__init__.py` | the requests suite needs a network test server and the verifier is offline; psf/requests excluded from pool eligibility (`8dfa939`); probe now pairs added lines with their hunk's removed lines so base copies are not leak evidence |
| `sphinx-doc--sphinx-9467` | validate | `nop` passed all 4 published F2P ids on base (P2P 22/23); oracle 0/…; plausible-wrong agent failed before a clean submission | SWE-bench's lists were computed with network access; the offline `probe` stage (base vs gold, whole-file then exact-id) now runs before every draft and an empty measured F2P ends the candidate as `not-reproducible-offline` |
| `scikit-learn--scikit-learn-13584`, first spec | validate | oracle 1.0/1 (first gold pass offline) and all five controls 0, but `nop` showed P2P `test_basic` failing on base when the runner listed F2P ids first | test-order pollution; the runner now emits ids in definition order and the probe runs the grader's exact command (`a98f56e`); re-drafted, valid |
| `pytest-dev--pytest-7283`, first spec | validate (stopped) | oracle `agent-invalid-submission`: the drafter's `protected_paths_extra` included the gold patch's source file `src/_pytest/unittest.py` | protected paths are code-owned (contract test files only; no gold-touched path may be protected, `78b087c`); later drafts failed on "no on-request fact links the single F2P test" (now auto-linked for single-target contracts) and the final re-probe returned "unreliable exact-id probe … keep evidence and reject" |
| `matplotlib--matplotlib-24189` | draft, compile, probe | twice "structural tests require an alternative-correct control and the drafter produced none"; an unslugified control name became a path at compile; final re-probe "unreliable exact-id probe" | control names are slugified by code (`f26063e`); the candidate stays rejected |
| `matplotlib--matplotlib-25547` | draft, then validate | probe binding mismatch (stale measurement, refused); a `pr-review` excerpt that was not verbatim (refused); then all seven controls "control agent failed before a clean submission" | the verifier image build guard exited 1 on the matplotlib base image (ledger: likely symlinks in the base tree, the pilot rule); an infrastructure outcome is never a passing control, so the scenario is invalid rather than accepted by default |
| `sphinx-doc--sphinx-10097` | draft, then validate | drafts failed for a missing alternative-correct control, a non-verbatim `fact_text`, and a requirement resting on an author-only fact; the final sweep had oracle 1.0/1, nop 0, tampers 0, but `alt-correct:regex-strip-variant` ended `agent-runtime-failure` with success 0 | "alt-correct: success != 1" invalidates; the model's alternative fix was not correct and the pipeline says so |
| `py-pdf--pypdf-3826`, first spec | validate | base, oracle and tampers correct; the plausible-wrong control agent failed before a clean submission because the model-authored diff was corrupt | draft-time verification of every control patch with `git apply --check` against fetched base files, diffs built by `difflib` from complete file texts, and a code fallback control when the model's patch is unusable (`f26063e`, `2455c7e`); re-drafted, valid |
| `mwaskom--seaborn-3190`, VM create job | validate (on the VM) | same defect: `plausible-wrong:xor-based-fix` agent failed before a clean submission; nop 0, oracle 1.0, tampers 0 | the fix above; the laptop chain after it produced the valid scenario |
| earlier draft failures | draft | sphinx-9467 grounding against a corpus that kept diff `+` markers; matplotlib-24189 with no `tests` on on-request facts; sklearn-13584 with an excerpt shorter than the minimum; seaborn-3190 "no removed spans"; pypdf-3826 and sphinx-10097 on the content-word rule for `fact_text` | markers stripped from the corpus; explicit F2P list plus code-owned auto-link; zero-removal drafts are valid (natural-only briefs); withheld `fact_text` is the verbatim span |

Discovery on `py-pdf/pypdf` (`--max-prs 400`, since 2026-06-01): the feature band (200 to 1,500 code lines, feature label) ended `no-valid-candidate` with the histogram no-linked-issue 119, linked-target-is-pr 11, diff-too-small 36, no-test-changes 16, feature-label-missing 2 (from the ledger; the first 188-PR pass before the plain-mention link tier had 160 without a linked issue, 22 too small, 6 other). The bugfix band (20 to 600 lines), whose histogram is the `.rejects.json` on disk, rejected no-linked-issue 119, linked-target-is-pr 11, diff-too-small 21, no-test-changes 16, build-budget-exhausted 13, thin-thread 2, no-source-changes 1, issue-not-before-pr 1, fetch-error 1 (PR 3552: thread pagination exceeded its bound, refused rather than truncated), build-or-probe-failed 1; it built and probed three candidates on Modal: PR 3826 (F2P 2, P2P 95), PR 4032 (F2P 1, P2P 188) and PR 4033 ("unreliable base/gold probe; keep evidence and reject"). The judge kept 3826. Its code diff is below the feature floor, as the ledger ruling anticipated for a repository-URL demonstration.

Offline probe measurements for the SWE-bench candidates that reached a draft: seaborn-3190 measured 86 ids (F2P 1, P2P 84, 1 dropped as neither), sklearn-13584 9 (F2P 6, P2P 3), sphinx-10097 F2P 2 / P2P 18, matplotlib-25547 F2P 2 / P2P 78, matplotlib-24189 F2P 2 / P2P 106 with 7 environment tests dropped, pytest-7283 F2P 1 / P2P 51 with 9 network or environment tests dropped (the last two later failed the exact-id stage).

### Not yet measured

The `hidden` arm has n = 1 or 2 per profile, so the cost of withholding and the effect of interaction are not estimated. No trial has run with a solver other than the three Anthropic models, and no calibration or comparison purpose has been run. Outcome 6 has never been assigned. The stakeholder's `disclosed_fact_ids` self-report has not been audited against the ledger. No human has labeled any episode, so every export stays `unadjudicated`.
