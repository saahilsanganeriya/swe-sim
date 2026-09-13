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

### 2.5 Console
- Same FastAPI app as a Cloud Run **service** behind an external HTTPS load balancer (serverless NEG) with a
  Google-managed certificate on `swesim.<ip>.nip.io` (or the user's domain) and **IAP enabled** with an allowlist of Google
  accounts; ingress restricted to the load balancer.
- The evaluator gate reads the IAP identity header (`X-Goog-Authenticated-User-Email`, verified through the IAP JWT)
  and an `EVALUATORS` allowlist; `?evaluator=1` is removed.
- Reads: Postgres for lists, funnels, score tables and trends; the bucket for artifacts (transcripts, patches, exports).
  Writes (create/run/approve): through the Temporal client with the same confirmation + Origin checks as today.
- Live views: creation funnel (mined → judged → probed → drafted → approved → compiled → validated → registered, with
  gate-reject reasons), trials by state and queue, spend by provider/model/run, per-model accuracy tables by condition ×
  brief profile (pass@k, pass^k, n; null when n is insufficient), trend charts (pass rate by model and condition as
  scenarios accrue; asked vs did not ask; questions and confirmations per trial; cost per solved task), episode pages.
  Charts follow the dataviz skill (one hue per model family, sequential for magnitudes, legend + table view, no dual axes).

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

1. **Creation** (after the smoke workflows pass on the new stack): one `CreateTasksWorkflow` for `swebench:A` (109 ids,
   `--max-parallel 12`) and one per repository (`--pr-kind bugfix`, `--max-candidates 20`, since 2026-06-01). Also one
   feature-band pass over the same ten repositories (`--pr-kind feature`) because larger diffs are the main lever on
   difficulty. Auto approval; every valid scenario has its seven-control validation.
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
5. **console** — IAP identity gate, Postgres-backed lists and score tables, funnel, spend, trend charts, model tier
   labels, difficulty labels. ~2 h, after 4's schema is frozen (schema first, data later).
6. **models** — OpenRouter provider in the solver path and probe; judge/stakeholder switch to Opus 5 with fallbacks;
   judge recalibration; per-trial cost capture. ~1.5 h. Independent of 1–5; can start immediately.
7. **smoke on the new stack** — `CreateTasksWorkflow` on one swebench id and `RunTasksWorkflow` on one scenario
   completing end to end; IAP login works; index rows appear. Gate for the sweep.
8. **sweep** — creation, screen, Tier A–C matrix, trend check, gated frontier; disclosure audit; integrity report.
9. **review+docs** — Part A review; README/architecture/ops/grading/hosting spec; `scripts/verify_cloud.sh`; evidence.

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
