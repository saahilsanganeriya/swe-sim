# Cloud setup

This is the path from a fresh checkout to the hosted pipeline. All builds and graded environments run in Cloud Build or Modal; Cloud Run hosts the orchestrator clients and console. Your computer needs no Docker daemon and does not host Temporal.

The procedure below follows the current scripts. A fresh remote clone at `1fb1530` passed `make check` (895 passed, 3 skipped). Infrastructure connectivity has been measured and the first hosted creation workflows ran on 2026-09-13 ([Hosted smoke](ops.md#hosted-smoke-2026-09-13)); fresh-clone cloud bootstrap and the authenticated browser walkthrough are still pending. Check [the evidence ledger](ops.md) for newer results.

## 1. Accounts and tools

Prepare a billing-enabled GCP project and a Google account permitted to enable APIs, manage IAM, create Cloud Run/SQL/storage resources and deploy them. Install Git, `uv`, Terraform 1.9 or newer, the Google Cloud CLI and GitHub CLI. The repository pins its Python packages, including Harbor and Modal, in `uv.lock`.

You also need:

- A Temporal Cloud account, namespace, gRPC endpoint and namespace API key.
- A Modal account and token.
- Anthropic, OpenAI and OpenRouter API credentials for the configured roles and solver tiers.
- A GitHub token able to read the repositories and issue/PR threads you will mine. Use read-only fine-grained permissions where available. CI deployment additionally needs permission to manage the repository's Actions configuration.

Authenticate the operator tools:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT
gh auth login
```

Check `gcloud config get project`, `gcloud config get account`, `terraform version`, `uv --version` and `gh auth status`. Do not print access tokens to verify authentication. Terraform uses Application Default Credentials; the Makefile can fall back to the active gcloud account's short-lived token.

## 2. Clone and configure

Clone your repository or fork, whose `origin` should be the repository allowed to deploy through Workload Identity Federation:

```bash
git clone https://github.com/YOUR_ORG/swesim.git
cd swesim
cp .env.example .env
chmod 600 .env
uv sync --extra dev --frozen
```

The checked-in [.env.example](../.env.example) describes every variable and its source. It defaults to local development; replace these values for cloud operation:

| Variable | Value to supply |
|---|---|
| `TEMPORAL_ADDRESS` | Your namespace's Cloud gRPC endpoint, not localhost |
| `TEMPORAL_NAMESPACE` | Your full Temporal Cloud namespace name |
| `TEMPORAL_API_KEY` | Namespace API key |
| `SWESIM_GCP_PROJECT`, `SWESIM_GCP_REGION` | Your project and region |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` | Provider credentials |
| `GH_TOKEN` | Repository-read token |
| `SWESIM_WORKER_SECRET` | Fresh random secret shared with the stakeholder service |
| `SWESIM_SPEND_CAP_USD` | Your run-planning spending limit |
| `SWESIM_STAKEHOLDER_URL` | Filled after step 4 |

Keep `SWESIM_DATABASE_URL` empty on the laptop unless you intentionally use a Cloud SQL Auth Proxy. A Cloud Run unix-socket URL will not work on your laptop. Terraform supplies runtime bucket, database, image, service-account, SQL-connection, IAP-audience and launcher values to hosted workloads.

This creates the worker secret and copies the authenticated GitHub token into the ignored file without printing either value. Existing values are preserved:

```bash
uv run --frozen python - <<'PY'
import secrets
import subprocess
from pathlib import Path
from dotenv import dotenv_values, set_key
path = Path(".env")
values = dotenv_values(path)
if not values.get("SWESIM_WORKER_SECRET"):
    set_key(str(path), "SWESIM_WORKER_SECRET", secrets.token_hex(32))
if not values.get("GH_TOKEN"):
    token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    set_key(str(path), "GH_TOKEN", token)
path.chmod(0o600)
PY
```

Edit provider and Temporal values with your editor. Dotenv files are read with python-dotenv; never shell-source them. All examples use `./.env`. For another location, pass `SWESIM_ENV_FILE=/path/to/your.env` to commands and Make.

**Optional GitHub App (a second discovery quota).** GitHub's core API limit is 5,000 calls per hour per identity, and a repository scan spends several calls per pull request, so `GH_TOKEN` alone caps discovery. A GitHub App installation is a separate identity with its own 5,000/h, and the workers rotate between every configured bucket (`swesim/sources/github_auth.py`): a refused token is set aside until its reset and the call is retried at once with another. To add one: at github.com/settings/apps create an App (any name, e.g. `swesim-discovery`; no webhook; repository permissions Contents, Issues, Metadata and Pull requests read-only), note its **App ID**, generate a **private key** and store the downloaded `.pem` outside the repository with mode 600, then install the App on the account or organization whose repositories you mine and take the **installation id** from the end of the installation's settings URL (github.com/settings/installations/<id>). Put `GH_APP_ID`, `GH_APP_INSTALLATION_ID` and `GH_APP_PRIVATE_KEY_PATH=/path/to/that.pem` in the env file; leave `GH_APP_PRIVATE_KEY` empty there. The laptop reads the key from that path; the hosted pools read it from Secret Manager: `make push-secrets` copies the file's bytes into the `GH_APP_PRIVATE_KEY` secret (or pass `--file GH_APP_PRIVATE_KEY=/path/to/that.pem` to `scripts/cloud/push_secrets.py`), and `make tfvars` copies the two ids into `infra/env.auto.tfvars.json` so `make apply` sets them on the worker pools and mounts the secret there; without the ids nothing about the App is deployed and `make push-secrets` reports the key as `skipped (optional)`. More personal tokens work the same way: `GH_TOKEN_2`, `GH_TOKEN_3`, ... in the env file (up to `SWESIM_GH_EXTRA_TOKENS`, default 3) are extra laptop-only buckets; they are not pushed to Secret Manager.

## 3. Check the checkout

```bash
make check
uv run --frozen swesim create --help
uv run --frozen swesim run-tasks --help
```

`make check` synchronizes the pinned development dependencies and runs the offline suite excluding `tests/integration`. Optional disposable-Postgres tests require their own explicit test URL. A passing check establishes local code health; it does not establish a hosted run.

Then, before the first `swesim create`/`run-tasks`, register swesim's six Keyword search attributes on the Temporal Cloud namespace you configured in step 2:

```bash
make bootstrap-namespace SWESIM_ENV_FILE=./.env
```

`make cloud-bootstrap` (step 5) also runs it after the secrets. The script connects with the configured API key, prints only the address, namespace and auth mode, and registers whatever is missing. On Temporal Cloud the SDK cannot register them (the OperatorService answers `PERMISSION_DENIED`, and Temporal Cloud denies even `ListSearchAttributes`, measured on 2026-09-13), so the script verifies presence with a read-only visibility probe (`count_workflows` per attribute). When something is missing or unverifiable it prints the exact command

```text
tcld namespace search-attributes add --namespace <ns> --search-attribute swesim_scenario_id=Keyword --search-attribute swesim_model=Keyword --search-attribute swesim_condition=Keyword --search-attribute swesim_status=Keyword --search-attribute swesim_version=Keyword --search-attribute swesim_purpose=Keyword
```

(or: cloud.temporal.io → Namespaces → your namespace → Edit → Custom Search Attributes → add each as Keyword → Save) and exits 2. Run that once, re-run the make target, and it exits 0 with `search attributes: all 6 present on <ns> (verified by visibility queries; the OperatorService answers PERMISSION_DENIED on Temporal Cloud)`. Exit 1 is a connection or authentication error (class and gRPC status only, never the key). Exit 2 stops `cloud-bootstrap` until the operator step is done; a re-run is then a no-op.

## 4. Configure Modal

Configure Modal before bootstrap:

```bash
uv run --frozen modal setup
```

Bootstrap calls `scripts/deploy_stakeholder.sh`: it creates the `swesim-llm` Modal secret, deploys the service, writes its URL to ignored `.stakeholder_url`, and checks `/health`. The script currently requires Anthropic and OpenAI keys plus `SWESIM_WORKER_SECRET`, even when a role later falls back to OpenAI.

After bootstrap, copy the deployed URL into your dotenv file for CLI clients:

```bash
uv run --frozen python - <<'PY'
from pathlib import Path
from dotenv import set_key
set_key(".env", "SWESIM_STAKEHOLDER_URL", Path(".stakeholder_url").read_text().strip())
PY
```

The script reuses an existing Modal secret; it does not rotate its values. See [rotation](runbook.md#rotate-credentials) before redeploying changed keys. `make deploy-stakeholder` runs this step separately when needed.

## 5. Provision and deploy GCP

```bash
make cloud-bootstrap PROJECT=YOUR_PROJECT REGION=us-central1 SWESIM_ENV_FILE=./.env
```

The target enables APIs, initializes a versioned Terraform-state bucket, checks for open workflows, deploys the stakeholder, generates non-secret tfvars, creates infrastructure, uploads secret values through stdin, registers the namespace's search attributes (exit 2 means: do the printed `tcld`/UI step once, then re-run), seeds an empty registry only when the workspace bucket has none, builds the selected Git commit in Cloud Build, pins the image digest, deploys the worker pools and console, publishes the applied pin to the state bucket, runs the Cloud Run index-maintenance Job, then verifies runtime connectivity. If the dotenv file has no stakeholder URL, tfvars generation uses the newly written `.stakeholder_url`.

The initial IAP and evaluator allowlists contain the active gcloud account. The GitHub repository trusted by WIF comes from `origin`. Review `infra/env.auto.tfvars.json` and the Terraform plan before expanding access. Generated tfvars and `infra/outputs.json` are ignored; they must not be committed.

Checks:

```bash
make outputs
terraform -chdir=infra output -raw console_url
make verify-cloud
make plan
```

The second plan should report no changes. `verify-cloud` must report API-key Temporal Cloud authentication, pollers for all activity queues and the workflow queue, IAP 302, Ready workloads and image parity. Read every `NOTE`: no trial Jobs or skipped SQL checks are outstanding evidence, not passing end-to-end checks.

New buckets start with `{"scenarios": {}}`; an existing registry is preserved. The tracked historical catalog is not automatically imported. [Record import](runbook.md#import-and-rebuild-the-record) and complete bucket/index parity are separate checks.

## 6. First use

Open the Terraform console URL and sign in. An account outside the IAP allowlist must be denied; evaluator-only material must remain hidden from an ordinary allowed account.

Browse Scenarios, Trials, Candidates, Workflows, Database and artifacts. An empty new bucket should show honest empty states; use the record-import procedure only when you have a preserved dataset to import. Check that counts and links match the indexed record, failed/unresolved attempts remain visible, and evaluator material is gated.

Creation and Run controls are available for later experiments and require confirmation. When that work resumes, choose small limits and follow each workflow's candidate outcomes; a legitimate rejection is not a service failure. Do not edit drafts to force acceptance. Screening precedes comparison, and solver ids are explicit.

Website acceptance requires the final image/schema, authenticated page and role checks, and record/index parity. Experimental acceptance later requires newly executed controls and trial evidence, bound interactive transcripts, one runtime execution per trial, an informative difficulty screen and passing task-sense labels. Those experiments are explicitly deferred in [ops.md](ops.md).

## 7. Teardown

```bash
make cloud-destroy PROJECT=YOUR_PROJECT SWESIM_ENV_FILE=./.env
```

This retains the workspace bucket and Terraform-state bucket. `PURGE=1` also deletes the workspace record and its objects; use it only when you intend that loss. A later bootstrap must adopt a retained workspace bucket with `make import-bucket` before applying a new state. Cloud SQL deletion is part of destruction; preserve the authoritative record and required backups first.
