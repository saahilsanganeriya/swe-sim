#!/bin/bash
# Idempotent preparation before the first `terraform apply`:
#   1. enable the Google APIs the stack uses,
#   2. create the versioned Terraform state bucket gs://<project>-swesim-tfstate (outside Terraform, by design),
#   3. `terraform init` against it (partial backend config; nothing project-specific is in infra/*.tf).
#
#   bash scripts/cloud/bootstrap.sh <project> <region>
#
# Re-running is safe: every step checks or is a no-op on an already-prepared project.
set -euo pipefail
cd "$(dirname "$0")/../.."

PROJECT=${1:?usage: bootstrap.sh <project> <region>}
REGION=${2:?usage: bootstrap.sh <project> <region>}
BUCKET="$PROJECT-swesim-tfstate"

echo "== enabling APIs in $PROJECT" >&2
gcloud services enable \
  run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com iap.googleapis.com iam.googleapis.com iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com serviceusage.googleapis.com logging.googleapis.com monitoring.googleapis.com \
  sts.googleapis.com compute.googleapis.com storage.googleapis.com \
  --project "$PROJECT" 1>&2

echo "== state bucket gs://$BUCKET" >&2
if ! gcloud storage buckets describe "gs://$BUCKET" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$BUCKET" --project "$PROJECT" --location "$REGION" \
    --uniform-bucket-level-access --public-access-prevention 1>&2
fi
gcloud storage buckets update "gs://$BUCKET" --versioning 1>&2

echo "== terraform init" >&2
terraform -chdir=infra init -input=false -reconfigure \
  -backend-config="bucket=$BUCKET" -backend-config="prefix=terraform/state" 1>&2

echo "gs://$BUCKET"
