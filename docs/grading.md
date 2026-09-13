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

Only what exists on disk now.

**psf--requests-1944, control sweep on Modal, 2026-09-12.** The candidate passed the judge and all six auto-approval gates. Validation launched seven Harbor jobs (nop, oracle, one plausible-wrong, three tamper controls, the probe) and returned `invalid`:

| Check | Observed (expected) |
|---|---|
| nop base evidence | 73 of 120 PASS_TO_PASS passed; 47 failed on the untouched base (all pass) |
| oracle | 0 of 5 FAIL_TO_PASS passed; reward 0, success 0 (reward 1, success 1) |
| gold exact-id manifest | 52 of 125 ids not passed under the gold patch (all pass) |
| reachability probe | gold-line hits in `requests/__init__.py` and `build/lib/...` copies present at base (no hits) |
| plausible-wrong | reward 0, success 0, outcome 2 (success 0, reward at most 0.5) |
| tamper, three incorrect-exploit controls | reward 0 each, outcome 3, one `protected-path-edit` signal each (reward 0) |

Cause: the requests test suite talks to an HTTP test server over the network and the verifier is offline by contract. The tamper controls were dropped by the path policy and earned zero as required; the probe failures were the added-line false positive. psf/requests is now excluded from pool eligibility and the scenario was not registered. The sweep is also the first evidence that the hardened collector (root-only directory, two file artifacts, agent processes killed) reaches the separate verifier on Modal: every control produced a receipt and test evidence rather than outcome 5.

**First valid scenario.** None has passed validation yet. Its control table will be inserted here from its `validation.json` when it exists; no trial in any condition has run, so pass@k, pass^k and mean reward are not yet measured.
