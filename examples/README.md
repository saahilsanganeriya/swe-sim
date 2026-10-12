# examples/

Tracked, sanitized snapshots of validated scenarios and completed runs, copied out of the git-ignored `workspace/`
by `scripts/snapshot_example.sh`. Everything under `examples/<scenario_id>/` is text, carries no credential, and is
listed with a sha256 in that package's `manifest.json`; `bash scripts/verify.sh 10` re-hashes every listed file,
requires each to be tracked, and scans all tracked text for credential-like strings. A package is enough to read
how a task was built, what the controls did, what an agent submitted, and why it earned the reward it did, without
Modal, Docker, Temporal or the stakeholder service.

## What an example contains

| Path | What it is | Written by |
|---|---|---|
| `manifest.json` | `scenario_id`, `spec_hash`, `snapshot_at`, `files` (package-relative path to sha256), `skipped`, the redaction and path-rewrite bookkeeping, a per-run summary | the snapshot script |
| `scenario.json` | the frozen scenario: source thread, persona, ledger facts with quoted spans and URLs, natural and withheld briefs, requirements and groups, checks, controls, environment. `spec_hash` is its content hash | `draft` (model, code-validated) |
| `assets/` | `oracle.diff`, `test.patch` (the hidden FAIL_TO_PASS tests), `plausible-wrong.diff`, the three tamper diffs | `draft` |
| `approval.json` | which code-owned gates passed (`approved_by: auto-validators` means no human looked; `review_needed` stays true) | `approve` |
| `compile.json` | `compiled_hash`, pinned `image_digest`, `run_config_hash`, agent hosts, the three task directories | `compile` |
| `validation.json` | executed controls on Modal (`nop`, `oracle`, one plausible-wrong, three tamper), each with the full reward row and its expectation, plus the git-history probe; `status: valid` is what lets the scenario exist | `validate` |
| `scores.json` | per `condition|brief_profile|run_config_hash`: `n`, `pass_at_k`, `pass_pow_k`, `mean_reward`, outcome counts, `trial_ids`, unresolved attempts | `score` |
| `task__full/`, `task__hidden/`, `task__interactive/` | the Harbor 0.23 task directories: `task.toml`, `instruction.md`, `environment/` (agent image, root-only collector, `ask-user`, `confirm-behavior` and `request-review` shims), `tests/` (verifier image, `test.sh`, `grader.py`, `policy.py`, `parsers.py`, `run_pytest.py`, `test.patch`, `grader_config.json`), `solution/`, `controls/` | `compile` |
| `runs/<trial_id>/record.json` | the ledger row: condition, profile, model, attempt, `spec_hash`, `compiled_hash`, `run_config_hash`, timings, reward, report, transcript | `run` |
| `runs/<trial_id>/reward.json` | the finite numbers the verifier wrote (`reward`, `success`, F2P and P2P counts, groups, `integrity_signal_count`, `outcome_code`, `patch_applied`) | verifier |
| `runs/<trial_id>/report.json` | the verifier report (`per_test`, `per_group`, `applied_paths`, `dropped_paths`, `integrity_signals`, `regression`, `receipt`, runner tail) plus the fields the run stage adds: `transcript_binding_valid`, `simulator_validity`, `image_digest`, `harbor_exception`, `reviewed_equals_submitted` | verifier, then `run` |
| `runs/<trial_id>/transcript.json` | the stakeholder's record of the interactive session (empty `{}` for `full` and `hidden`) | the hosted stakeholder service |
| `runs/<trial_id>/episode.json` | the export: `policy_visible` (instruction, agent trajectory, replies as the agent saw them), `evaluator_only` (report, ledger, author-only facts, test and oracle assets, audit), `lineage` (every hash), `interaction`, `training_eligible` | `export` |
| `runs/<trial_id>/agent/trajectory.json` | the agent's own record of its steps (ATIF, from mini-swe-agent through Harbor) | the agent harness |
| `runs/<trial_id>/verifier/` | `submission.diff`, the raw `report.json` and `reward.json`, `grader.log`, `runner_stdout.txt`, `test-stdout.txt` as the verifier container wrote them | verifier |

Not included: the collected tree `app.tar` (about 6 MB per run), the raw `mini-swe-agent.*` logs, the per-trial task
copy (it carries the session token), and `run_config.json`/`spec.json` (their hashes are in `record.json`, and the
spec is `scenario.json` whenever `record.spec_hash` equals the package `spec_hash`, which the script enforces).

Sanitization is recorded, not assumed. `manifest.redaction` names the rules (`SWESIM_SESSION_TOKEN` values,
`Authorization` and `x-api-key` header values, bearer tokens, and known credential values from the trial's task copy,
the ignored env file and the environment) and lists `files_changed`; the replacement is always `<redacted>`.
`manifest.path_rewrites` lists how absolute host paths were rewritten: to `runs/<trial_id>` or `task__<condition>`
when the target is in the package, to a repo-relative `workspace/...` path when it is not.

The stakeholder host in `compile.json` and the three `task.toml` files is the placeholder
`example--swesim-stakeholder-web.modal.run`, not the deployment that ran these trials. The manifest digests cover
the files as tracked. The recorded `compiled_hash` was computed over the deployed host, so recompiling with your own
stakeholder URL gives a different hash.

## The included example: `mwaskom--seaborn-3190` (version 2)

SWE-bench annotated instance `mwaskom__seaborn-3190` (boolean data in a continuous color scale), rebuilt as version 2 on
2026-09-13 through `CreateTasksWorkflow` with `--rebuild`: spec `812be6e0...`, compiled `e208a80f...`, image
`sha256:5ce876a5...`, validated on Modal. `validation.json` shows the control table that admitted it: `nop` reward 0 (F2P 0
of 1, P2P 84 of 84, outcome 3), `oracle` reward 1 (outcome 1), one plausible-wrong control reward 0, each of the three
tamper controls reward 0 with a `protected-path-edit` signal, and a probe that could not reach the gold commit from the
shipped git object database. `task__full/tests/grader_config.json` has 85 expected ids (1 FAIL_TO_PASS, 84 PASS_TO_PASS)
and one requirement group `g01`. Version 2 differs from version 1 in three ways: the stakeholder speaks as the reporter and
answers `confirm-behavior` (the `environment/shims/` directory now has three shims), the fact that described the regression
test is author-only, and the withholding model selected no spans on this draft, so the scenario has only the `natural`
brief (`scenario.json` → `briefs`). Version 1 is archived on the VM under `workspace/scenarios/mwaskom--seaborn-3190/archive/v1/`.

Three runs, all on the `natural` brief, all reward 1.0, chosen to show the v2 interaction:

- `runs/c1846a15feb5afc61bfdc76e/` is `interactive`, `claude-sonnet-5`: the agent asked one question that stated its
  intent (treat booleans as categorical or as 0/1 on a continuous scale?), the stakeholder answered in behavior ("False ->
  0.0 and True -> 1.0 … should not fail"), the agent then ran `confirm-behavior` with a description of the fix and the
  stakeholder confirmed it with no unmet requirement. Patience 6 → 3. The transcript is frozen, its hash recomputes from
  `events` and equals `report.receipt.transcript_hash` (`transcript_binding_valid` true); every reply carries
  `regenerated: false` and an empty `redacted_terms`.
- `runs/dc88e7322955749303867fee/` is `interactive`, `claude-haiku-4-5`: one question with the same two options, the same
  behavior-level answer, one `confirm-behavior`, reward 1.0 with 85 of 85 tests passed.
- `runs/f9435904e9de18475cbd980e/` is `hidden`, `claude-haiku-4-5`: the same brief with no stakeholder, reward 1.0. It is
  the control for the two interactive runs; on this scenario the model does not need the stakeholder to pass.

`training_eligible` is false on the interactive runs because interactive episodes stay `simulator_validity: unadjudicated`
until a person labels them. All three `compiled_hash` values match the package.

## Reproducing the grading offline

Read, in this order, then recompute; nothing here needs a network.

1. Integrity. Every file the manifest lists hashes to the recorded digest:
   ```bash
   uv run --frozen python -c "
   import hashlib, json, pathlib
   root = pathlib.Path('examples/mwaskom--seaborn-3190'); m = json.load(open(root / 'manifest.json'))
   bad = [p for p, h in m['files'].items() if hashlib.sha256((root / p).read_bytes()).hexdigest() != h]
   print('ok' if not bad else bad)"
   ```
2. What the contract was. `task__<condition>/tests/grader_config.json`: `expected_tests` maps every id to `f2p` or
   `p2p`, `groups` maps every FAIL_TO_PASS id to one weighted group, `protected` lists the paths a submission may not
   change (`tests/test.patch` is applied over them before running). `instruction.md` is what the agent saw.
3. What was submitted. `runs/<trial_id>/verifier/submission.diff` is a unified diff the verifier regenerated from the
   pristine tree and the graded tree for `report.applied_paths` (dropped protected paths are not in it).
   `report.submission_sha256` hashes the whole submitted change set and `report.patch_sha256` the applied one; when
   they differ, `dropped_paths` says why.
4. What the verifier observed. `report.per_test` gives `expected` and `observed` per id from the four-way evidence
   cross-check (collected ids, JUnit, terminal summary, exit code); `per_group`, `regression`, `integrity_signals`,
   `receipt` (collector status, tree hash, transcript hash) and `runner_tail` are there too.
5. Recompute the reward from 2 and 4 and compare with `reward.json`:
   ```bash
   uv run --frozen python -c "
   import json
   run = 'examples/mwaskom--seaborn-3190/runs/c1846a15feb5afc61bfdc76e'
   rec = json.load(open(run + '/record.json')); rep = json.load(open(run + '/report.json'))
   cfg = json.load(open('examples/mwaskom--seaborn-3190/task__%s/tests/grader_config.json' % rec['condition']))
   observed = {t['id']: t['observed'] for t in rep['per_test']}
   regression = any(observed.get(t) != 'passed' for t, k in cfg['expected_tests'].items() if k == 'p2p')
   reward = sum(g['weight'] for g in cfg['groups'] if all(observed.get(t) == 'passed' for t in g['tests']))
   reward = 0.0 if regression else min(1.0, round(reward, 8))
   success = int(bool(observed) and all(v == 'passed' for v in observed.values()))
   print(reward, success, json.load(open(run + '/reward.json')))"
   ```
   This is the rule in `swesim/verifier/grader.py` (and `docs/grading.md`): credit per requirement group, zero on any
   PASS_TO_PASS failure, `success` only when every expected id passed. `reward` is exported for training and for
   explaining near misses; `success` and `outcome_code` decide pass rates. Outcomes 3 to 7 (`agent-invalid-submission`,
   `agent-runtime-failure`, `collection-failure`, `simulator-failure`, `evaluator-failure`) never carry credit; 5 to 7
   are infrastructure and leave the pass-rate denominator. A submission with nothing left after the path policy is
   outcome 3 regardless of what the tests would say.
6. Optional re-execution, not offline: the task directories are complete Harbor tasks with a pinned image digest, so
   with Modal credentials `uv run --frozen harbor run --max-retries 0 -p examples/mwaskom--seaborn-3190/task__full
   --agent oracle -e modal --yes` rebuilds the images and must reproduce the `oracle` row of `validation.json`
   (`--agent nop` the `nop` row). Only Modal has been exercised. `task__interactive` needs a live stakeholder session
   and is run through `swesim run`, not `harbor` directly.

## Two records of one episode

`agent/trajectory.json` is the agent's own account, written by the agent harness inside the sandbox: useful for
reading what it tried, never an input to grading, and not protected against a policy that misreports. The verifier
grades the collected tree, not the trajectory.

`transcript.json` is the stakeholder's account, written by the hosted service outside the sandbox: every question,
review and reply with sequence numbers and patience, then a freeze. At the end of the agent phase the root-only
collector archives the tree and, in the same hook, calls `POST /freeze`; the service seals the transcript, hashes its
`events` (`sha256` of the canonical JSON, `sort_keys=True`, separators `,` and `:`), and that hash is written into the
collector receipt beside the tree hash. The run stage then fetches the transcript and sets
`report.transcript_binding_valid` only if the session is frozen and the fetched hash equals the receipt's. Recheck it:
```bash
uv run --frozen python -c "
import hashlib, json
run = 'examples/mwaskom--seaborn-3190/runs/c1846a15feb5afc61bfdc76e'
t = json.load(open(run + '/transcript.json')); r = json.load(open(run + '/report.json'))
h = hashlib.sha256(json.dumps(t['events'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
print(t['frozen'], h == t['transcript_hash'] == r['receipt']['transcript_hash'], r['transcript_binding_valid'])"
```
The snapshot script recomputes this for every copied transcript and refuses to write if it does not match; the
manifest records `transcript_hash_recomputed` and `transcript_modified_by_redaction` per run. A redaction inside a
transcript would break the hash, so it must never be needed: the session module that writes `events`
(`swesim/stakeholder/session.py`) never handles the token, which the service checks and discards before the
session sees a request.

## Snapshotting another scenario

Preconditions: `workspace/scenarios/<id>/validation.json` says `valid`; `compile.json` matches the three
`workspace/tasks/<id>__*` directories; each trial has `record.json` (status `completed`, same `spec_hash`),
`reward.json`, `report.json`, `transcript.json`, its Harbor job directory still on disk, an export, and an entry in
`scores.json`. Then:

```bash
SWESIM_WORKSPACE=$PWD/workspace uv run --frozen swesim score <scenario_id> --k 3
SWESIM_WORKSPACE=$PWD/workspace uv run --frozen swesim export <scenario_id>
bash scripts/snapshot_example.sh <scenario_id> <trial_id> [<trial_id>...]
bash scripts/verify.sh 10
git add examples && git commit
```

The script copies exactly the file set above, redacts before anything is written, rewrites host paths, refuses
binary files (they cannot be scanned), skips `runner_stdout.txt` and `test-stdout.txt` over 200 KB and lists them
under `manifest.skipped` with the source digest, writes the manifest, and finishes with an independent `grep` for
credential-like strings that deletes the package on a hit. It never overwrites an existing `examples/<id>/`: remove
it deliberately, or set `SWESIM_EXAMPLES_DIR` to write elsewhere. `SWESIM_WORKSPACE` and `SWESIM_ENV_FILE` are
honoured as everywhere else. Keep a package near or under 5 MB; this one is 95 files and about 1.8 MB.
