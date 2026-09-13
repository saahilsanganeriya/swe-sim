# GCP hosting — implementation and acceptance

Updated 2026-09-13. The hosted-pipeline brief supersedes this document's original VM/MIG proposal. The implemented target uses **Cloud Run worker pools and Jobs, Temporal Cloud, GCS and Cloud SQL**. GKE, a VM managed-instance group, a VM load balancer and periodic disk-to-bucket mirroring are not the hosted architecture.

This document distinguishes implemented resources from demonstrated end-to-end behavior. [Setup](../../setup.md), [runbook](../../runbook.md), [architecture](../../architecture.md) and [operations evidence](../../ops.md) are the operating references.

## Implemented topology

| Layer | Implementation |
|---|---|
| Workflow service | Temporal Cloud, API-key authentication with TLS; namespace and endpoint are configuration |
| Worker pools | Four native Cloud Run worker pools, one per `swesim-authoring`, `swesim-build`, `swesim-trials`, `swesim-control` queue |
| Workflow polling | Authoring queue; verified separately from activity pollers |
| Trial execution | A runtime-created Cloud Run Job per deterministic trial id; one task, no infrastructure retry; Harbor remains a Modal client |
| Heavy compute | Modal task builds, agent environments and separate offline verifiers |
| Stakeholder | Modal service with persisted sessions and shared worker authentication |
| Record | Versioned GCS bucket mounted at `/workspace`; workers/Jobs writable, console read-only |
| Index | Cloud SQL PostgreSQL 16 with strict row models, matching tables and forward Alembic migrations |
| Maintenance | Terraform-managed Cloud Run Job for index migrations, status and rebuilds |
| Console | Cloud Run service with native IAP; signature/audience-verified identity and separate evaluator allowlist |
| Secrets | Secret Manager for GCP runtimes, Modal secret for the stakeholder |
| Build/deploy | Cloud Build from a Git commit; Terraform pins the digest; GitHub WIF supports the runtime deployment workflow |
| Observability | Cloud Logging metrics/alerts, Temporal history, durable launch/result evidence; notification channels require operator configuration |

The Cloud SQL instance has no authorized public client networks. Hosted connections use the Cloud Run Cloud SQL socket and runtime identity, with the database credential in Secret Manager. A laptop uses the maintenance Job or an explicitly configured Auth Proxy; it does not need the hosted database URL for normal operation.

## Ownership of state

Temporal owns workflow history. The bucket owns scenario, trial, cost, audit and discovery artifacts. Postgres is their rebuildable index plus workflow/operator-action projections and advisory locks. It is not an alternate source for invented result files.

Every trial preserves its spec and effective configuration, including actual model, purpose, image/build identity and scaffold. A deterministic Job name and persisted launch intent are used together; a workflow id alone is not exactly-once execution. Unknown launch state is reconciled rather than relaunched. Jobs are cleaned up after durable results; cleanup must preserve proof of the execution.

Discovery observations have immutable ids derived from source, reject-artifact hash and workflow/run identity. Latest-per-source counters feed the histogram; history remains browsable. Legacy flat artifacts carry explicitly unknown workflow identity.

## Provisioning and deployment

`make cloud-bootstrap PROJECT=YOUR_PROJECT REGION=us-central1` initializes state and APIs, checks open workflows, deploys the stakeholder, generates non-secret tfvars, provisions the remaining resources, pushes secret values, seeds a new bucket with an empty registry, builds/pins the worker image, applies workloads, runs the maintenance migration and verifies runtime connectivity.

A subsequent `make deploy SHA=COMMIT_SHA` checks open workflows, builds the selected commit, applies its digest, migrates through the hosted maintenance Job and verifies. Migration CLI and worker startup share a schema advisory lock. The deploy workflow's IAM scope permits runtime changes; broader infrastructure changes remain an operator apply.

An existing record is imported separately and retains its legacy origin. Bootstrap must not seed references to scenario assets that were never uploaded. Project, region, account, bucket, namespace and public URLs come from inputs/outputs; no deployment depends on an author's laptop path.

## Observed on 2026-09-13

Cloud Build `55086fb6` from `2bc7389` succeeded. Terraform added the maintenance Job and corrected missing trial-worker runtime values. Maintenance execution `swesim-index-maintenance-tl5t6` succeeded. The stakeholder redeployed with its missing `python-ulid` dependency and answered `/health`.

`workspace/evidence/cloud/verify_cloud-20260913T164426Z.txt` records authenticated Temporal Cloud access, pollers on every activity queue and the workflow queue, IAP HTTP 302, Ready resources and image parity at digest `c095bcb87841fad47686f3d1bdd43ea652b81175d6bcb8b2732e07a6f3c7e726`.

Its limitations are explicit: no trial Jobs existed and SQL status was skipped from the laptop. The earlier 09:17 verification used localhost Temporal and missed a trials-worker crash loop; it is not accepted as hosted workflow evidence.

## Website acceptance still to establish

- Full bucket/import parity against the registry and SQL, including skipped and unresolved records.
- An allowed/denied browser-role walkthrough through IAP.
- An operator-run idempotent `cloud-bootstrap` from the fresh clone. The developer check passed at `1fb1530` with 895 passed and 3 skipped.
- Authenticated page, asset and artifact checks against the final deployed image and database schema.

New task discovery, hosted graded trials, worker-restart experiments, difficulty screening and the model sweep are deferred by the current user priority. Those outcomes still require their own evidence when experimental work resumes.

The preserved 50 trial records and three version-2 scenarios are historical demonstrations. The 19 current-spec records do not constitute the new hosted sweep. No monthly-cost estimate is asserted here; actual provider costs and unknown prices are tracked in the record, while cloud billing must be measured separately.

The VM/systemd backend and its restart demonstration remain available as historical fallback evidence. Using that backend again requires an explicit origin and experiment label; it does not satisfy the Cloud Run acceptance above.
