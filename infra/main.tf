data "google_project" "current" {
  project_id = var.project
}

locals {
  project_number = data.google_project.current.number
  bucket_name    = var.bucket_name != "" ? var.bucket_name : "swesim-${var.project}-workspace"
  tfstate_bucket = "${var.project}-swesim-tfstate"
  artifact_repo  = "${var.region}-docker.pkg.dev/${var.project}/swesim"
  image          = var.image != "" ? var.image : "${local.artifact_repo}/worker:latest"
  image_digest   = var.image != "" ? split("@", var.image)[1] : ""
  console_name   = "swesim-console"
  # Cloud Run's deterministic URL; stable across re-creation, so it can be an env var of the console itself.
  console_url  = "https://${local.console_name}-${local.project_number}.${var.region}.run.app"
  iap_audience = "/projects/${local.project_number}/locations/${var.region}/services/${local.console_name}"

  # One worker pool per Temporal task queue; the pool name is the queue name (swesim/workflows/queues.py).
  pools = {
    authoring = { queue = "swesim-authoring", instances = var.pool_instances.authoring }
    build     = { queue = "swesim-build", instances = var.pool_instances.build }
    trials    = { queue = "swesim-trials", instances = var.pool_instances.trials }
    control   = { queue = "swesim-control", instances = var.pool_instances.control }
  }

  # Secret resources whose values are pushed by scripts/cloud/push_secrets.py (never by Terraform).
  pushed_secret_names = [
    "TEMPORAL_API_KEY", "SWESIM_WORKER_SECRET", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "OPENROUTER_API_KEY", "GH_TOKEN", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "GH_APP_PRIVATE_KEY",
  ]
  # Secrets the deployer may read: TEMPORAL_API_KEY for the open-executions check in CI; the two Terraform-managed
  # versions because `google_secret_manager_secret_version` reads its payload back on refresh.
  deployer_secret_names = ["TEMPORAL_API_KEY", "DB_PASSWORD", "SWESIM_DATABASE_URL"]
}

module "registry" {
  source  = "./modules/registry"
  project = var.project
  region  = var.region
}

module "storage" {
  source        = "./modules/storage"
  project       = var.project
  region        = var.region
  bucket_name   = local.bucket_name
  force_destroy = var.bucket_force_destroy
}

module "sql" {
  source              = "./modules/sql"
  project             = var.project
  region              = var.region
  tier                = var.sql_tier
  deletion_protection = var.sql_deletion_protection
}

module "secrets" {
  source       = "./modules/secrets"
  project      = var.project
  pushed_names = local.pushed_secret_names
  db_password  = module.sql.password
  database_url = module.sql.database_url
}

module "iam" {
  source                = "./modules/iam"
  project               = var.project
  region                = var.region
  workspace_bucket      = module.storage.bucket
  tfstate_bucket        = local.tfstate_bucket
  cloudbuild_bucket     = module.storage.cloudbuild_bucket
  artifact_repo_id      = module.registry.repository_id
  secret_ids            = module.secrets.secret_ids
  deployer_secret_names = local.deployer_secret_names
  github_repository     = var.github_repository
}

module "run" {
  source                   = "./modules/run"
  count                    = var.deploy_workloads ? 1 : 0
  project                  = var.project
  region                   = var.region
  image                    = local.image
  image_digest             = local.image_digest
  worker_service_account   = module.iam.worker_email
  iap_service_agent_member = module.iam.iap_service_agent_member
  bucket                   = module.storage.bucket
  sql_connection_name      = module.sql.connection_name
  secret_ids               = module.secrets.secret_ids
  pools                    = local.pools
  console_name             = local.console_name
  console_url              = local.console_url
  iap_audience             = local.iap_audience
  iap_members              = var.iap_members
  iap_programmatic_clients = var.iap_programmatic_clients
  iap_client_projects      = var.iap_programmatic_client_projects
  project_number           = local.project_number
  iap_resource_name        = "projects/${local.project_number}/iap_web/cloud_run-${var.region}/services/${local.console_name}"
  evaluators               = var.evaluators
  spend_cap_usd            = var.spend_cap_usd
  stakeholder_url          = var.stakeholder_url
  temporal_address         = var.temporal_address
  temporal_namespace       = var.temporal_namespace
  gh_app_id                = var.gh_app_id
  gh_app_installation_id   = var.gh_app_installation_id
  worker_cpu               = var.worker_cpu
  worker_memory            = var.worker_memory
  console_min_instances    = var.console_min_instances
  console_max_instances    = var.console_max_instances
  depends_on               = [module.iam]
}

module "logging" {
  source                = "./modules/logging"
  count                 = var.deploy_workloads ? 1 : 0
  project               = var.project
  notification_channels = var.notification_channels
}
