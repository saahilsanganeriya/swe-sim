# swesim hosted stack: Cloud Run (worker pools + console) + Temporal Cloud + GCS record + Cloud SQL index.
# State lives in a GCS bucket created by `scripts/cloud/bootstrap.sh`; the backend is configured at `terraform init`
# (`-backend-config=bucket=<project>-swesim-tfstate -backend-config=prefix=terraform/state`), never hard-coded here.
terraform {
  required_version = ">= 1.9"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.2"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 8.2"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  backend "gcs" {}
}
