# Hosted pipeline runbook

Use this after [setup](setup.md). Commands run from the checkout with a filled ignored dotenv file. Replace uppercase placeholders and use your configured project and region. The authoritative record is the bucket; never repair a result by editing its reward, report, spec or launch files.

Current measured results and outstanding acceptance checks are in [ops.md](ops.md). The hosted smoke started on 2026-09-13 (two `CreateTasksWorkflow` executions; see [Hosted smoke](ops.md#hosted-smoke-2026-09-13)); the goal is a running pipeline to iterate on task generation, reward and tuning. Polish that was cut is listed in [Deferred from the 2026-09-13 finishing run](ops.md#deferred-from-the-2026-09-13-finishing-run).

## Deploy a commit

```bash
make check
uv run --frozen swesim workflows open-types
make deploy PROJECT=YOUR_PROJECT REGION=us-central1 SHA=COMMIT_SHA SWESIM_ENV_FILE=./.env
```

The deployment guard (`make open-executions`) writes the open workflow types to `workspace/evidence/cloud/open-types-<UTC>.txt` and stops before building if any are open. It needs the env file in its environment: a guard that reports `127.0.0.1:7233 … Connection refused` never reached the Cloud namespace (measured in `deploy-1976af3.log`; the report file is then empty, not "no open workflows"). Finish or cancel the open executions, or override:

```bash
make deploy PROJECT=YOUR_PROJECT REGION=us-central1 SHA=COMMIT_SHA SWESIM_ENV_FILE=./.env OVERRIDE_OPEN_EXECUTIONS=1
```

`OVERRIDE_OPEN_EXECUTIONS=1` is legitimate only when every changed workflow command sequence between the running image's commit and `COMMIT_SHA` is behind a `workflow.patched` marker (the old branch retained while matching executions remain open) and the open executions' remaining commands are unchanged in shape. Write that reasoning and the cost if wrong into ops.md; the Makefile appends `# Explicit operator override: OVERRIDE_OPEN_EXECUTIONS=1` to the guard's report. If a command sequence changed without a patch marker, the open execution fails with a non-determinism error at its next workflow task and must be re-run as a new execution; cancel it first instead of overriding. The GitHub deploy workflow has the same switch (`override_open_executions`).

Deployment then builds the selected Git commit with Cloud Build, pins its digest in the ignored `infra/image.auto.tfvars.json`, applies Terraform with `FORCE_PIN=1`, publishes the applied pin, executes the index-maintenance Job, then verifies the hosted runtime. A failed migration or verification stops the target. Keep the build id, commit, image digest, Terraform apply summary, migration execution id and verification path together (the `609f923` row in [ops.md](ops.md#hosted-smoke-2026-09-13) is the shape).

The image pin is one authoritative digest. `make apply` runs `scripts/cloud/pin_guard.py check` first and refuses when the pin file disagrees with what the pools and the console run: `pin-missing` (fresh clone; a plain apply would roll the workloads to `:latest`), `mismatch` (stale laptop file after a GitHub deploy, or an unapplied build), `mixed` (half-rolled stack) or `unknown` (gcloud failed, which is not "no workloads"). Each refusal prints its fix. Before a plain `make plan`/`make apply` on a laptop after a GitHub deploy, fetch the published pin:

```bash
make pull-pin PROJECT=YOUR_PROJECT
```

It copies `gs://YOUR_PROJECT-swesim-tfstate/image.auto.tfvars.json` into the pin file, or exits 1 when no deploy has published one yet. `FORCE_PIN=1` turns a refusal into a recorded `forced` pass; `make deploy` and `make cloud-bootstrap` set it for the apply that follows their own `pin-image` because rolling is their purpose. Do not pass `FORCE_PIN=1` to a plain `make apply` unless you intend to roll the workloads to whatever the pin file names. `write-pin` reads the applied image back from `terraform output -raw image` (never from a build log) and `push-pin` publishes it, so a laptop and CI agree on one digest.

The GitHub deployment workflow uses WIF and the deployer service account. It is restricted to runtime resources; IAM, storage and SQL changes need an operator's reviewed Terraform apply. `make gh-variables` sets the repository variables from Terraform outputs.

A successful deploy does not replace a hosted smoke test. Check the exact new image in the worker pools, console and next trial record.

## Inspect work and spend

Use the console's Workflows, Candidates, Trials, Database and artifact pages. The CLI equivalents for workflow state are:

```bash
uv run --frozen swesim workflows list all
uv run --frozen swesim workflows progress WORKFLOW_ID
uv run --frozen swesim workflows cancel WORKFLOW_ID
```

`swesim workflows progress WORKFLOW_ID` returns the workflow's `status`, `stopped_reason` and (for a refused frontier run) `admission`. The console's Workflows page does not show `stopped_reason` yet (deferred list). Outcome vocabulary:

| Value | Where | Meaning |
|---|---|---|
| `complete` | `RunTasksWorkflow`, `CreateTasksWorkflow`, per-scenario rows | Run: every requested scenario reached `complete`. Create: at least one candidate registered as `valid`. |
| `complete-with-failures` | `RunTasksWorkflow`, `CreateTasksWorkflow` | Run: some scenario was skipped, blocked or errored, or no scenario was planned. Create: no candidate became valid. Read the per-scenario/per-candidate rows; a legitimate rejection is not a service failure. |
| `complete-no-trials` | per-scenario row (legacy `PipelineWorkflow`) | The scenario registered but the probe found the requested solver unavailable; nothing may substitute it, so no trial was launched (`solver_unavailable`, `needs_human`). |
| `blocked` | `RunTasksWorkflow` | The frontier gate refused before any probe or plan; `stopped_reason` is `frontier-gate` and `admission.reasons` says why. |
| `skipped` with `reason: spend-cap` | per-scenario row | The spend check found recorded spend over the cap before this scenario was planned. |
| `withholding-leak` | approval gate name (`CandidateWorkflow`) | Not a run status: the leak gate rejected a draft; the workflow re-drafts once, then takes the next candidate. Appears in candidate receipts and approval evidence, never as `stopped_reason`. |

`stopped_reason` values: `spend-cap` (recorded spend crossed the cap in force; no new trials are planned, in-flight trials finish), `frontier-gate` (Tier D/E admission refused; see below), `model-unavailable` (the pre-run probe could not reach a required role model; `probe.needs_human` lists what needs a person). `null` means the run stopped for none of these. Trial-level statuses (`duplicate-rejected`, `unresolved`, `launch-failed`) are on the trial rows, not the run.

Use the console's spend view and cost rows for recorded provider usage. Unknown usage or price remains unknown, not zero. `SWESIM_SPEND_CAP_USD` stops planning new trials once recorded spend crosses the cap; it is not an external billing limit and does not retroactively cancel work already in flight. A run's own `spend_cap_usd` lowers the cap for that run only.

Keep `purpose=screen`, `comparison`, `calibration` and `demonstration` separate. A screen checks hidden/full success before a larger comparison. Frontier tiers require the brief's gate; no requested model is silently substituted.

### Frontier gate (Tier D/E solvers)

A `RunTasksWorkflow` that names a Tier D or E model (`swesim/roles.py` `TIERS`) runs `frontier_gate_activity` before probing: the gate writes a durable decision to `evidence/sweep/frontier-<identity>.json` (identity = SHA256 of models, conditions, k, purpose, brief profile, `include_saturated` and the ordered target scenario ids) and every later planning step re-reads that file and refuses a plan it does not admit. A refused run returns `status: blocked`, `stopped_reason: frontier-gate` and `admission`, whose `reasons` are the exact conditions not met, verbatim from `swesim/stages/frontier.py`:

- "Requires 15 current audited scenarios with at least two outcomes per arm on every core Tier A–C baseline." — fewer than 15 qualified scenarios (valid, audited, informative or saturated screen, n ≥ 2 per arm for each of the six core baselines from `purpose=comparison`, current spec and build, `origin=cloudrun`).
- "Full must exceed hidden on a majority of the qualified scenarios." — the pooled full-over-hidden ordering does not hold.
- "No eligible target scenarios remain." — every requested scenario was excluded; `admission.excluded_scenarios` names each with its reason ("Requires valid controls, task-sense audit and a completed difficulty screen." or "Saturated scenarios are excluded by default.").
- "<model> requires a completed k=1 trial before increasing repeats." — Tier E starts at k=1.
- "A positive spend cap is required for frontier runs." / "Spend accounting is incomplete; unknown usage cannot be treated as zero." / "A forecast requires measured baseline token usage." / "No verified price for <model>." / "Projected spend exceeds the remaining cap." — the forecast (largest observed baseline token counts × 1.25 × trials) plus known spend must fit under the lower of the run's `spend_cap_usd` and `SWESIM_SPEND_CAP_USD`; `admission.forecast_usd` and `forecast_basis` show the arithmetic.

Saturated scenarios are excluded unless the request carries `include_saturated: true`; the decision binds that flag, so a decision made without it never admits a plan that includes a saturated scenario. Neither `swesim run-tasks` nor the console's Run form exposes the flag yet (deferred list); until they do, the only way to include a saturated scenario is to start the workflow with `include_saturated` in its request through `swesim.workflows.client.start_run`, which still runs the gate. Tier A–C requests carry no admission and are never gated. The gate never launches a trial and never substitutes a model.

## Diagnose a missing or stuck trial

Start with its Temporal history and `trials/TRIAL_ID/{request,launch,status,record,runner-error}.json`. Inspect the corresponding Cloud Run Job and Cloud Logging:

```bash
gcloud run jobs describe swesim-trial-TRIAL_ID --project=YOUR_PROJECT --region=us-central1
gcloud run jobs executions list --job=swesim-trial-TRIAL_ID --project=YOUR_PROJECT --region=us-central1
```

| Evidence | Response |
|---|---|
| Launch intent exists; acknowledgement missing | Reconcile the named Job/execution and Harbor evidence. Do not issue another execute call. |
| Job is running; worker pool restarted | Restore the pool and let the existing workflow resume polling. |
| Job finished; record missing | Inspect execution logs, `runner-error.json`, Harbor artifacts and storage/SQL permissions. |
| No Job after recorded cleanup | Inspect the persisted record and launch evidence; absence after cleanup is expected. |
| API query fails | Runtime state is unknown. Retry the query; do not interpret the error as absence. |
| Trial is unresolved | Preserve evidence, determine whether a runtime still exists, and record the resolution before a new attempt. |

A retry of a completed or failed experiment is a new attempt index, with the original purpose and lineage recorded. Do not delete a Job or launch file to make a used trial id reusable.

For worker faults, Cloud Logging's resource type is `cloud_run_worker_pool`, with `resource.labels.worker_pool_name`. Missing pollers can reveal a crash loop even when Cloud Run reports Ready. Check Temporal API-key/namespace values, provider quotas, required secret versions, image reference, service account and Cloud SQL connection.

## Discovery reports every GitHub token exhausted

A `discover_activity` whose heartbeat details say `waiting_for: github-rate-limit`, or a worker log line `gh api ...: every token is out` followed by `every GitHub token exhausted (GH_TOKEN, app:<installation id>)`, means each configured quota bucket is spent (or paused by a secondary limit) and the scan is waiting inside its attempt for the soonest reset named in the message; nothing is lost and no operator action is required, because the scan resumes at the refused page or pull request after the wait (`docs/superpowers/specs/2026-09-13-interfaces.md`, discovery). To check the buckets yourself run `gh api rate_limit` under each credential (`GH_TOKEN=... gh api rate_limit` for a personal token; the App's installation token is minted by the worker and lives about an hour); GitHub does not count that call. If this happens on every scan, add a bucket rather than waiting: install the GitHub App (setup guide, "Optional GitHub App") if it is not configured, or on the laptop add `GH_TOKEN_2`, `GH_TOKEN_3` to the env file, then `make push-secrets` and `make tfvars` + `make apply` for the hosted pools. A worker log line `no token from app:<id> (auth: ...)` instead means the App itself is misconfigured (wrong ids, a revoked or rotated private key, the App uninstalled): the pool sets that bucket aside for five minutes at a time and continues on the others; fix the ids in the env file (`make tfvars`, `make apply`) or push the new key (`make push-secrets`, then a new revision so the pools re-read the secret). Never paste a token or the key into a ticket or a log.

## Change capacity and test restart recovery

Terraform's `pool_instances` object contains `authoring`, `build`, `trials` and `control`. Activity concurrency is separate from instance count; `SWESIM_MAX_CONCURRENT_ACTIVITIES` can override per-queue defaults. Preserve provider, Modal and spending limits when changing capacity.

Edit the non-secret Terraform inputs, inspect `make plan`, then `make apply`. If you customize `infra/env.auto.tfvars.json`, keep the state-bucket copy used by CI in sync. `make tfvars` regenerates defaults from the active account and dotenv inputs; review customized access and capacity afterward.

A restart acceptance exercise must record:

1. One active hosted trial id, frozen request hash, Job name and execution name.
2. The worker-pool restart or replacement event and before/after worker identity.
3. Resumed polling of that same execution, without another launch.
4. The completed frozen record, reward and transcript binding, or an honestly reported failure.
5. Cancellation behavior, if exercised, and the persisted cleanup receipt after record ingestion.

Save evidence under `workspace/evidence/cloudrun/` and reference it in ops. The old VM/systemd restart exercise does not establish Cloud Run recovery. The hosted exercise is pending.

## Rotate credentials

Update your ignored dotenv file or approved secret source, then:

```bash
make push-secrets PROJECT=YOUR_PROJECT SWESIM_ENV_FILE=./.env
```

Only changed GCP secrets receive new versions. Running containers may retain their startup environment; deploy a new tested workload revision and verify its pollers and health. Rerunning an unchanged Terraform apply alone does not prove that processes refreshed credentials.

The stakeholder uses a separate Modal secret. Its deployment script's `allow_existing=True` preserves existing values. To replace only its three required keys without placing values in shell arguments:

```bash
uv run --frozen python - <<'PY'
import json
import os
import subprocess
import tempfile
from dotenv import dotenv_values
values = dotenv_values(".env")
names = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SWESIM_WORKER_SECRET")
if any(not values.get(name) for name in names):
    raise SystemExit("Required stakeholder secret names are missing")
with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as source:
    os.chmod(source.name, 0o600)
    json.dump({name: values[name] for name in names}, source)
    source.flush()
    subprocess.run(["uv", "run", "--frozen", "modal", "secret", "create",
                    "swesim-llm", "--from-json", source.name, "--force"], check=True)
PY
make deploy-stakeholder SWESIM_ENV_FILE=./.env
```

The pinned Modal CLI supports `--from-json` and `--force`; see its [secret command reference](https://modal.com/docs/cli/latest/secret). Finish active interactive trials before rotating the shared worker secret, because existing session credentials depend on it. Verify `/health` afterward; a complete interactive smoke is the stronger check.

For provider outages or quota exhaustion, preserve the probe result and resolved role ids. The 2026-09-13 takeover used GPT-5.5 for judging and Sol for drafting/stakeholder/audit after Anthropic rejected requests; this is a dated observation. Re-probe through the workflow for a new run. Never relabel the requested solver as a different model.

## Import and rebuild the record

To import a preserved VM/laptop mirror, configure `SWESIM_WORKSPACE` and `SWESIM_REGISTRY` to that mirror, then:

```bash
SWESIM_ENV_FILE=./.env bash scripts/cloud/import_vm.sh
```

The script copies scenarios, compiled tasks, exports, candidates, evidence, probes, calibration, costs and retained job evidence without deleting unmatched bucket objects. It excludes trial task copies carrying session tokens and sensitive job config files. It then migrates and rebuilds the index with `origin=vm-systemd`, using the hosted maintenance Job when the laptop has no SQL connection.

The import is historical evidence, not a new hosted execution. New trials use the hosted origin and current frozen configuration.

For explicit hosted maintenance:

```bash
make migrate PROJECT=YOUR_PROJECT REGION=us-central1
SWESIM_MAINTENANCE_JOB=$(terraform -chdir=infra output -raw index_maintenance_job)
gcloud run jobs execute "$SWESIM_MAINTENANCE_JOB" --project=YOUR_PROJECT --region=us-central1 --args=status --wait
gcloud run jobs execute "$SWESIM_MAINTENANCE_JOB" --project=YOUR_PROJECT --region=us-central1 --args=rebuild,--origin=vm-systemd,--json --wait
```

Choose the origin appropriate to the input files; do not label imported legacy results as hosted runs. Read the Job logs for revision, row counts, skipped records and rebuild diff. Migrations and worker startup share the schema advisory lock. Rebuilds retain workflow and operator-action history while replacing artifact-backed rows.

Compare bucket scenario/trial artifacts against SQL rows and inspect every skipped record. Registry presence alone is insufficient. New discovery history is stored as immutable snapshots; its forward migration is `0002_discoveries`. Old compiled tasks without manifests must be rebuilt and validated before execution.

## Restore from bucket versions

List the object's generations and copy the selected generation into a recovery location first:

```bash
gcloud storage ls --all-versions gs://YOUR_BUCKET/trials/TRIAL_ID/record.json
gcloud storage cp 'gs://YOUR_BUCKET/trials/TRIAL_ID/record.json#GENERATION' ./recovered-record.json
```

Check its hashes and related spec/config/launch evidence. Record the selected generation and reason, then restore the verified artifact and rebuild the affected index projection. Restoring one file from an inconsistent set is not a valid recovered trial. Retention/lifecycle rules can remove older generations; inspect the bucket policy before depending on a version. The generation-qualified copy syntax is documented in [Cloud Storage versioned-object operations](https://docs.cloud.google.com/storage/docs/using-versioned-objects).

`make pull` creates a local mirror for inspection, excluding trial task copies and large Harbor job directories. It merges files and does not delete local objects absent from the bucket; it is not a complete proof of bucket parity.

## Revoke or grant console access

IAP access and evaluator access are separate Terraform lists: `iap_members` controls who can enter; `evaluators` controls hidden material within the app. Update both as appropriate, review the plan and apply. Keep the CI tfvars copy aligned. Verify a removed account is denied and an ordinary allowed account cannot reach evaluator routes. Forged email headers and query parameters do not grant evaluator access.

Notification channels are also operator configuration. An alert with an empty `notification_channels` list does not page anyone.

## Historical VM fallback

The preserved `scripts/vm/` backend uses systemd trial units and a local Temporal development server. Its old checks remain in ops as historical evidence. A deliberate fallback must keep `origin=vm-systemd`, preserve workflow/run ids and avoid mixing its trials with the hosted matrix. Do not redirect open Cloud workflows to a different namespace or queue as an improvised recovery step. The VM's own update path is `scripts/vm/update.sh` (fast-forward only: `git pull --ff-only`, then a worker/dashboard restart; it never restarts the Temporal server or trial units).

## Acceptance checklist

For the website release, record the exact commit/image, clean repeat Terraform plan, successful migration, authenticated Cloud pollers, IAP allow/deny checks, complete bucket/index parity and authenticated page/asset checks over preserved records. Fresh-clone `make check` and idempotent `cloud-bootstrap` are separate stranger-reuse checks.

Hosted creation and graded runs, one execution per trial, worker-restart experiments, difficulty screening and audited model comparisons remain deferred. They become required evidence when the experimental phase resumes; website availability is not proof of those outcomes.
