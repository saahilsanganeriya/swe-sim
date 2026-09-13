# Hosted pipeline runbook

Use this after [setup](setup.md). Commands run from the checkout with a filled ignored dotenv file. Replace uppercase placeholders and use your configured project and region. The authoritative record is the bucket; never repair a result by editing its reward, report, spec or launch files.

Current measured results and outstanding acceptance checks are in [ops.md](ops.md). The immediate priority is the hosted website over preserved records; new discovery and experiments are deferred. The trial procedures below are for later operation or recovery of existing work.

## Deploy a commit

```bash
make check
uv run --frozen swesim workflows open-types
make deploy PROJECT=YOUR_PROJECT REGION=us-central1 SHA=COMMIT_SHA SWESIM_ENV_FILE=./.env
```

The deployment guard records open workflow types and stops before building if any are open. Finish or cancel them, or verify Temporal patch compatibility and record an explicit `OVERRIDE_OPEN_EXECUTIONS=1` override. A new implementation must retain the old `workflow.patched` branches while matching executions remain open.

Deployment builds the selected Git commit with Cloud Build, pins its digest in ignored `infra/image.auto.tfvars.json`, applies Terraform, executes the index-maintenance Job, then verifies the hosted runtime. A failed migration or verification stops the target. Keep the build id, commit, image digest, Terraform output, migration execution id and verification path together.

The GitHub deployment workflow uses WIF and the deployer service account. It is restricted to runtime resources; IAM, storage and SQL changes need an operator's reviewed Terraform apply. `make gh-variables` sets the repository variables from Terraform outputs.

A successful deploy does not replace a hosted smoke test. Check the exact new image in the worker pools, console and next trial record.

## Inspect work and spend

Use the console's Workflows, Candidates, Trials, Database and artifact pages. The CLI equivalents for workflow state are:

```bash
uv run --frozen swesim workflows list all
uv run --frozen swesim workflows progress WORKFLOW_ID
uv run --frozen swesim workflows cancel WORKFLOW_ID
```

Use the console's spend view and cost rows for recorded provider usage. Unknown usage or price remains unknown, not zero. `SWESIM_SPEND_CAP_USD` stops planning new trials once recorded spend crosses the cap; it is not an external billing limit and does not retroactively cancel work already in flight.

Keep `purpose=screen`, `comparison`, `calibration` and `demonstration` separate. A screen checks hidden/full success before a larger comparison. Frontier tiers require the brief's gate; no requested model is silently substituted.

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

The preserved `scripts/vm/` backend uses systemd trial units and a local Temporal development server. Its old checks remain in ops as historical evidence. A deliberate fallback must keep `origin=vm-systemd`, preserve workflow/run ids and avoid mixing its trials with the hosted matrix. Do not redirect open Cloud workflows to a different namespace or queue as an improvised recovery step.

## Acceptance checklist

For the website release, record the exact commit/image, clean repeat Terraform plan, successful migration, authenticated Cloud pollers, IAP allow/deny checks, complete bucket/index parity and authenticated page/asset checks over preserved records. Fresh-clone `make check` and idempotent `cloud-bootstrap` are separate stranger-reuse checks.

Hosted creation and graded runs, one execution per trial, worker-restart experiments, difficulty screening and audited model comparisons remain deferred. They become required evidence when the experimental phase resumes; website availability is not proof of those outcomes.
