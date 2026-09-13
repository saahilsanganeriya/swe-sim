# Operations evidence

This is the evidence ledger, not the setup procedure. Use [setup](setup.md), [the runbook](runbook.md), and [architecture](architecture.md) for current operation. Dates are UTC unless labelled otherwise. Historical VM demonstrations remain separate from Cloud Run acceptance.

On 2026-09-13 the user first reprioritized completion to **all code and a properly hosted website over the preserved record**, then (ledger entry labelled 11:55 PDT) un-deferred the hosted smoke: the goal is a running pipeline to iterate on task generation, reward and tuning; cost and infrastructure polish are not priorities. The measured smoke is in [Hosted smoke (2026-09-13)](#hosted-smoke-2026-09-13); everything the finishing streams did not finish is in [Deferred from the 2026-09-13 finishing run](#deferred-from-the-2026-09-13-finishing-run). The website checks below remain required.

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

## Hosted smoke (2026-09-13)

The first workflows ever started in the Temporal Cloud namespace `swesim.abcde`, and the deploys around them. Times are UTC from the evidence file names; the ledger's PDT labels are not repeated here. Nothing in this section is a graded outcome.

### Deploys

| Commit | What happened | Evidence |
|---|---|---|
| `1976af3` | `make deploy` stopped at the open-executions guard before building: `swesim workflows open-types` tried `127.0.0.1:7233` (connection refused), so the guard could not list the Cloud namespace. The guard's report file is empty. | `workspace/evidence/cloud/deploy-1976af3.log`, `open-types-20260913T182145Z.txt` (0 bytes) |
| `609f923` (`609f923557ca0807e1e90cde0114057f77fce027`) | Guard: `# No open workflows`. Cloud Build `ae494b42-bf9e-4306-8f8b-7af7b168fb6b` SUCCESS (2M13S, started 18:22:38Z). Pinned `worker@sha256:f2b265054b9ea61d442957945d94ad89ee544e4683475ee41a85889bb8f425fc`. Terraform `Plan: 0 to add, 6 to change, 0 to destroy` (image `c095bcb8…` → `f2b26505…` on the four pools, the console and the maintenance Job); `Apply complete! Resources: 0 added, 6 changed, 0 destroyed`. Migration execution `swesim-index-maintenance-nzrvm` completed successfully. `verify_cloud` `result: OK`: Temporal api-key on `us-central1.gcp.api.temporal.io:7233` / `swesim.abcde` with pollers (`swesim-authoring` activity 2 / workflow 2, `swesim-build` activity 2, `swesim-control` activity 1, `swesim-trials` activity 1), IAP 302 to `accounts.google.com`, four pools and the console Ready on the pinned image, `registry.json` in the bucket. Notes: no `swesim-trial-*` Jobs yet; index status skipped (no Cloud SQL socket on the laptop). | `deploy-609f923.log`, `open-types-20260913T182230Z.txt`, `terraform-apply-20260913T182558Z.txt`, `verify_cloud-20260913T183003Z.txt`, `iap-headers-20260913T183003Z.txt` |
| `40c84d3` | Guard stopped the deploy: `CreateTasksWorkflow 1` open (the swebench creation below). | `deploy-40c84d3.log`, `open-types-20260913T183755Z.txt` |
| `1ca70a6` (`1ca70a66d142210bb25b4a6edf3744e204bcad30`) | Started with `OVERRIDE_OPEN_EXECUTIONS=1` (the guard's report carries the override line). Ledger ruling: every command-sequence change since the running image (frontier gate, lifecycle rows, requested-score-k, frozen retry) is behind `workflow.patched` markers and the open workflow is a `CreateTasksWorkflow` whose remaining commands are unchanged in shape; cost if wrong: one non-determinism failure on that workflow, which would be re-run. Cloud Build `31c3a7b9-91c9-49ac-ae43-a1fc4cb60509` was still running when this section was written (18:42Z). Record its digest, apply summary, migration execution and verify result here when it finishes. | `deploy-1ca70a6.log`, `open-types-20260913T184221Z.txt` |

The deploy of `609f923` is the newest verified deploy in this ledger. The image digest of a later deploy is not known until its log carries `pinned …@sha256:…` and `verify_cloud … result: OK`.

### Creation workflows

| Workflow id | Request | Observed | Evidence |
|---|---|---|---|
| `create-fa2489a292574fd19ba88796c0c18087` | `swebench:A:pytest-dev__pytest-7283`, auto approval | Started 18:31Z. Open at 18:37:55Z and 18:42:21Z (the guard listings). Ledger entry labelled 13:25: blocked on the missing `gh` binary too (`swebench_annotated.candidates` → `threads.fetch_pr_thread`); `mine_activity` at attempt 3 under the LLM retry class. Plan recorded there: once an image built from main ≥ `238fb33` is deployed, cancel it and resubmit. | `workspace/evidence/smoke/create-swebench-pytest-7283.log` (the id), `open-types-20260913T183755Z.txt`, `open-types-20260913T184221Z.txt` |
| `create-3b1513db47894549a189f678343774b9` | `https://github.com/encode/httpx`, band `bugfix`, max 3, auto approval | Started 18:31Z. Errored in discovery: `FileNotFoundError: 'gh'` — the worker image had no GitHub CLI. Fixed by commit `1555810` (`Dockerfile.worker` installs gh 2.100.0, checksum-verified; validated in a throwaway amd64 container per the ledger). Resubmission pending the deploy of that image. | `workspace/evidence/smoke/create-httpx.log` (the id), `Dockerfile.worker`, commit `1555810` |

No `RunTasksWorkflow`, trial Job or graded outcome exists on the hosted stack at the time of writing (`verify_cloud-20260913T183003Z.txt`: `NOTE no swesim-trial-* Jobs yet`).

Lesson recorded for the image: the worker image must carry every binary the stages shell out to (`git`, `gh`); a `gh --version` check inside the image belongs in `verify_cloud` (deferred, infra list below).

### Model-role fallbacks in force

The overnight probe (`workspace/evidence/model-probe.json`, probed 2026-09-13 08:37Z) reached all 25 ids with no substitutions. After the Anthropic workspace API usage limit ("regain access 2026-10-01" in the operator's account) the takeover probe (`workspace/evidence/model-probe-takeover.json`, 16:24Z) recorded `anthropic/claude-opus-5` and `anthropic/claude-sonnet-5` as unreachable (HTTP 400 `invalid_request_error`; the provider text is redacted in the file) and resolved:

| Role | Configured (`swesim/roles.py` `ROLE_CHAINS` head) | Resolved | Reasoning line in the file |
|---|---|---|---|
| judge | `anthropic/claude-opus-5` | `openai/gpt-5.5` | fell back to `openai/gpt-5.6-sol`, then "judge and draft both resolved to openai/gpt-5.6-sol; draft chain exhausted, advanced the judge chain to openai/gpt-5.5" |
| draft | `anthropic/claude-sonnet-5` | `openai/gpt-5.6-sol` | "anthropic/claude-sonnet-5 unavailable; fell back to openai/gpt-5.6-sol" |
| stakeholder | `anthropic/claude-opus-5` | `openai/gpt-5.6-sol` | "anthropic/claude-opus-5 unavailable; fell back to openai/gpt-5.6-sol" |
| audit | `anthropic/claude-opus-5` | `openai/gpt-5.6-sol` | same |
| screen | `openrouter/deepseek/deepseek-v3.2` | unchanged | — |

Hosted workflows re-probe in `probe_models_activity` and write their own resolution to `evidence/model-probe.json` in the bucket; the laptop files above are the operator's probes, not the workers'. The judge remains `anthropic/claude-opus-5` by the user's directive (see [grading](grading.md#judge-calibration)); today's runs used `openai/gpt-5.5` because of the limit. The solver is never substituted.

## Preserved record inventory

A read-only inventory on 2026-09-13 found **50 trial records**, including **19 whose spec hash matches the current registered version-2 spec**, and 50 episode exports. The current subset contains 13 correct and 6 incorrect outcomes. There are 3 registered version-2 scenarios; their contracts contain respectively 1/84, 6/3 and 2/95 FAIL_TO_PASS/PASS_TO_PASS tests for seaborn, sklearn and pypdf.

These are preserved laptop/VM results. Importing them does not turn them into hosted runs. At the 16:44:26 verification, the bucket had a registry seed; upload of the full preserved record and SQL parity had not been established. The import log is `workspace/evidence/cloud/takeover-import-record.log`; completion and count comparison remain pending until recorded below.

The upload pre-scan covered known credential values and excluded trial task copies, job config/task files, locks and symlinks. Its no-match result is limited to that scan and selected upload set.

New projects now seed an empty registry rather than copying a catalog whose artifacts are absent. Bootstrap also deploys the stakeholder and reads its URL output when the dotenv file has none (`29a3d41`). These code changes remove two setup gaps; the fresh-project execution of the entire sequence remains to be measured.

## Outstanding hosted acceptance

| Required evidence | Status in this ledger | Exact next check |
|---|---|---|
| Complete record import | Pending | Import command finishes; compare bucket registry/scenario/trial objects and SQL row counts; inspect skipped records |
| Current release image and schema | `609f923`: build `ae494b42…`, digest `f2b26505…`, execution `swesim-index-maintenance-nzrvm` ([Hosted smoke](#hosted-smoke-2026-09-13)); `1ca70a6` in progress | Record build id, full digest, maintenance execution and migrated head revision (`0004_history` is the single Alembic head on main) after each deploy |
| Hosted task creation | Started 18:31Z; both creations blocked on the missing `gh` binary; resubmission pending the `1555810` image ([Hosted smoke](#hosted-smoke-2026-09-13)) | A `CreateTasksWorkflow` reaches an honest terminal result and a task passes controls |
| Hosted graded run | Not started (planned: `run-tasks` hidden k=1 on `openrouter/deepseek/deepseek-v3.2` once a task registers) | A `RunTasksWorkflow` produces frozen trial/config/spec, reward/report, and transcript binding |
| Launch-once and cleanup | Deferred experiment | Persist one Job/execution identity per new trial and cleanup evidence |
| Worker-pool restart | Deferred experiment | Restart a pool during a future trial; show the same runtime resumed and exactly one execution |
| IAP identity enforcement | Redirect measured; browser roles pending | Allowed account enters, denied account fails, ordinary allowed account cannot see evaluator material |
| Fresh-clone developer check | Passed at `1fb1530` | 895 passed, 3 skipped; repeat after a materially changed release |
| Fresh-clone bootstrap | Pending | Operator runs `cloud-bootstrap` against the configured project; repeat plan/apply is clean; console loads |
| Screen and model sweep | Deferred | Informative screen labels, actual Tier A–C runs, spend ledger and frontier gate |
| Disclosure/integrity audit | Historical audit exists; new experiments deferred | Audit future scenarios and delivered replies, index labels, explain every signal |
| Full acceptance scripts | Pending for final record | `scripts/verify.sh 10` and hosted verification against the final integrated image and record |

Do not replace a pending item with a planned command or a unit-test result. Add workflow/trial ids, timestamps and artifact paths when it is observed.

## Deferred from the 2026-09-13 finishing run

Compiled from the five finishing reports (`.superpowers/sdd/2026-09-13-hosted-pipeline/reports/{frontier-2,history-2,website-2,costs-2,infra-2}.md`) and the ledger. Each line: what — why it was deferred — where it goes. "Direction" means the user's 11:55 PDT essentials-only ruling relayed by the integrator.

Frontier and launcher (`frontier-2.md`):

- Planned/launched/running trial rows (review P1-4) — cross-stream contract with storage; not required to iterate — `prepare_trial_plan_activity` upserts `planned`; `TrialWorkflow` hooks `index_trial_activity` with `status` after the launch acknowledgement; `await_trial_activity` refreshes `running`; storage's `index_trial` accepts `status` + `request`/`request_path` (payload shape in the report's concern 4).
- `reconcile_workflows_activity` + `swesim workflows reconcile` (P1-6, second half) — needs a Temporal connection to test beyond fakes; lifecycle rows already remove the common stale-`running` cases — `client.py:reconcile_workflows()` describing every `running` row and upserting the Temporal-observed status with `lifecycle_source="temporal"`; CONTROL activity; CLI command.
- Request bounds, per-run spend cap, fail-closed spend check (P2-16) — changes env-test semantics for uncapped dev runs; the frontier cap-in-force part shipped — `swesim/workflows/bounds.py` checked in `client.start_run/start_create` and at the top of both operator workflows; `spend_check_activity` reports min(run, global); `_spend_exceeded` returns `stopped_reason="spend-unknown"` on an unreadable ledger.
- `validation.run_control` transient classification (P2-17) — validation is not on the iteration path — Harbor infrastructure exceptions → `retries.transient(kind="HarborInfrastructure")` with a fresh `<job>--attempt-<n>` directory (max 3); `assemble_validation` treats an infrastructure-class directory as "nothing measured".
- Console surfaces `admission.reasons`, `include_saturated` and a per-run `spend_cap_usd` input (concern 3) — UI completeness — `dashboard/console.py:validate_run` and the Run form; progress query `stopped_reason == "frontier-gate"`.
- Timing flake `tests/test_workflow_batching.py::test_threaded_launch_finishes_before_cancellation_stops_runtime` (concern 1) — pre-existing, not in the narrowed scope — give the fake launch activity a short heartbeat timeout or assert on the cancel receipt.

History, registry and provenance (`history-2.md`):

- Storage half of the live trial rows (item 4, P1-4) — only useful together with the launcher call sites above — `swesim/index/activities.py:index_trial` builds a row from `request`/`request_path` when no terminal files exist, never regressing a terminal row; `WorkflowStatus.unknown` for reconciled, not-retained executions.
- Provenance fields `worker_image_digest, origin, workflow_id, run_id` on `TrialRecord`, `approval.json`, `compile.json`, `validation.json` (item 5, P2-11) — three of the four writers are other streams' files — `launchers/base.py:launch_once`, `stages/compile.py`, `workflows/validation.py` / `stages/validate.py`, `stages/run.py:TrialRecord`; projections already prefer file provenance when present.
- `reconcile_workflow_rows(store, temporal_listing)` writer (item 6, P1-6) — only the reconciler function and its caller remain (schema column, `choose_workflow` precedence and the activity payload field are in place) — `swesim/index/history.py` + the CONTROL activity above.
- `CandidateWorkflow` does not yet call `index_candidate_event_activity` (concern C2, exact call sites and payload in the report) — dispatched separately as the `finish/receipts` stream (ledger entry labelled 13:20); not merged when this list was written — `swesim/workflows/workflows.py`.

Website, discovery and integrity (`website-2.md`):

- SQL half of the scan record: `DiscoveryRow.funnel/decisions/scan_path/scan_sha256` + migration `0005_discovery_integrity` (item 2) — direction; the file-backed Candidates page is enough to iterate; the migration's parent `0004_history` was not on main at the time — `swesim/index/{models,schema,project}.py`, a new Alembic revision with `down_revision="0004_history"`; head expectations in `tests/test_index_{schema,cli,store}.py`.
- §7 integrity views on the leaderboard (item 4): per-model `integrity_signal_count` distribution with flagged-trial links, protected-config touch detector, small-patch-vs-oracle heuristic, useful-question-rate columns, interactive-audit completeness badge — direction; measurement surface — `swesim/index/reports.py`, `LeaderboardRow`, `pages.leaderboard_body`.
- Facets without the first-500 subsets (item 6) — direction; UI completeness — `app.py:/trials` facets and the `limit=500` candidate lookup.
- Authenticated-page screenshots in both modes into `workspace/evidence/console/finish/` (item 7) — direction; needs the console running locally over the file reader — command in the report.
- Discovery retry policy (concern 3): `DiscoveryUnavailable` is retried under the default `retries.LLM` policy (4 attempts, 10s→2m); an `auth` failure (401/403) is retried too — decide `retries.READ`/dedicated policy and fail-fast on `kind == "auth"` — `swesim/workflows/activities.py:discover_activity`.
- Legacy `mine_github_repo` (`swesim/stages/mine.py`, debug-only) now propagates `DiscoveryUnavailable` instead of counting `fetch-error` (concern 4) — behaviour change in an unowned file; acceptable for a debug stage — note only.
- Hosted Candidates page lists `candidates/*/scans/` on the GCS mount per request (concern 5) — fine for tens of sources — the SQL half above or a TTL cache like `data._live_cache`.

Costs (`costs-2.md`, no deferred scope; follow-ups named in its concerns):

- Separate "served by" cell for `price_source` values such as `openrouter-generation (served by DeepInfra)` — optional readability — `swesim/dashboard/pages.py` cost table via `costs.served_providers_of` / `costs.price_source_key`.
- `record_cost_activity` passes no `idempotency_key` while `stages/run.py:_solver_cost` passes `"solver-final"` — both idempotent today; aligning gives one `cost_id` per trial — `swesim/workflows/activities.py:record_cost_activity`.
- Surface `accounting_summary()['complete']` next to every spend figure — handoff item — website/history owner.
- OpenRouter `provider.order` pinning — a RunConfig-relevant request-body knob inside the agent container that cannot be verified offline — `swesim/stages/run.py` once a smoke trial can confirm the served provider changes.

Infra (`infra-2.md`):

- File/SQL parity through a read-only maintenance Job `swesim-parity` (item 3, P2-13) — direction; also needs `scripts/cloud/parity_report.py` copied into `Dockerfile.worker` and excepted in `.dockerignore` — `infra/modules/run/main.tf` next to `index_maintenance`, `scripts/cloud/verify_cloud.sh` check 8; report to `evidence/cloud/parity-<UTC>.json`.
- Cloud Build trigger on push to main (item 5a, P3-18) — direction — `google_cloudbuild_trigger` in `infra/modules/registry/main.tf` (or a `build` module) with `cloudbuild_repository` / `cloudbuild_trigger_enabled` variables; the trigger builds only, rolling stays with `make deploy`.
- Selective open-executions check (item 5b, P3-18) — direction — `scripts/cloud/open_executions.py` mapping changed files in `<running sha>..<new sha>` to `@workflow.defn` types, conservative fallback to all types.
- Alert on the reconcile metric > 1 in 10 minutes (item 5c, P3-18) — direction; a fresh user metric is not queryable for a few minutes after creation (needs a second apply) — `google_monitoring_alert_policy` in `infra/modules/logging/main.tf`.
- `verify_cloud` runs `gh --version` (and the other shelled-out binaries) inside the image — lesson from the smoke above — `scripts/cloud/verify_cloud.sh`.

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
