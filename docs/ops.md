# Operations evidence

This is the evidence ledger, not the setup procedure. Use [setup](setup.md), [the runbook](runbook.md), and [architecture](architecture.md) for current operation. Dates are UTC unless labelled otherwise. Historical VM demonstrations remain separate from Cloud Run acceptance.

The user reprioritized completion to **all code and a properly hosted website over the preserved record**. New task discovery and experiments are deferred. The website checks below remain required; experimental rows are retained as deferred work, not as blockers to providing a verified website URL.

## Hosted takeover — 2026-09-13

Claude Code stopped at its workspace usage limit, missing the 08:00 PDT target. Codex resumed the original worktrees and preserved their interrupted changes. At takeover, the starting checkout was `30ef8af`, 53 commits ahead of the remote, and the authenticated Temporal Cloud namespace contained no workflows.

The first recovered offline run returned 785 passed, 3 skipped and 2 failures: an obsolete console model-default assertion and an order-dependent cost mock. A later integrated checkpoint recorded **849 passed, 3 skipped**, clean `ruff --select E9,F`, and valid Terraform. Evidence: `workspace/evidence/takeover-offline-tests.log`.

The operator renewed gcloud authentication and GitHub's workflow permission before deployment and pushing could resume. The ignored dotenv file was changed to mode 0600. No credential values belong in this ledger.

| Check | Observed result | Evidence |
|---|---|---|
| Cloud Build | Build `55086fb6` from commit `2bc7389` succeeded | `workspace/evidence/cloud/takeover-deploy-2bc7389.log`; Cloud Build execution |
| Terraform update | Added index-maintenance Job and supplied missing trial image, service account and SQL settings | `workspace/evidence/cloud/terraform-apply-20260913T164005Z.txt` |
| Hosted migration | Maintenance execution `swesim-index-maintenance-tl5t6` completed successfully | Cloud Run execution, reported by the operator |
| Temporal Cloud | API-key connection; authoring activity/workflow pollers 2/2, build activity 2, control activity 2, trials activity 1 | `workspace/evidence/cloud/verify_cloud-20260913T164426Z.txt` |
| Console access edge | Unauthenticated request returned HTTP 302 to Google sign-in | `workspace/evidence/cloud/iap-headers-20260913T164426Z.txt` |
| Runtime readiness | All four worker pools and console Ready; each used Terraform's pinned image | Same 16:44:26 verification file |
| Stakeholder | Redeployed in about 23.5 s after adding `python-ulid`; `/health` succeeded | `workspace/evidence/cloud/takeover-stakeholder-deploy.log` |
| Import pre-scan | 4,093 files, 6,837,576,392 bytes scanned; no known credential-value matches in the selected upload set | `workspace/evidence/cloud/takeover-import-scan.json` |
| New hosted graded outcomes | None established by these checks | Verification explicitly reported no trial Jobs |

The verified image digest was:

```text
sha256:c095bcb87841fad47686f3d1bdd43ea652b81175d6bcb8b2732e07a6f3c7e726
```

This is a dated deployment observation, not a claim that every later source commit is deployed.

### Correction to the earlier verification

`verify_cloud-20260913T091709Z.txt` used a plaintext localhost Temporal connection. It did not demonstrate the Cloud namespace or hosted workflow execution. Resource Ready conditions also missed a trials-pool crash loop: logs reported missing `SWESIM_WORKER_IMAGE` and `SWESIM_WORKER_SERVICE_ACCOUNT`, and the activity queue had zero pollers.

The updated verifier checks Cloud authentication and pollers as well as Ready/image state. The 16:44:26 file passes those stronger checks. It still **skips SQL status because the laptop has no Cloud SQL socket** and reports no trial Jobs. Registry presence is the only bucket-content check in that script; complete scenario/trial/index parity and post-cleanup execution evidence require separate verification.

### Model availability

`workspace/evidence/model-probe-takeover.json`, starting 16:24:23 UTC, records quota-limit errors for the configured Anthropic primaries. Tiny requests to the listed OpenAI and OpenRouter alternatives succeeded.

| Role | Resolved id in that probe |
|---|---|
| Judge | `openai/gpt-5.5` |
| Drafter | `openai/gpt-5.6-sol` |
| Stakeholder | `openai/gpt-5.6-sol` |
| Audit | `openai/gpt-5.6-sol` |
| Screen | `openrouter/deepseek/deepseek-v3.2` |

Judge and drafter initially collided on Sol. With the drafter chain exhausted, the resolver advanced the judge to GPT-5.5. The probe also reached `openrouter/z-ai/glm-5.3` and `openai/gpt-5-mini`. Reachability is not a graded result, and the solver is never silently substituted.

### Fresh-clone check

A new clone of pushed commit `29a3d41d8d4565d83a69308f3c38a7cf379d33af`, with a fresh copy of `.env.example` at mode 0600, ran `make check`. Result: **869 passed, 3 skipped, 1 failed in 70.76 s**. The failure was `test_reader_candidates_funnel_and_reject_histogram`: it expected `judge`, while the new reader correctly exposes `candidate:judge` plus `discovery:` counts.

A second fresh remote clone of `1fb1530b66066c17b832c9e4cffe73f1b0ffe262`, again with a new `.env` and virtual environment, ran the same command successfully: **895 passed, 3 skipped, 1 warning in 124.90 s**. The warning is the Starlette/AnyIO deprecation. The clone log was supplied to the integrator for the evidence record. Documentation local links/code fences passed, and 21 documented shell/Python blocks parsed without executing their commands.

No cloud bootstrap, deployment or trial was executed from that fresh clone. The operator is responsible for the separate idempotent `cloud-bootstrap` acceptance and authenticated browser checks.

## Preserved record inventory

A read-only inventory on 2026-09-13 found **50 trial records**, including **19 whose spec hash matches the current registered version-2 spec**, and 50 episode exports. The current subset contains 13 correct and 6 incorrect outcomes. There are 3 registered version-2 scenarios; their contracts contain respectively 1/84, 6/3 and 2/95 FAIL_TO_PASS/PASS_TO_PASS tests for seaborn, sklearn and pypdf.

These are preserved laptop/VM results. Importing them does not turn them into hosted runs. At the 16:44:26 verification, the bucket had a registry seed; upload of the full preserved record and SQL parity had not been established. The import log is `workspace/evidence/cloud/takeover-import-record.log`; completion and count comparison remain pending until recorded below.

The upload pre-scan covered known credential values and excluded trial task copies, job config/task files, locks and symlinks. Its no-match result is limited to that scan and selected upload set.

New projects now seed an empty registry rather than copying a catalog whose artifacts are absent. Bootstrap also deploys the stakeholder and reads its URL output when the dotenv file has none (`29a3d41`). These code changes remove two setup gaps; the fresh-project execution of the entire sequence remains to be measured.

## Outstanding hosted acceptance

| Required evidence | Status in this ledger | Exact next check |
|---|---|---|
| Complete record import | Pending | Import command finishes; compare bucket registry/scenario/trial objects and SQL row counts; inspect skipped records |
| Current release image and schema | Pending after each new deploy | Record build id, full digest, maintenance execution and migrated head revision |
| Hosted task creation | Deferred | When experiments resume, a `CreateTasksWorkflow` reaches an honest terminal result and a task passes controls |
| Hosted graded run | Deferred | A future `RunTasksWorkflow` produces frozen trial/config/spec, reward/report, and transcript binding |
| Launch-once and cleanup | Deferred experiment | Persist one Job/execution identity per new trial and cleanup evidence |
| Worker-pool restart | Deferred experiment | Restart a pool during a future trial; show the same runtime resumed and exactly one execution |
| IAP identity enforcement | Redirect measured; browser roles pending | Allowed account enters, denied account fails, ordinary allowed account cannot see evaluator material |
| Fresh-clone developer check | Passed at `1fb1530` | 895 passed, 3 skipped; repeat after a materially changed release |
| Fresh-clone bootstrap | Pending | Operator runs `cloud-bootstrap` against the configured project; repeat plan/apply is clean; console loads |
| Screen and model sweep | Deferred | Informative screen labels, actual Tier A–C runs, spend ledger and frontier gate |
| Disclosure/integrity audit | Historical audit exists; new experiments deferred | Audit future scenarios and delivered replies, index labels, explain every signal |
| Full acceptance scripts | Pending for final record | `scripts/verify.sh 10` and hosted verification against the final integrated image and record |

Do not replace a pending item with a planned command or a unit-test result. Add workflow/trial ids, timestamps and artifact paths when it is observed.

## Historical VM evidence

The VM backend used a local Temporal development server and one systemd unit per trial. Its results predate the Cloud Run path.

### Worker restart

Trial `26f3304d9caedfe10cd75ebf`, inside workflow `run-48574f7dd72f4ecfbd853dbea588f25b`, started its trial unit at 03:25:35 UTC. The worker restarted at 03:26:04 UTC, changing PID 5096 to 5647 while the trial unit remained active. The journal contains one start; one completed record reports reward 1.0; the restarted worker reconciled and ingested it.

Evidence beneath `workspace/evidence/vm/`: `workspace/evidence/swesim-trial-26f3304d9caedfe10cd75ebf.journal.txt`, `workspace/trials/26f3304d9caedfe10cd75ebf/{launch,record,report}.json`, and `workspace/evidence/run-history.json`.

### Laptop independence

Workflow `run-42874b1b2f724a129fa75fbfeb95a03e` ran from 03:46:06 to 03:55:38 UTC and completed two seaborn trials: `bb750ab72dd0751a972931ea` interactive and `b64138e19ab4e23fd94c54de` hidden, both reward 1.0. Its history has 41 events; the corresponding systemd journals and records are under `workspace/evidence/vm/`.

The reported no-SSH window was 03:46:03–04:10:46 UTC, written in `workspace/evidence/laptop_off_window.txt`. The lid remained open. **Laptop/session absence is operator-reported**: no independent SSH authentication log was found in the inspected mirror. The workflow artifacts establish VM progress during the reported interval, not physical laptop-off or Cloud Run acceptance.

The earlier VM creation workflow `create-da33e214d30d46a7a3eecc739c16b016` reached validation and ended invalid because its plausible-wrong patch did not apply. Its history is retained as a failure, not counted as an accepted task.

## Historical failures and resulting fixes

| Trigger | Observed failure | Resulting safeguard |
|---|---|---|
| requests-1944 | Network-dependent tests did not reproduce offline | Repository excluded from the pilot pool; probe measures the offline contract |
| sphinx-9467 | Published FAIL_TO_PASS tests already passed on untouched base | Empty measured FAIL_TO_PASS rejects a candidate |
| pytest-7283 / sklearn-13584 | Model-proposed protected paths shielded the oracle's source edit | Protected paths are code-owned; gold-touched source cannot be protected |
| sklearn-13584 | Whole-file and exact-id ordering produced different results | Definition-order exact-id probe and grading |
| pypdf-3826 / pytest-7283 | Corrupt model control patches failed before grading | Draft-time apply checks and fetched-source diff construction |
| Early pypdf trials | Missing gcc/make caused agent setup to attempt blocked apt access | Build tools installed in the task image |
| matplotlib-25547 | Base-image guard rejected the environment before controls ran | Infrastructure errors invalidate the scenario; they do not defeat attacks |
| sphinx-10097 | Alternative-correct control failed | Structural tasks require a passing alternative implementation |
| Initial v2 interactive workflows | Planner and runner hashed different scaffold identities | Shared effective scaffold identity; unresolved phantom ids remain in the record |
| Repository rebuild | Feature defaults obscured the successful bugfix band | Explicit discovery band and source identity |
| Reporter disclosure | Author-only requirements leaked through requirement text | Ground every requirement and sanitize all stakeholder-visible discoveries |
| Repeated withholding | One selected phrase could explain multiple removed sentences | Record each occurrence and reconstruct one occurrence per operation |
| Cached probes | Matching row metadata could reuse root-run or stale evidence | Binding recorded by the job, grading identity required on reuse and draft |
| Fallback controls | A docstring inversion could retain gold behavior | Full-source lexical state and distinct valid executable AST |
| Build provenance | Hash omitted runtime/render settings | Canonical configuration and rendered-file manifest, verified before use |
| Base reproducibility | Two image manifests were written but never compared | Collector digest and fail-closed verifier parity check |

The code changes above have offline regression coverage. Their presence does not retroactively validate old builds. Recompilation, controls and new hosted outcomes must name the code that ran.

## Retry and evidence rules

A retry is a new attempt index after reconciling any existing runtime. Preserve request, launch, status, record, reward, report and transcript files. Record the old trial id, terminal status, why a new attempt is justified and the new id. Never remove evidence merely to bypass duplicate-launch protection.

The root collector, isolated pytest launcher and report-consistency checks are tested defenses. Submitted code still shares the pytest process, so coordinated forgery remains a residual. Semantic stakeholder stance remains partly prompt-based and needs disclosure audit evidence.
