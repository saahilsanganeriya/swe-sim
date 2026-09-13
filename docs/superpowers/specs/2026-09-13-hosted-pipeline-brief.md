# Brief: swesim as a hosted production pipeline on GCP, plus the overnight multi-model sweep

Written 2026-09-13 ~07:30 UTC (about midnight PDT) for the goal that runs until 2026-09-13 08:00 PDT (15:00 UTC).
This brief is the detailed companion to the `/goal` text; the goal names the conditions, this document explains what
they mean, how to verify each one, and what to do when something does not fit. Everything measured here refers to
artifacts on disk or on the VM; nothing is asserted without a path.

---

## 0. Where we are (measured, as of this brief)

- **Code:** `main` at `15bf683` (pushed). Offline suite 549 tests green; `bash scripts/verify.sh 10` rc 0 against the
  laptop mirror of the VM record.
- **Orchestration today:** one GCE VM `swesim-worker` (us-central1-a, project `YOUR_PROJECT`) runs the Temporal
  dev server (SQLite), one worker (single task queue `swesim`, `max_concurrent_activities=16`), and the console. Trials
  launch as transient user systemd units (`swesim-trial-<id>`), launch-once with reconcile; proven by the worker-restart
  and laptop-off evidence in `workspace/evidence/ops.json`.
- **Sandboxes:** every image build, control run and graded trial runs through Harbor 0.23's Modal backend
  (`harbor run -e modal`). The verifier is a separate Modal container with no network. The stakeholder is a Modal web
  endpoint. Local Docker is optional smoke only. Daytona is not used.
- **Record:** the workspace is a directory on the VM disk; `scripts/vm/pull.sh` mirrors it to the laptop.
  `registry.json` is tracked. 44 trial records; three valid scenarios (`mwaskom--seaborn-3190` v2,
  `scikit-learn--scikit-learn-13584` v2, `py-pdf--pypdf-3826` v1, with a v2 rebuild running as `create-22cefb3a…`).
- **Stakeholder v2 measured:** 12 Temporal trials on the two v2 scenarios, all reward 1.0; Sonnet 5 asked with intent
  and confirmed behavior in 4 of 4 interactive trials, Haiku 4.5 in 2 of 4; all 12 replies `regenerated: false`, no
  redactions. These two scenarios do not separate hidden from interactive for those models (see §5, difficulty).
- **Models in use now:** judge `openai/gpt-5.5`, draft `anthropic/claude-sonnet-5`, stakeholder `openai/gpt-5.5`,
  solvers Haiku 4.5 / Sonnet 5 / Opus 5 (Anthropic) via Harbor's mini-swe-agent adapter. **No OpenRouter path exists in
  the solver run** (`swesim/stages/run.py:PROVIDER_HOSTS` knows `anthropic` and `openai` only).
- **Verified model ids on the configured keys (2026-09-13 07:20 UTC):** OpenAI: `gpt-6-astra`, `gpt-5.6-sol`,
  `gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.5`, `gpt-5-mini`. Anthropic: `claude-fable-5-1`, `claude-fable-5`,
  `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5-20251001`. OpenRouter catalog (prices per M tokens in/out):
  `deepseek/deepseek-chat-v3-0324` 0.25/1.00, `deepseek/deepseek-v3.2` 0.27/0.40, `deepseek/deepseek-v4-flash`
  0.05/0.10, `deepseek/deepseek-v4-pro-0813` 0.58/1.73, `deepseek/deepseek-v4.1-flash` 0.15/0.60, `z-ai/glm-4.5`
  0.60/2.20, `z-ai/glm-4.6` 0.43/1.75, `z-ai/glm-5` 0.60/1.92, `z-ai/glm-5.2` 0.60/2.00, `z-ai/glm-5.3` 1.09/3.43,
  `z-ai/glm-5.3-flash` 0.15/0.50, `qwen/qwen3-coder` 0.30/1.00, `qwen/qwen3-coder-next` 0.12/0.80, `qwen/qwen3.6-plus`
  0.33/1.95, `moonshotai/kimi-k2` 0.57/2.30, `moonshotai/kimi-k2.7-code` 0.71/3.50, `minimax/minimax-m2` 0.26/1.02,
  `minimax/minimax-m3` 0.30/1.20, `openai/gpt-oss-120b` 0.04/0.17, `openai/gpt-5.6-luna` 0.20/1.20.

## 1. Decisions taken with the user (2026-09-13, brainstorming)

| Question | Decision |
|---|---|
| Priority if not everything lands by 8 am | **Infra first.** Temporal Cloud, GKE workers, IAP console, Cloud SQL live before the sweep; the sweep runs only on the new stack. |
| Temporal | **Temporal Cloud**; the user is creating the account now and will place `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_API_KEY` in `~/Documents/.env`. Client support exists (`swesim.workflows.client.connect_kwargs`). |
| Database | **Cloud SQL Postgres as the query index; GCS as the record of truth.** Files stay authoritative; tables serve the console and the score tables. |
| Compute | **Cloud Run** (GCP's ECS/Fargate equivalent; no Kubernetes): one **Cloud Run worker pool per task queue**, **one Cloud Run Job per trial** named by trial id, the console as a Cloud Run service behind IAP. Harbor inside the Job still executes on Modal. GKE was considered and rejected for tonight: it is the EKS equivalent and adds a cluster, RBAC, kustomize and External Secrets for no capability we need. |
| Creation scale | **SWE-bench pool A (all 109) + ten repositories** (bugfix band): py-pdf/pypdf, pydantic/pydantic, fastapi/fastapi, encode/httpx, psf/black, pytest-dev/pytest, marshmallow-code/marshmallow, Textualize/rich, python-poetry/poetry, sqlfluff/sqlfluff. |
| Spend | About **$1,500 or more is acceptable**, but the sweep must **focus on cheap models** (OpenRouter open models, OpenAI small models, Anthropic Haiku); **frontier models only once the path is proven with trends**. Spend tracked per run in the console. |
| Console access | **HTTPS load balancer + Identity-Aware Proxy**, the user's Google account only, Google-managed certificate on a nip.io/sslip.io hostname unless a domain is named later. |
| Pipeline LLMs (judge, stakeholder, drafter) | **Very well-aligned frontier models only**: judge and stakeholder on `claude-opus-5` or `gpt-5.6-sol` (the user said "GPT 5.6 Soul"; `gpt-5.6-sol` is the id that exists — confirm the variant with the user; `luna`/`terra` also exist). |
| Evaluated agents | **Older-generation and newer-generation DeepSeek and GLM first**, then other cheap open and small closed models; **frontier (GPT-6 Astra, Fable 5.1, Opus 5, gpt-5.5) only after the path is tested.** |
| Tasks | Must be **hard enough**: difficulty is measured, not asserted (§5). |
| Runtime (confirmed ~07:50 UTC) | **Cloud Run**, not GKE. The user first asked about the ECS equivalent, then leaned GKE, then confirmed Cloud Run for tonight; the launcher contract and queue split are runtime-neutral so GKE remains a later Terraform module. |
| "GPT 5.6 Soul" | Mapped to `gpt-5.6-sol` (the id that exists; `luna` and `terra` also exist). Opus 5 is primary for judge and stakeholder, `gpt-5.6-sol` the fallback, unless the user swaps them. |
| Primary objective (added ~08:20 UTC) | **Creating tasks from new repositories given as links is the main goal of swesim.** Pool A (the 109) is the starting point and the calibration set, not the product. The sweep runs the repositories first, in both bands, with a per-repository discovery funnel in the console, and targets at least 10 valid repository-sourced scenarios by 08:00 PDT. No reward hacking (§7) and tasks and trajectories that are hard enough (§5) are explicit goals, not side effects. |
| Reusability (added ~08:35 UTC) | **A stranger must be able to clone the repository, put in their own API keys, set up the environment and run it.** `.env.example` lists every variable with a comment; `make check` runs the offline suite; `make dev` runs a local stack (Temporal dev server, worker with the local launcher, console) against Modal with the user's keys; `make cloud-bootstrap` applies Terraform and deploys with the user's GCP project; no path, hostname or file that exists only on the author's machine (Codex B10 is the checklist). Verified by a fresh clone into a new directory on the laptop. |
| Console scope (added ~08:10 UTC) | **The website is the way we interact with the pipeline.** It must show the entire database and every artifact in the bucket, and the complete history of everything that ever happened: every workflow, every trial including failed, unresolved and launch-failed ones, every candidate and gate reject, every scenario version. See §2.5. |

## 2. Target architecture on GCP

All in project `YOUR_PROJECT`, region `us-central1`. Terraform under `infra/` (no Kubernetes manifests; Cloud Run resources are Terraform). Nothing is applied by hand that Terraform can own; anything applied by hand is
recorded in `docs/ops.md` with the command.

### 2.1 Orchestration: Temporal Cloud
- Namespace `<name>.<account>` with API-key auth. Workers and clients read `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`,
  `TEMPORAL_API_KEY` (TLS on when the key is present; `describe_connection` logs only the auth mode).
- **Task queues** (replace the single `swesim` queue):
  - `swesim-authoring`: mine, judge, draft, approve, scenario-version, archive, register, score, export. LLM-bound and
    light on CPU; high activity concurrency (32+).
  - `swesim-build`: probe, compile, validate (Harbor/Modal-bound). Concurrency capped (8–12) so Modal and the image
    registry are not flooded; heartbeats every 30 s.
  - `swesim-trials`: launch, await, reconcile of trial Jobs. Concurrency high (Jobs run elsewhere); heartbeating polls.
  - `swesim-control`: workflow-side bookkeeping (index writes, cost accounting, notifications).
- Workflows are unchanged in shape (`CreateTasksWorkflow` → `CandidateWorkflow` children; `RunTasksWorkflow` →
  `TrialWorkflow` children). Each `execute_activity` names its queue. Existing REJECT_DUPLICATE / ALLOW_DUPLICATE_FAILED_ONLY
  semantics stay.
- Worker versioning: every activity result and every trial record carries `worker_image_digest` (from the pod's
  downward API / an env var set by the Deployment). A result names the code that produced it.

### 2.2 Compute: Cloud Run (worker pools, Jobs, one service)
- **Why Cloud Run and not GKE.** Cloud Run is GCP's serverless container runtime (the ECS/Fargate analogue); GKE is the
  EKS analogue. Everything this pipeline needs from a runtime is: long-running pull-based workers, an isolated container
  per trial with an idempotent "create once" primitive, an HTTPS service with Google login, mounted object storage, and
  native secrets. Cloud Run has all five without a cluster: **worker pools** for pull-based workers (no HTTP contract,
  instance-count scaling), **Jobs** for per-trial containers, **services** with IAP, **Cloud Storage volume mounts**
  (GCS FUSE), and Secret Manager references. GKE would buy per-queue autoscaling on Temporal backlog, which we would have
  to build anyway (custom metrics) and do not need tonight.
- **Workers:** one Cloud Run worker pool per task queue: `swesim-authoring`, `swesim-build`, `swesim-trials`,
  `swesim-control`. Same image, `SWESIM_TASK_QUEUE` and `MAX_CONCURRENT_ACTIVITIES` per pool, instance counts fixed per
  pool (authoring 2, build 2, trials 1–2, control 1), CPU always allocated, the `swesim-worker` service account. Scaling
  is a Terraform variable per pool; a later step can drive it from Temporal's task-queue backlog. If worker pools are
  not enabled in the project, the fallback is a Cloud Run **service** per queue with min=max instances, CPU always on,
  and a 1-line `/healthz` server in the worker (documented in `docs/ops.md`).
- **Trial launcher = Cloud Run Job** (`SWESIM_TRIAL_LAUNCHER=cloudrun`), implementing the existing contract
  (`launch_once` / `read_evidence` / `decide_launch`):
  - One Job resource per trial, name `swesim-trial-<id>` (Job names are unique per region: creating an existing name
    returns 409 AlreadyExists, which is the launch-once guard), `task_count: 1`, `parallelism: 1`, `max_retries: 0`,
    timeout ≥ solver timeout + 2 h, labels `swesim-trial-id`, `swesim-scenario-id`, `swesim-run-workflow-id`. After the
    Job is created it is executed once (`executions.create`); `launch.json` is written before the create call, so a lost
    acknowledgement is reconciled, never relaunched.
  - `read_evidence` reads the Job's executions (`running`/`succeeded`/`failed` counts, start and completion times) and the
    record files; `decide_launch` maps to launch / reconcile / completed exactly as the systemd version does.
  - The Job container runs the worker image with the same per-trial runner the systemd unit invokes today, against Modal,
    with provider keys, Modal token and the worker secret as Secret Manager references; the workspace bucket mounted at
    `/workspace`; 1 vCPU / 2 GiB (Harbor is a client; the heavy work is on Modal).
  - Housekeeping: a `swesim-control` activity deletes a trial's Job resource after its record is written and its execution
    has finished (logs stay in Cloud Logging; the record and `launch.json` are the evidence). This keeps the live Job count
    near the concurrent-trial count and inside the per-region Jobs quota.
  - Proof: a worker pool restart (`gcloud run worker-pools update … --no-traffic`-style re-deploy or instance recycle)
    during a running trial produces exactly one Job and one execution for that trial and the workflow completes (the
    Cloud Run analogue of the VM restart evidence); saved under `workspace/evidence/cloudrun/`.
- Cloud Logging: worker pool and Job logs; a log-based metric on `TrialWorkflow` reconcile events; alert on worker pool
  instance crash loops.

### 2.3 Storage: GCS record + Cloud SQL index
- Bucket `gs://swesim-<project>-workspace` (versioned, uniform access). Mounted at `/workspace` in the worker pools, the Jobs and the
  console as a **Cloud Run Cloud Storage volume** (GCS FUSE under the hood). The code keeps writing files; `SWESIM_WORKSPACE=/workspace`.
  - Known FUSE constraints to design around: no hard links, `os.replace` within one directory works (atomic_json is fine),
    `fcntl.flock` is not reliable across instances — the registry lock moves to a Postgres advisory lock (`pg_advisory_lock`)
    held by the `register` activity. Directory listings of thousands of trial dirs are slow: the console reads the
    index, not the bucket.
  - Import: the VM record is copied once with `gcloud storage rsync` (scenarios, tasks, trials without task copies,
    exports, candidates, evidence, registry.json, job essentials). VM-era trial records are labelled
    `origin: vm-systemd` in the index.
- Cloud SQL Postgres (`db-custom-2-7680`, private IP, reached through Cloud Run's Cloud SQL connection or the Python connector). Schema (v1):
  - `scenarios(id, version, spec_hash, source, source_ref, validation_status, compiled_hash, image_digest, difficulty_tier, created_at, registry_json)`
  - `candidates(instance_id, source, stage, status, reasons, judged_at, workflow_id)` — the creation funnel
  - `trials(trial_id, scenario_id, spec_hash, condition, brief_profile, model, run_config_hash, attempt_index, purpose, status, outcome_code, reward, success, started_at, finished_at, worker_image_digest, workflow_id, cost_usd, input_tokens, output_tokens, origin)`
  - `exchanges(trial_id, seq, role, exchange, ts, request_id, disclosed_fact_ids, regenerated, redacted_terms, text)` — evaluator-only columns flagged
  - `scores(scenario_id, spec_hash, condition, brief_profile, model, run_config_hash, n, success_count, pass_at_k, pass_pow_k, mean_reward, questions_asked, confirmations, computed_at)`
  - `workflows(workflow_id, type, status, params_summary, started_at, closed_at, cost_usd)`
  - `costs(trial_id or workflow_id, provider, model, input_tokens, output_tokens, usd, at)`
  - Written by the `swesim-control` activities at the end of each stage/trial (idempotent upserts keyed by ids); a
    `swesim index rebuild` command rebuilds every table from the files (the files stay the record of truth, the index is
    disposable).
- Snapshots: bucket versioning plus a daily Cloud SQL backup.

### 2.4 Secrets and identity
- Secret Manager: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `SWESIM_WORKER_SECRET`, `GH_TOKEN`
  (read-only), `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, `TEMPORAL_API_KEY`, `DB_PASSWORD`.
- Cloud Run mounts them as environment variables from Secret Manager references (`--set-secrets`); the worker pools,
  Jobs and console run as the `swesim-worker` service account with `secretmanager.secretAccessor`, `storage.objectAdmin`
  on the bucket, `cloudsql.client`, and `run.developer` scoped to creating/deleting trial Jobs.
- Never in images, manifests, Temporal payloads, logs, or the console. The laptop-copied `/etc/swesim/env` path is retired.

### 2.5 Console: the operator surface for the whole record

The website is how the pipeline is operated and how its history is read. Nothing an operator needs should require
`gcloud`, `kubectl`, Temporal's UI or the CLI. Concretely:

- **Deployment:** the same FastAPI app as a Cloud Run **service** behind an external HTTPS load balancer (serverless NEG)
  with a Google-managed certificate on `swesim.<ip>.nip.io` (or the user's domain) and **IAP enabled** with an allowlist
  of Google accounts; ingress restricted to the load balancer. The evaluator gate reads the IAP identity header
  (`X-Goog-Authenticated-User-Email`, verified through the IAP JWT) against an `EVALUATORS` allowlist; `?evaluator=1` is
  removed. Evaluator-only data (author-only facts, hidden test assets, oracle, redacted sentences, tool audit, judge and
  draft metadata) renders only for evaluator identities; the public timeline never carries `redacted_terms` or
  `withheld_sentences` (Codex B-P2-6).
- **Data sources:** Postgres for every list, filter, funnel, score table and trend; the bucket for artifacts. A page never
  globs the bucket to list things; it reads the index and links to the artifact.
- **History, complete:** a Workflows page listing every workflow ever started (create, run, candidate, trial children)
  with type, status including failed/cancelled/terminated with the failure message, params summary, start/close times,
  cost, and a link to the Temporal Cloud UI; a Trials page listing every trial record and every planned-but-unresolved or
  launch-failed attempt, filterable by scenario, version, model, condition, brief profile, status, outcome, origin
  (vm-systemd / cloudrun), date; a Candidates page with the creation funnel (mined → judged → probed → drafted → approved →
  compiled → validated → registered) and every reject with its gate and reason; a Scenarios page with every version and
  its archive, the control table, the difficulty label and the build hashes.
- **Database browser:** every table in §2.3 is browsable with filters, sorting, paging and CSV export; the schema page
  shows the index rebuild time and the last rebuild's diff against the files.
- **Artifact browser:** a tree over the bucket (scenarios, tasks, trials, exports, evidence, candidates, archive) with
  file sizes, sha256 where the manifest has one, text preview for JSON/diff/log/toml files and download; evaluator-only
  paths are gated; the per-trial task copy (session token) is never listed.
- **Actions, all from the UI:** create tasks (sources, band, models, approval mode, rebuild), run tasks (scenario
  selection with difficulty/tier filters, models, conditions, k, purpose, cap), approve/reject held drafts, cancel
  workflows, retry a failed candidate, label episodes (simulator validity), trigger an index rebuild; every action needs a
  confirmation step and a scheme+host+port Origin check (Codex B-P2-7), and is written to an `operator_actions` audit
  table with the IAP identity.
- **Live views:** trials by state and queue, worker pool health, spend by provider/model/run (live ledger), the creation
  funnel as it moves, episode pages with the agent/stakeholder conversation streaming as it happens.
- **Measurement views:** per-model accuracy tables by condition × brief profile (pass@k, pass^k, n; null when n is
  insufficient), trend charts (pass rate by model and condition as scenarios accrue; asked vs did not ask; questions and
  confirmations per trial; cost per solved task; difficulty distribution), model tier and difficulty labels everywhere a
  model or scenario is named. Charts follow the dataviz skill (one hue per model family, sequential for magnitudes, legend
  + table view, no dual axes).
- **Quality bar:** consistent layout, keyboard-navigable tables, dark and light, no page over ~1 s on the index, every
  number linked to the artifact it came from.

### 2.6 Build and deploy
- `Dockerfile.worker` (python 3.12, `uv sync --frozen`, Harbor + Modal clients, `swesim` installed). Cloud Build
  trigger on push to `main` builds `us-central1-docker.pkg.dev/<project>/swesim/worker:<sha>` and `:latest`; digest
  recorded in the build log and passed to `terraform apply` as the `image` variable.
- Deploy: `make deploy SHA=<sha>` = `terraform apply -var image=<digest>` (the four worker pools, the console service and
  the trial Job template are Terraform resources pinned to one image digest), then `scripts/cloud/verify_cloud.sh`. A
  GitHub Actions `workflow_dispatch` job with Workload Identity Federation does the same from CI (no long-lived keys).
  PR gating is not required (single contributor) but the offline suite runs on every push.
- Terraform modules: `network` (VPC, subnet, Private Service Access for Cloud SQL, Serverless VPC connector or Direct VPC
  egress), `registry`, `storage` (bucket, versioning, lifecycle), `sql` (instance, db, user, private IP), `secrets`
  (secret resources; values set out of band), `iam` (service account and roles), `run` (worker pools, console service,
  Job template defaults), `ingress` (static IP, serverless NEG, managed cert, IAP OAuth client + allowlist), `logging`
  (log-based metrics, alert policy on worker crash loops and reconcile > 1).

### 2.7 What is retired, what remains
- The VM keeps running until the new stack passes the smoke workflows; then its worker is stopped (`systemctl --user
  disable --now swesim-worker`) and the VM is documented as the fallback path in `docs/ops.md`. The Temporal dev server on
  the VM is not migrated (Temporal Cloud is a fresh namespace; workflow history restarts).
- Modal stays for sandboxes and the stakeholder service.

### 2.8 Temporal design: what exists and what the goal must change

Measured against `swesim/workflows/*.py` at `15bf683`:

**Already proper.** Two operator workflows (`CreateTasksWorkflow`, `RunTasksWorkflow`) fan out to one child per unit of
work (`CandidateWorkflow` per candidate with id `create:<sid>[@v<n>]`, `TrialWorkflow` per trial with id = trial id) under
explicit id-reuse policies; per-candidate activities (`judge_one`, `probe_candidate`, `draft`, `precheck_draft`,
`auto_approve`/`human_approve`, `verify_approval`, `compile`, `validate`, `register`, `scenario_version`, `archive`) and
per-trial activities (`plan_trials`, `launch_trial`, `await_trial`, `score`, `export`); every blocking stage runs in a thread
with heartbeats; `await_trial` is a heartbeating read-only poll with `maximum_attempts=3`; stage outputs are idempotent
(existing output is retained, write-then-rename); payloads carry paths and ids, not bodies; `launch.json` before the
launch call makes a lost acknowledgement reconcilable.

**Not yet proper — required in the goal:**
1. **One task queue** (`swesim`) carries LLM calls, Modal builds, launches and bookkeeping. Split into
   `swesim-authoring`, `swesim-build`, `swesim-trials`, `swesim-control` (§2.1); each `execute_activity` names its queue;
   one worker pool per queue with its own concurrency.
2. **Retry classification.** `act()` uses `maximum_attempts=1` for everything. Temporal's rule: retry network errors,
   timeouts, rate limits and transient unavailability; never retry invalid input or business rejections. Activities must
   raise `ApplicationError(non_retryable=True)` for gate rejections, schema failures and "candidate not kept", and let
   transient provider/Modal/GitHub errors retry with backoff (3–5 attempts, initial 10 s, max 2 min). Launch stays at one
   attempt (the launcher is idempotent by design, but a retry must go through `decide_launch`, never a blind relaunch).
3. **Coarse activities.** `mine_activity` for a GitHub repository does discovery *and* up to three Modal probe builds in
   one activity; split into `discover_activity` (GitHub only) and per-candidate `probe_candidate_activity` (already
   exists). `validate_activity` runs all seven control Harbor runs in one activity (10–25 min); split into one activity per
   control (`validate_control_activity`) plus an `evaluate_controls_activity`, so a single infrastructure failure retries one
   control, and the console can show per-control progress. The legacy whole-file `judge_activity` is used only by the
   legacy `PipelineWorkflow`; mark it legacy or delete it.
4. **Versioning discipline.** No `workflow.patched(...)` anywhere; workflow code changed while workflows could be running
   (rebuild mode, band parameter). Every change to a workflow's command sequence ships behind `workflow.patched("<id>")`
   until no pre-change executions remain; worker deploys check `temporal workflow list` for open executions of changed
   types first; the deploy step records the check in `docs/ops.md`.
5. **Search attributes.** Register `swesim_scenario_id`, `swesim_model`, `swesim_condition`, `swesim_status`,
   `swesim_version`, `swesim_purpose` (keyword) on the namespace and set them on every workflow start and on status changes
   (`upsert_search_attributes`), so the console and the Temporal UI can filter without reading files.
6. **Continue-as-new.** `CreateTasksWorkflow` over 109 + repository candidates and `RunTasksWorkflow` over hundreds of trials
   must continue-as-new every N children (N = 100) carrying the remaining plan, so histories stay far below the event
   limit.
7. **Cancellation.** Activities that run threads (`blocking()`) heartbeat but do not observe cancellation; add
   `activity.is_cancelled()` checks between stages and let the launcher cancel a Job on workflow cancellation.
8. **Payload hygiene.** `visible_view` and forbidden terms are sent to the stakeholder over HTTP, not through Temporal;
   keep it that way; assert every activity input/output is under 256 KB in tests.

## 3. Models

### 3.1 Pipeline roles (must be very well-aligned frontier models)
| Role | Primary | Fallback chain | Notes |
|---|---|---|---|
| Judge | `anthropic/claude-opus-5` | `openai/gpt-5.6-sol` → `openai/gpt-5.5` | Re-run judge calibration on the 145 annotated rows with the new judge and report agreement/precision/recall next to the gpt-5.5 numbers (0.655 / 0.644 / 0.460). |
| Drafter | `anthropic/claude-sonnet-5` | `openai/gpt-5.6-sol` | Must differ from the judge (existing rule). Sonnet 5 stays unless the user objects. |
| Stakeholder | `anthropic/claude-opus-5` | `openai/gpt-5.6-sol` → `anthropic/claude-sonnet-5` | Stakeholder v3 = v2 prompt + Opus 5. `RunConfig.stakeholder.model_id` changes, so v3 trials never pool with v2. |
| Withholding span selector | same as drafter | | |

The user named "GPT 5.6 Soul"; the id that exists is `gpt-5.6-sol`. If they meant it as primary for judge or
stakeholder, swap the primary and fallback above. Opus 5 is primary here because the Anthropic path (adaptive thinking,
effort, fallback on API rejection) is already exercised by our `llm.py`; the OpenAI path for a `5.6` model must be probed
once (`workspace/evidence/model-probe.json`) before it is used in a role.

### 3.2 Evaluated agents (solvers), in order of use
Tier A, older generation open models (cheap; establishes the floor and the "asks nothing" baseline):
`openrouter/deepseek/deepseek-chat-v3-0324`, `openrouter/z-ai/glm-4.5`, `openrouter/qwen/qwen3-coder`,
`openrouter/moonshotai/kimi-k2`, `openrouter/minimax/minimax-m2`, `openrouter/openai/gpt-oss-120b`.

Tier B, newer generation open models: `openrouter/deepseek/deepseek-v3.2`, `openrouter/deepseek/deepseek-v4-flash`,
`openrouter/deepseek/deepseek-v4-pro-0813`, `openrouter/z-ai/glm-5.2`, `openrouter/z-ai/glm-5.3`,
`openrouter/z-ai/glm-5.3-flash`, `openrouter/qwen/qwen3.6-plus`, `openrouter/moonshotai/kimi-k2.7-code`,
`openrouter/minimax/minimax-m3`.

Tier C, small closed models: `openai/gpt-5-mini`, `openai/gpt-5.6-luna`, `anthropic/claude-haiku-4-5-20251001`.

Tier D, mid frontier (gated on Tier A–C trends): `anthropic/claude-sonnet-5`, `openai/gpt-5.6-sol`.

Tier E, frontier (gated, k=1 first): `openai/gpt-6-astra`, `anthropic/claude-fable-5-1`, `anthropic/claude-opus-5`,
`openai/gpt-5.5`.

Gating rule for D and E: at least 15 valid scenarios with n ≥ 2 per arm on Tier A–C, the full > hidden ordering
holding on the majority of scenarios (§5), and the per-trial cost model (§6) predicting the tier fits the remaining cap.

### 3.3 OpenRouter support (required work)
- `run.py`: provider `openrouter` → host `openrouter.ai`; model ids `openrouter/<vendor>/<model>`; litellm inside
  mini-swe-agent uses the `openrouter/` prefix with `OPENROUTER_API_KEY`.
- Harbor's mini-swe-agent adapter forwards provider keys into the agent container; confirm it forwards
  `OPENROUTER_API_KEY` (if not, pass it through the agent env option or the task's `[agent.env]`, never through task
  files). One smoke trial (`openrouter/deepseek/deepseek-v3.2`, seaborn v2 hidden) before any sweep.
- `llm.py` does not need OpenRouter (pipeline roles are Anthropic/OpenAI), but the probe must record every solver id
  as reachable before the matrix is planned.
- Cost accounting: read `usage` from the agent trajectory (mini-swe-agent records per-step usage/cost) and OpenRouter's
  generation cost when present; store per trial in `costs`.

## 4. Sweep plan

1. **Creation, repositories first** (after the smoke workflows pass on the new stack): one `CreateTasksWorkflow` per
   repository in the bugfix band (`--pr-kind bugfix`, `--max-candidates 20`, since 2026-06-01) and one in the feature band
   (`--pr-kind feature`), for the ten repositories in §1; then one for `swebench:A` (109 ids, `--max-parallel 12`).
   Discovery is the product here: every repository gets a funnel row in the console (PRs listed → linked issue found →
   band filters → probe → judge → draft → gates → valid) with the reject histogram, so the bands and link tiers are tuned
   from data during the night (the first pypdf pass rejected 119 of 188 PRs for "no linked issue" and 36 for diff size).
   Target: at least 10 valid repository-sourced scenarios by 08:00 PDT. Auto approval; every valid scenario has its
   seven-control validation. If the ten repositories yield fewer than 10 valid scenarios by 05:00 PDT, add repositories
   from a generated list of active pytest-based Python projects (the console's Create page accepts any URL).
2. **Difficulty screen** (§5) on every valid scenario with one Tier A model: hidden k=2, full k=1.
3. **Tier A–C matrix**: full k=1, hidden k=2, interactive k=2 on every scenario that passed the screen; brief profile
   `withheld` where it exists.
4. **Trend check**: per model, pass rate by condition with n; asked-vs-not; the ordering full ≥ interactive ≥ hidden as
   a per-scenario indicator; report in the console and `docs/grading.md`.
5. **Tier D, then E**, only when the gate in §3.2 holds; k=1 first, then k=2 if the cap allows.
6. **Disclosure audit** on every interactive transcript (no test/verification/identifier disclosure; regenerated and
   redaction counts) and a reward-integrity sweep (§7).

Concurrency: build queue 8–12 validations in parallel (Modal), trial Jobs 30–50 in parallel, LLM concurrency per
provider respecting rate limits (OpenRouter 20 concurrent per key is a safe start).

## 5. Difficulty: measured, tiered, reported

The user's requirement: the trajectories we see and the tasks we create must be difficult enough, and the creation process must
find the right kind of tasks. Difficulty is a property we measure on every scenario and report per model tier; it is never
asserted from diff size (CLAUDE.md) and never manufactured by editing a task after a gate.

"Hard enough" has a definition here: a scenario is informative for a model tier when the hidden arm does not saturate
and the full arm is at least as good as hidden.

- **Screen:** after validation, run hidden k=2 and full k=1 with `openrouter/deepseek/deepseek-v3.2` (Tier A/B
  boundary, cheap). Label: `saturated` if hidden 2/2 and full 1/1; `informative` if hidden ≤ 1/2; `broken` if full 0/1
  (the brief plus facts still fails: suspect the task, look at the report). Labels live in `scenarios.difficulty_tier`
  and on the scenario page. Frontier runs skip `saturated` scenarios by default.
- **Sources of difficulty we can pull:** the feature band (200–1,500 diff lines, ≥ 8 comments), contracts with more
  than one FAIL_TO_PASS test, withheld briefs with several removed spans, on-request facts the tests depend on. The
  creation funnel reports how many valid scenarios came from each band.
- **Reporting:** the distribution of hidden pass rates per scenario for the screen model; the share of `saturated`
  scenarios; pass rate by tier. The existing v2 scenarios (seaborn, sklearn) are expected to be `saturated` for Sonnet
  5 and Haiku 4.5 and are kept as mechanism demonstrations, not as difficulty evidence.
- **Not done:** hand-tuning tests to be harder. Difficulty comes from selection and measurement, never from editing a
  scenario after a gate.

## 6. Spend model and caps

Per-trial cost is estimated from measured token usage of the first trials per model and then tracked live:
- Tier A/B open models: ~0.3–1.5 M input tokens per trial (context re-sent per step) → roughly $0.1–1.0 per trial.
- Tier C: $0.3–2. Tier D: $2–8. Tier E: $5–25 (Opus 5 at $5/$25 per M; `gpt-6-astra` pricing unknown — measure one trial).
- Validation: seven Harbor runs on Modal per scenario ≈ $0.5–2 in Modal compute, plus the drafter/judge tokens.
- Planning numbers: 40 valid scenarios × (screen 3 + Tier A–C 18 models × 5 trials) ≈ 3,700 cheap trials is too many
  for the cap and the night; the goal therefore fixes **Tier A–C at 6 models** chosen across generations (older DeepSeek
  `chat-v3-0324`, newer DeepSeek `v3.2` and `v4-flash`, older GLM `4.5`, newer GLM `5.3`, plus `gpt-5-mini`) for the
  full matrix, and runs the remaining Tier A–C models on a 10-scenario subset. Frontier: k=1 on the informative subset.
- Hard cap: the console shows cumulative spend per provider; the goal's cap of about $1,500 is enforced by the operator
  (the user) and by a `SWESIM_SPEND_CAP_USD` check in `RunTasksWorkflow` that stops planning new trials when the recorded
  spend exceeds it.

## 7. Grading integrity on the sweep

- Every registered scenario: the seven executed controls (`nop`, `oracle`, plausible-wrong, three tamper controls,
  reachability probe) with their reward rows in `validation.json`.
- Sweep-time signals: `integrity_signal_count` distribution per model; protected-path edits (`dropped_paths`); trials
  with JUnit/terminal/exit disagreement (outcome 3); reward 1.0 with suspiciously small patches versus the oracle; any
  submission touching `conftest.py`/`pytest.ini`/`pyproject.toml`. Reported per model in the console and in
  `docs/grading.md`; any model whose trials show integrity signals gets those trials listed.
- Part A of `docs/reviews/PROMPT-pipeline-review.md` executed by a fresh reviewer agent (fable) against the deployed
  build; P1 findings fixed or documented with the reason.
- Disclosure audit: for every interactive transcript in the sweep, the reply filter's `regenerated`/`redacted_terms`,
  plus a reading pass (LLM-assisted, Opus 5, evaluator-only) that flags replies naming a test, a verification step, an
  identifier outside the brief, or a code-path choice; counts by model in the report.

## 7.1 Independent review (Codex, 2026-09-13 07:00 UTC, snapshot at a76654a) — dispositions

Reports: `/private/tmp/swesim-independent-review/part-a.md`, `part-b.md` (copy both into `docs/reviews/2026-09-13-codex/`
in the repository, unchanged). Dispositions below are binding for the goal.

| Finding | Disposition | When |
|---|---|---|
| A-P1-1 in-process falsification (pytest runs as `tester` with the submitted roots loaded; the parent trusts its outputs) | Acknowledged residual, already documented in `docs/grading.md` and CLAUDE.md; add the explicit statement Codex asks for and the list of what the consistency checks do and do not authenticate. No code change tonight. | docs, before sweep |
| A-P1-2 demoted fact still disclosed through requirement text; deny-list exempts identifiers present in requirements; sanitizer skips `repo-derivable`/`volunteered-at-review` | **Fix before the sweep:** run `unsafe_fact_text` on every stakeholder-visible discovery and on every requirement text; a requirement whose fact was demoted is rewritten from the thread or dropped with the scenario failing the gate; forbidden terms are computed from the hidden assets minus the *natural issue* only (not minus requirements). Tests for each. | code, before sweep |
| A-P1-3 `tier` and `patience_remaining` returned to the agent | **Ruling:** remaining patience is the availability signal the design intends and stays public; `tier` is removed from public responses; docs updated to say exactly that. | code, before sweep |
| A-P1-4 reporter stance partly prompt-enforced; `confirm` accepts any text incl. code | **Mitigate:** `confirm-behavior` rejects inputs that look like code (fenced blocks, `def `/`class `/`import `, braces-heavy lines, diff headers) with a 422 and a hint; the filter also rejects replies that name any identifier from the hidden assets regardless of the request's option letters; docs qualify the remaining guarantees as prompt rules with the audit as the evidence. | code + docs, before sweep |
| B-P1-1 planned vs executed trial identity | Fixed (`ec16791`), confirmed by Codex's late check. | done |
| B-P1-2 a repeated selected phrase removes several sentences but records one; requirements with `fact_id=None` are ungrounded | **Fix before the sweep:** every expanded occurrence becomes its own ledger removal (or the selection is rejected when it matches more than one sentence and the model did not select each); `_integrity` verifies that the kept text equals the natural text minus exactly the recorded excerpts; every requirement must carry a grounded `fact_id` or a verbatim thread excerpt. | code, before sweep |
| B-P2-1 probe reuse binding attaches to old cached executions; probe runs as root while grading runs as `tester` | **Fix before creation:** Harbor probe job names include the probe binding hash; reuse only when the job's recorded binding equals the current one; probe runs under the grading identity. | code, before sweep |
| B-P2-2 fallback plausible-wrong control can invert inside a docstring and keep gold behaviour | Fix the lexical mask (carry open-string state across hunks) and require the fallback control to differ from gold in executable AST; reject `unfetched:` for fallback controls; append-only control notes. | code, during the night |
| B-P2-3 `compiled_hash` omits compiler constants and render inputs (stakeholder URL, hosts, timeouts, review config) | Include a canonical dump of the compile configuration and a manifest of rendered files in the hash; run verifies the manifest before use. | code, during the night |
| B-P2-4 base manifests written but never compared | Verifier compares the agent image's base manifest to its own at collection and fails closed on mismatch; a test covers it. | code, during the night |
| B-P2-5 legacy scaffold and non-scaffold trials pool | Score grouping derives the scaffold identity from `report.agent_config_sha256` when `run_config.agent_config` is absent; legacy trials regroup; docs updated. | code, before sweep |
| B-P2-6 console evaluator gate leaks `redacted_terms`/`withheld_sentences`; raw judge/draft metadata served | Fixed as part of §2.5 (IAP identity gate, strip list extended, gated file routes). | code, before console cutover |
| B-P2-7 create/run without confirmation; Origin check hostname-only | Fixed as part of §2.5 (confirmation step, scheme+host+port, audit table). | code, before console cutover |
| B-P2-8 calibration metric definition and missing per-instance predictions | Save `per_instance` predictions with the calibration; define the positive as `verified_rubric_score >= 2` in the docs; recompute with the new judge. | code + docs, during the night |
| B-P2-9 stale README numbers; laptop-off evidence lacks a session log; nine non-interactive exports training-eligible | README numbers regenerated from the index at the end; laptop-off labelled operator-reported unless the VM auth log is captured; training-eligible statement corrected. | docs |
| B-P3-1 README offline command includes the integration test | Fix the command. | docs |
| A-P3-1 diagnostic rerun budget and forged-output prose | Document the separate diagnostic budget; qualify the example. | docs |

## 8. Work breakdown (streams, interfaces, order)

Streams run in parallel worktrees on `fable`; the integrator merges. Order within the night:

1. **infra** — Terraform (`infra/`), apply, outputs (bucket, SQL connection name, static IP, IAP client, service
   account). ~1.5 h. Interfaces: `infra/outputs.json` consumed by `scripts/cloud/*.sh`.
2. **image+deploy** — `Dockerfile.worker`, Cloud Build trigger, the `run` Terraform module (four worker pools, console
   service, Job template), `make deploy`. ~1 h. Depends on 1 for the registry and service account.
3. **launcher+queues** — `swesim/workflows/launchers/cloudrun.py` (Cloud Run Jobs API contract + Job deletion after
   record), queue routing in workflows/activities, `SWESIM_TASK_QUEUE`, per-queue concurrency, `worker_image_digest` in
   records; unit tests with a fake API; the worker-pool restart proof after 2. ~2 h.
4. **storage+index** — FUSE mount config, Postgres schema + `swesim index` (rebuild, upsert activities), advisory lock
   for the registry, VM import script `scripts/cloud/import_vm.sh`. ~2 h.
5. **console** — everything in §2.5: IAP identity gate, history pages (workflows incl. failed, trials incl. unresolved,
   candidates funnel, scenario versions), database browser with CSV export, artifact browser over the bucket with gating,
   all actions with confirmation + audit, live views, measurement views. ~3 h, after 4's schema is frozen (schema first,
   data later).
6. **review-fixes** — the before-sweep rows of §7.1 (A-P1-2, A-P1-3, A-P1-4, B-P1-2, B-P2-1, B-P2-5) with tests. ~2 h. Independent of 1–5; starts immediately.
7. **temporal-design** — §2.8 items 1–8 (queues, retry classification, split mine/validate, `patched`, search attributes, continue-as-new, cancellation, payload tests). ~2 h. Overlaps with 3.
8. **models** — OpenRouter provider in the solver path and probe; judge/stakeholder switch to Opus 5 with fallbacks;
   judge recalibration; per-trial cost capture. ~1.5 h. Independent of 1–5; can start immediately.
9. **smoke on the new stack** — `CreateTasksWorkflow` on one swebench id and `RunTasksWorkflow` on one scenario
   completing end to end; IAP login works; index rows appear. Gate for the sweep.
10. **sweep** — creation, screen, Tier A–C matrix, trend check, gated frontier; disclosure audit; integrity report.
11. **review+docs** — Part A review; README/architecture/ops/grading/hosting spec; `scripts/verify_cloud.sh`; evidence.

Fallback if 1–3 slip past ~04:00 PDT: keep the VM path for the sweep (documented as such), finish the stack, and cut
over the console only. The goal says infra first, so the sweep does not start on the VM unless the user changes the
priority.

## 9. Acceptance and evidence

- `terraform apply` idempotent (second apply: no changes); `gcloud run worker-pools list` shows the four pools ready
  and `gcloud run services describe swesim-console` shows the IAP-protected service; `scripts/verify_cloud.sh` checks: Temporal Cloud namespace reachable with the API key (auth mode logged,
  never the key), IAP redirect (302 to accounts.google.com) on the console hostname, the bucket contains
  `registry.json` and every scenario in it, Postgres `scenarios` and `trials` counts match the files, one Job and one
  execution per trial id (no duplicates), Cloud Build's latest image digest equals the digest the worker pools run and the
  digest in the newest trial records.
- `scripts/verify.sh 10` still passes on a laptop mirror (`scripts/cloud/pull.sh` from the bucket replaces the VM pull).
- Evidence saved under `workspace/evidence/cloudrun/` (restart proof), `workspace/evidence/cloud/` (verify_cloud output,
  IAP curl headers, Terraform plan summary), `workspace/evidence/sweep/` (spend ledger, screen labels, disclosure
  audit, integrity report), all mirrored into the bucket.
- Docs: `README.md` (hosted topology, one-command deploy, what a stranger needs), `docs/architecture.md` (Cloud Run launcher,
  queues, index), `docs/ops.md` (Terraform/kustomize procedures, IAP, secrets rotation, restart proof, VM fallback),
  `docs/grading.md` (difficulty method, tiered results, integrity report, disclosure audit),
  `docs/superpowers/specs/2026-09-13-gcp-hosting-design.md` updated to "as built".

## 9.1 Reusable by a stranger (acceptance)

- `git clone` into a fresh directory; `cp .env.example ~/Documents/.env` (or any path via `SWESIM_ENV_FILE`) and fill:
  provider keys (Anthropic, OpenAI, OpenRouter), `SWESIM_WORKER_SECRET`, `GH_TOKEN` (read-only), Modal token, GCP project and
  region, Temporal Cloud values (optional for local mode). Every variable documented in the file.
- `make check`: `uv sync --extra dev --frozen` + the offline suite + `verify.sh` levels that need no cloud.
- `make dev`: `temporal server start-dev`, the worker with `SWESIM_TRIAL_LAUNCHER=local`, the console on 127.0.0.1; a
  documented smoke `swesim create --source swebench:A:<id>` and `swesim run-tasks` that reach Modal with the user's keys.
- `make cloud-bootstrap PROJECT=<id>`: Terraform init/apply, Cloud Build image, deploy, `verify_cloud.sh`; `make cloud-destroy`
  tears it down. Secrets are read from the env file into Secret Manager by a script that never echoes values.
- `README.md` Quickstart is exactly these steps; the fresh-clone run is recorded (commands and outcomes) in `docs/ops.md`.

## 10. Rules that do not bend

- Task creation and graded runs only through Temporal (workflows or the console). Stage commands stay debug-only.
- No credentials in git, images, manifests, payloads, logs, console responses, or this brief. Keys live in Secret Manager
  and `~/Documents/.env`; python-dotenv reads them; never `export $(...)`.
- No fabricated numbers, no hand-edited artifacts, no metrics on insufficient samples (null, with n shown).
- Every result names its code (commit, image digest) and its build (compiled_hash, image digest of the task).
- VM-era and v1/v2 stakeholder records are labelled, never pooled with v3 or GKE-era runs.
- Subagents: code, design and review on `fable`; recon on `opus`/`sonnet`; never `haiku`.
- Anything a human must do (Temporal Cloud values, IAP consent screen, DNS) is written to `docs/ops.md` with the exact
  step and the work continues on everything else.

## 11. Known risks and how they are handled

| Risk | Handling |
|---|---|
| Temporal Cloud values arrive late | Streams 1–6 do not need them; the smoke (7) does. If they are not there by ~03:00 PDT, run the smoke against the VM dev server through a port-forward from the cluster (documented as interim), swap the env when the values arrive. |
| IAP needs an OAuth consent screen / organization | The account is a Google Workspace account; if brand creation fails, fall back to Identity Platform-less basic auth behind the load balancer for the night and document it. |
| Cloud Run worker pools not enabled in the project, or Jobs quota (per region) too low for the sweep | Worker pools: fall back to services with min=max instances and a `/healthz` endpoint. Jobs: delete each trial's Job after its record is written; request a quota raise early in the night. |
| GCS FUSE semantics (locks, listing) via Cloud Run volume mounts | Advisory locks in Postgres; console reads the index; `atomic_json` unchanged; the mount is read-write on workers and Jobs, read-only on the console. |
| Harbor does not forward `OPENROUTER_API_KEY` | Pass through the agent env option or `[agent.env]`; smoke trial before any sweep. |
| OpenRouter rate limits and provider routing variance | Pin providers where OpenRouter allows (`provider.order`) and record the served provider from the response; cap concurrency at 20. |
| GitHub API rate limit during discovery of ten repos | Sequential discovery per repo with the mine activity's existing pagination bound; spread over the authoring queue. |
| Cost overrun | Spend ledger per run; `SWESIM_SPEND_CAP_USD`; frontier tiers gated. |
| Presentation at 16:00 PDT the same day | Everything reported is measured; the fallback path (VM) stays documented and runnable. |

## 12. Goal command (verbatim)

The `/goal` text the user runs; it points here and this brief is binding where they differ.

```
Ship swesim as a hosted production pipeline on GCP (Cloud Run, Temporal Cloud) and use it to create tasks from new repositories given as links and run the tiered multi-model sweep on them, by 2026-09-13 08:00 PDT. The brief docs/superpowers/specs/2026-09-13-hosted-pipeline-brief.md is binding on every detail; where this text and the brief differ, the brief wins. Infra first; the sweep runs only on the new stack; the website is the way the pipeline is operated.

Infrastructure (brief 2.1-2.7, Terraform in infra/, second apply clean): Temporal Cloud for every client and worker; one worker image built by Cloud Build, digest pinned in Terraform and recorded in every result; four Cloud Run worker pools, one per task queue; one Cloud Run Job per trial named by trial id, deleted after its record is written, launch-once proven by a worker-pool restart during a running trial with saved evidence; workspace on a versioned GCS bucket mounted everywhere, VM record imported and labelled; Cloud SQL Postgres index written by the workflows, rebuildable from the files; secrets only from Secret Manager; console as a Cloud Run service behind an HTTPS load balancer with IAP and an IAP-bound evaluator gate; one CreateTasksWorkflow and one RunTasksWorkflow completing end to end on the new stack before any sweep; VM worker then stopped and documented as fallback.

Temporal design (brief 2.8, all eight items): per-queue routing; retry classification (transient retries, gate rejections non-retryable, no blind relaunch); mine split into discover plus per-candidate probe, validate split per control; workflow.patched with an open-executions check before deploys; search attributes; continue-as-new; cancellation observed; payload sizes asserted.

Independent review (brief 7.1): Codex reports in docs/reviews/2026-09-13-codex/; before-sweep rows fixed with tests (A-P1-2, A-P1-3, A-P1-4, B-P1-2, B-P2-1, B-P2-5, B-P2-6, B-P2-7); the rest as the table says. No reward hacking: brief 7 in full.

Models (brief 3): judge and stakeholder anthropic/claude-opus-5 with openai/gpt-5.6-sol fallback, drafter anthropic/claude-sonnet-5; judge calibration re-run with per-instance predictions; OpenRouter in the solver path with a smoke trial first; per-trial cost captured.

Sweep (brief 4-6, Temporal only): repositories first, the ten in the brief in both bands with a per-repository discovery funnel in the console and at least 10 valid repository-sourced scenarios targeted, then swebench:A; seven-control validation and the difficulty screen on every valid scenario; Tier A-C matrix on the six matrix models, remaining Tier A-C on a 10-scenario subset; Tier D and E (claude-sonnet-5, gpt-5.6-sol; gpt-6-astra, claude-fable-5-1, claude-opus-5, gpt-5.5) only when the brief 3.2 gate holds, k=1 first; spend tracked per run and capped by SWESIM_SPEND_CAP_USD at about $1,500.

Reusable (brief 9.1): .env.example, make check, make dev (local stack), make cloud-bootstrap; a fresh clone runs them with only the env file; no author-machine paths.

Console (brief 2.5, all of it): complete history of every workflow, trial (including failed, unresolved, launch-failed), candidate, gate reject and scenario version; database and bucket artifact browsers with evaluator gating; every operator action from the UI with confirmation, Origin check and audit; live views; accuracy tables with null on insufficient n; trend charts; difficulty and tier labels.

Done also requires: scripts/verify_cloud.sh passing (Temporal Cloud auth mode, IAP redirect, bucket vs registry, Postgres vs files, one Job and execution per trial, image digest consistency); scripts/verify.sh 10 on a bucket mirror; offline suite green; README, architecture, ops, grading and the hosting spec describing measured hosted behaviour with dates, ids and evidence paths; disclosure audit and integrity-signal report per model; every commit pushed to origin main; no fabricated numbers, hand-edited artifacts or credentials; human-only steps written to docs/ops.md while all other work continues.
```
