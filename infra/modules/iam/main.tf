# Two service accounts (brief §2.4, §2.6.1):
# - swesim-worker: runtime identity of the pools, the trial Jobs and the console.
# - swesim-deployer: Cloud Build + `terraform apply -var image=...` from GitHub Actions through Workload Identity
#   Federation (no long-lived keys). It can read secret names, and the payloads of exactly the secrets the root passes.
resource "google_service_account" "worker" {
  project      = var.project
  account_id   = "swesim-worker"
  display_name = "swesim worker (pools, trial Jobs, console)"
}

resource "google_service_account" "deployer" {
  project      = var.project
  account_id   = "swesim-deployer"
  display_name = "swesim deployer (Cloud Build, Terraform apply from CI)"
}

locals {
  worker_project_roles = [
    "roles/cloudsql.client",   # Cloud SQL connection from pools, Jobs and console
    "roles/run.developer",     # create/execute/delete trial Jobs at runtime (launcher)
    "roles/logging.logWriter", # structured logs from Jobs
    "roles/monitoring.metricWriter",
  ]
  deployer_project_roles = [
    "roles/viewer",                   # terraform refresh of every managed resource
    "roles/run.admin",                # roll pools/console to a new digest
    "roles/cloudbuild.builds.editor", # submit builds
    "roles/logging.logWriter",        # build logs when the build runs as this account
    "roles/secretmanager.viewer",     # names only (payload access is per-secret below)
  ]
}

resource "google_project_iam_member" "worker" {
  for_each = toset(local.worker_project_roles)
  project  = var.project
  role     = each.key
  member   = google_service_account.worker.member
}

resource "google_project_iam_member" "deployer" {
  for_each = toset(local.deployer_project_roles)
  project  = var.project
  role     = each.key
  member   = google_service_account.deployer.member
}

resource "google_storage_bucket_iam_member" "worker_workspace" {
  bucket = var.workspace_bucket
  role   = "roles/storage.objectAdmin"
  member = google_service_account.worker.member
}

resource "google_storage_bucket_iam_member" "deployer_workspace" {
  bucket = var.workspace_bucket
  role   = "roles/storage.objectUser" # verify_cloud.sh reads registry.json and uploads its evidence file
  member = google_service_account.deployer.member
}

resource "google_storage_bucket_iam_member" "deployer_tfstate" {
  bucket = var.tfstate_bucket
  role   = "roles/storage.objectAdmin" # terraform state + lock objects, env.auto.tfvars.json
  member = google_service_account.deployer.member
}

resource "google_storage_bucket_iam_member" "deployer_cloudbuild" {
  bucket = var.cloudbuild_bucket
  role   = "roles/storage.objectAdmin"
  member = google_service_account.deployer.member
}

# The launcher creates trial Jobs with the worker identity; Cloud Run validates image access with the CALLER's permissions
# at CreateJob (measured 2026-09-13: 403 artifactregistry.repositories.downloadArtifacts on the first hosted trial). The pools
# themselves pull through the Cloud Run service agent and never hit this.
resource "google_artifact_registry_repository_iam_member" "worker_reader" {
  project    = var.project
  location   = var.region
  repository = var.artifact_repo_id
  role       = "roles/artifactregistry.reader"
  member     = google_service_account.worker.member
}

resource "google_artifact_registry_repository_iam_member" "deployer_writer" {
  project    = var.project
  location   = var.region
  repository = var.artifact_repo_id
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.deployer.member
}

resource "google_secret_manager_secret_iam_member" "worker_accessor" {
  for_each  = var.secret_ids
  project   = var.project
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.worker.member
}

resource "google_secret_manager_secret_iam_member" "deployer_accessor" {
  for_each  = toset(var.deployer_secret_names)
  project   = var.project
  secret_id = var.secret_ids[each.key]
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.deployer.member
}

# The worker creates Jobs that run as itself; the deployer deploys pools/console/builds that run as the worker or itself.
resource "google_service_account_iam_member" "worker_self_user" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.worker.member
}

resource "google_service_account_iam_member" "deployer_acts_as_worker" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer.member
}

resource "google_service_account_iam_member" "deployer_self_user" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer.member
}

# Workload Identity Federation for GitHub Actions (brief §2.6.1). Pool/provider ids are soft-deleted for 30 days after
# destroy; re-creating them within that window needs `gcloud iam workload-identity-pools undelete`.
resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  display_name                       = "GitHub OIDC"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.actor"      = "assertion.actor"
    "attribute.ref"        = "assertion.ref"
  }
  attribute_condition = "assertion.repository == \"${var.github_repository}\""
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "deployer_wif" {
  count              = var.github_repository != "" ? 1 : 0
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

# The IAP service agent must exist before it can be granted run.invoker on the console.
resource "google_project_service_identity" "iap" {
  provider = google-beta
  project  = var.project
  service  = "iap.googleapis.com"
}
