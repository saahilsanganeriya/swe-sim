# infra — the swesim hosted stack on GCP

Terraform for everything that runs swesim in production: Cloud Run worker pools (one per Temporal task queue) and the
IAP-protected console, the GCS bucket that *is* the record, Cloud SQL for the rebuildable index, Secret Manager,
the two service accounts, Workload Identity Federation for GitHub Actions, and a little logging. Temporal Cloud and
Modal are external (their addresses are variables; their credentials are secrets). Nothing project-specific is in a
tracked file: the project id, accounts, bucket names and hostnames are variables with neutral defaults.

Binding documents: `docs/superpowers/specs/2026-09-13-hosted-pipeline-brief.md` (§2, §9) and
`docs/superpowers/specs/2026-09-13-interfaces.md` (§9, the infra ⇄ code contract).

## What exists after `make cloud-bootstrap`

| Piece | Resource | Notes |
|---|---|---|
| Image registry | Artifact Registry `swesim` (Docker) in `<region>` | `<region>-docker.pkg.dev/<project>/swesim/worker:<sha7>` and `:latest`; workloads are pinned to the digest. Cleanup: keep 20 most recent, delete untagged after 30 days. |
| The record | Bucket `swesim-<project>-workspace` | Versioned, uniform access, public access prevented; noncurrent versions pruned (keep 5 / 30 days). Mounted at `/workspace` read-write on the pools and the trial Jobs, read-only on the console (`uid=1000,gid=1000` mount options match the image's user). |
| Build sources | Bucket `<project>_cloudbuild` | Where `gcloud builds submit` stages sources; objects expire after 7 days. |
| Terraform state | Bucket `<project>-swesim-tfstate` | Created by `scripts/cloud/bootstrap.sh` (outside Terraform, by design); versioned. Also holds `env.auto.tfvars.json` for the GitHub deploy workflow. |
| Index | Cloud SQL Postgres 16 `swesim-index-<4hex>` | `db-custom-2-7680`, zonal, SSD, daily backups + PITR. **Public IP with no authorized networks**: reachable only through the Cloud Run Cloud SQL connection (`/cloudsql/<connection_name>` unix socket, IAM-authenticated proxy). Database `swesim`, user `swesim`. |
| Secrets | Secret Manager | `TEMPORAL_API_KEY SWESIM_WORKER_SECRET ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY GH_TOKEN MODAL_TOKEN_ID MODAL_TOKEN_SECRET` — resources by Terraform, **values only by `scripts/cloud/push_secrets.py`**. `DB_PASSWORD` and `SWESIM_DATABASE_URL` (`postgresql+psycopg://swesim:<pw>@/swesim?host=/cloudsql/<conn>`) are the only Terraform-written versions. |
| Identity | `swesim-worker` | Runtime identity of pools, Jobs, console: `secretmanager.secretAccessor` per secret, `storage.objectAdmin` on the bucket, `cloudsql.client`, `run.developer` (creates/executes/deletes trial Jobs), `iam.serviceAccountUser` on itself, log/metric writer. |
| | `swesim-deployer` | CI identity: `viewer`, `run.admin`, `cloudbuild.builds.editor`, `artifactregistry.writer` on the repo, `secretmanager.viewer` (names) + `secretAccessor` on `TEMPORAL_API_KEY`, `DB_PASSWORD`, `SWESIM_DATABASE_URL` only, `storage.objectAdmin` on the state and build buckets, `storage.objectUser` on the record, `serviceAccountUser` on `swesim-worker` and itself. |
| | WIF pool `github` / provider `github-oidc` | Trusts `token.actions.githubusercontent.com` for `assertion.repository == <github_repository>` only; that principal set may impersonate `swesim-deployer`. |
| Compute | Worker pools `swesim-authoring`, `swesim-build`, `swesim-trials`, `swesim-control` | Same image, `SWESIM_TASK_QUEUE=<pool name>`, 1 vCPU / 2 GiB, manual instance counts (`pool_instances`), bucket + Cloud SQL volumes, secrets as env references. |
| | Service `swesim-console` | Same image, command `uvicorn swesim.dashboard.app:app --host 0.0.0.0 --port 8080`, Cloud Run **native IAP** (`iap_enabled`, ingress all): the IAP service agent holds `run.invoker`, `iap_members` hold `iap.httpsResourceAccessor`. Only `TEMPORAL_API_KEY`, `SWESIM_WORKER_SECRET`, `SWESIM_DATABASE_URL` are mounted (no provider keys). |
| Observability | Log metrics `swesim/trial_reconcile`, `swesim/worker_crashes`; alert "swesim worker crash loop" | The alert is a log-match condition on worker-pool container exits (rate-limited to one notification per 30 min per pool); `notification_channels` is empty by default (the alert exists, nobody is paged). |
| Not Terraform | Jobs `swesim-trial-<id>` | Created at runtime by the Cloud Run launcher (`swesim/workflows/launchers/cloudrun.py`), one per trial, deleted after the record is written. |

Environment on the pools/console follows interfaces §9 exactly (`SWESIM_WORKSPACE=/workspace`, `SWESIM_REGISTRY`,
`SWESIM_BUCKET`, `SWESIM_TRIAL_LAUNCHER=cloudrun`, `SWESIM_ORIGIN=cloudrun`, `SWESIM_WORKER_IMAGE_DIGEST`,
`SWESIM_GCP_PROJECT/REGION`, `SWESIM_EVALUATORS`, `SWESIM_SPEND_CAP_USD`, `SWESIM_STAKEHOLDER_URL`, `SWESIM_CONSOLE_URL`,
`SWESIM_IAP_AUDIENCE`, `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`); empty values are omitted so `Settings` sees "unset".

## Variables (`infra/variables.tf`)

| Variable | Default | Meaning |
|---|---|---|
| `project` | — (required) | GCP project id |
| `region` | `us-central1` | Region for everything |
| `image` | `""` → `<artifact_repo>/worker:latest` | Digest reference the pools/console run; `make deploy` passes the digest |
| `iap_members` | `[]` | IAM members allowed through IAP (`user:...`, `group:...`) |
| `evaluators` | `[]` | Emails that see evaluator-only material (`SWESIM_EVALUATORS`) |
| `temporal_address`, `temporal_namespace` | `""` | Temporal Cloud endpoint and namespace (non-secret) |
| `stakeholder_url` | `""` | Stakeholder service base URL (Modal) |
| `spend_cap_usd` | `null` | `SWESIM_SPEND_CAP_USD`; null = unset |
| `pool_instances` | `{authoring=2, build=2, trials=1, control=1}` | Instances per worker pool |
| `github_repository` | `""` | `owner/name` trusted by the WIF provider |
| `deploy_workloads` | `true` | `false` only for the bootstrap apply (before secrets and image exist) |
| `bucket_name` | `""` → `swesim-<project>-workspace` | Record bucket name |
| `bucket_force_destroy` | `false` | Let destroy delete the record (PURGE) |
| `sql_tier`, `sql_deletion_protection` | `db-custom-2-7680`, `false` | Cloud SQL sizing / Terraform-side protection |
| `console_min_instances`, `console_max_instances` | `1`, `3` | Console scaling |
| `worker_cpu`, `worker_memory` | `"1"`, `"2Gi"` | Per pool instance |
| `notification_channels` | `[]` | Monitoring channels for the crash-loop alert |

The non-secret values come from your env file: `make tfvars` runs `scripts/cloud/tfvars_from_env.py`, which writes the
git-ignored `infra/env.auto.tfvars.json` (Temporal address/namespace, stakeholder URL, `iap_members`/`evaluators` from
`gcloud config get account`, `github_repository` from `git remote get-url origin`) and uploads a copy to the state bucket
for CI. `infra/terraform.tfvars.example` shows the same values for a hand-written `terraform.tfvars` (git-ignored).

## Outputs (`infra/outputs.tf`; `make outputs` → git-ignored `infra/outputs.json`)

`project`, `region`, `bucket`, `worker_service_account`, `deployer_service_account`, `artifact_repo`,
`sql_connection_name`, `database_name`, `database_user`, `console_url`, `console_service`, `iap_audience`
(`/projects/<number>/locations/<region>/services/swesim-console`, the JWT audience the console verifies),
`worker_pools` (queue → pool name), `wif_provider`, `image`, `image_digest`, `tfstate_bucket`, `temporal_address`,
`temporal_namespace`, `github_repository`. `scripts/cloud/verify_cloud.sh` and `scripts/cloud/pull.sh` read `outputs.json`.

## How to deploy

Prerequisites: a project with billing, `gcloud` authenticated as an owner (`gcloud auth login`; `gcloud auth
application-default login` is preferred for Terraform — without it the Makefile falls back to the active account's
short-lived token through `GOOGLE_OAUTH_ACCESS_TOKEN`), Terraform ≥ 1.9, `uv`, an env file filled from `.env.example`
with `SWESIM_ENV_FILE` pointing at it, and `~/.modal.toml` (or `MODAL_TOKEN_ID/SECRET` in the env file).

```bash
make check                                   # uv sync + offline suite: the checkout is healthy before any spend
make cloud-bootstrap PROJECT=<id> REGION=us-central1 SWESIM_ENV_FILE=<path> [EXTRA_ENV_FILES=<path with OPENROUTER_API_KEY>]
```

`cloud-bootstrap` is idempotent and runs, in order: `scripts/cloud/bootstrap.sh` (enable APIs, create the state bucket,
`terraform init`), `make tfvars`, `terraform apply -var deploy_workloads=false` (everything but the workloads),
`make push-secrets` (values streamed to Secret Manager on stdin; prints `NAME: updated|unchanged`), `make seed-registry`
(the tracked `registry.json` into an empty bucket, never overwriting), `make pin-image` (`scripts/cloud/build.sh`:
Cloud Build of `git archive HEAD`, reused when that sha already has an image, digest written to the git-ignored
`infra/image.auto.tfvars.json`), `terraform apply` (now pinned to the digest), `make migrate` (`swesim index migrate`
when the command exists and Cloud SQL is reachable, otherwise a notice), and `scripts/cloud/verify_cloud.sh`. Because the
digest lives in `image.auto.tfvars.json`, a later plain `make apply` keeps the pools on it (it never reverts to `:latest`);
`-var image=...` on the command line still overrides.

Later deploys:

```bash
make deploy SHA=<sha>        # build that commit -> terraform apply -var image=<digest> -> verify-cloud
make push-secrets            # after rotating a value in the env file (only changed secrets get a new version)
make plan | make apply       # any Terraform change; every plan/apply output lands in workspace/evidence/cloud/
make pull                    # mirror the record (without trials/*/task/) into the laptop workspace
```

Every apply is expected to be clean on re-run (`No changes.`); the second apply of the bootstrap night is saved as
`terraform-apply-2.txt` under the evidence directory.

### The GitHub path (`.github/workflows/deploy.yml`)

`workflow_dispatch` (inputs `sha`, `override_open_executions`) or a `v*` tag: authenticates with WIF as
`swesim-deployer`, fetches `env.auto.tfvars.json` from the state bucket, runs the open-executions check
(`uv run swesim workflows open-types`; a listed type blocks the deploy unless the override is set and recorded; skipped
with a warning while the command is not in the checkout), builds the image, plans and applies with the digest, runs
`verify_cloud.sh` and posts digest, plan line and verify output as the job summary. Repository variables, set once after
the first apply (`gh` authenticated):

```bash
gh variable set GCP_PROJECT --body "$(terraform -chdir=infra output -raw project)"
gh variable set GCP_REGION  --body "$(terraform -chdir=infra output -raw region)"
gh variable set WIF_PROVIDER --body "$(terraform -chdir=infra output -raw wif_provider)"
gh variable set DEPLOYER_SA  --body "$(terraform -chdir=infra output -raw deployer_service_account)"
```

(`make gh-variables` runs exactly these.) `ci.yml` runs the offline suite, the contract tests, `ruff --select E9,F`,
`terraform fmt -check` and `terraform validate` on every push and pull request.

### Reaching the index from outside Cloud Run

The instance has no authorized networks and no VPC path. From a laptop use the Cloud SQL Auth Proxy
(`cloud-sql-proxy <sql_connection_name>` with `roles/cloudsql.client`) and
`SWESIM_DATABASE_URL=postgresql+psycopg://swesim:<DB_PASSWORD>@127.0.0.1:5432/swesim`, or run `swesim index migrate |
rebuild | status` as a one-off Cloud Run Job with the worker image, the `swesim-worker` account, the Cloud SQL connection
and the `SWESIM_DATABASE_URL` secret (the same shape as a trial Job).

## How to destroy

```bash
make cloud-destroy PROJECT=<id>          # everything except the record bucket and the state bucket
make cloud-destroy PROJECT=<id> PURGE=1  # also deletes the record bucket and its objects
```

Without `PURGE=1` the workspace bucket is removed from the state first (so it is retained), then `terraform destroy`
runs. A later `cloud-bootstrap` on the same project must adopt the retained bucket: `make import-bucket`. The state
bucket is never deleted by these targets. Caveats: Cloud SQL instance names are reserved for about a week after deletion
(the instance carries a random suffix for this reason); WIF pool/provider ids are soft-deleted for 30 days
(`gcloud iam workload-identity-pools undelete` to reuse them).

## Applied by hand

Nothing is applied by hand. Two things are deliberately outside Terraform: the state bucket
(`scripts/cloud/bootstrap.sh`, `gcloud storage buckets create` + versioning) and the secret *values*
(`scripts/cloud/push_secrets.py`). Trial Jobs are runtime resources of the launcher.

## Layout

```
infra/
  versions.tf providers.tf variables.tf main.tf outputs.tf   root module (partial GCS backend, configured at init)
  terraform.tfvars.example                                    the non-secret inputs, with placeholders
  modules/registry  Artifact Registry
  modules/storage   record bucket + Cloud Build source bucket
  modules/sql       Cloud SQL instance, database, user, generated password
  modules/secrets   Secret Manager resources (+ the two Terraform-written versions)
  modules/iam       service accounts, roles, WIF pool/provider, IAP service agent
  modules/run       worker pools, console service, IAP bindings
  modules/logging   log-based metrics and the crash-loop alert
scripts/cloud/
  bootstrap.sh build.sh push_secrets.py tfvars_from_env.py verify_cloud.sh pull.sh   (import_vm.sh: storage stream)
Dockerfile.worker  cloudbuild.yaml  .dockerignore  .gcloudignore  Makefile  .github/workflows/{ci,deploy}.yml
```
