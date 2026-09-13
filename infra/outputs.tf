# The infra <-> code contract (docs/superpowers/specs/2026-09-13-interfaces.md §9). `make outputs` writes these to
# infra/outputs.json (git-ignored) for the scripts under scripts/cloud/.
output "project" {
  value = var.project
}

output "region" {
  value = var.region
}

output "bucket" {
  value = module.storage.bucket
}

output "worker_service_account" {
  value = module.iam.worker_email
}

output "deployer_service_account" {
  value = module.iam.deployer_email
}

output "artifact_repo" {
  value = module.registry.url
}

output "sql_connection_name" {
  value = module.sql.connection_name
}

output "database_name" {
  value = module.sql.database_name
}

output "database_user" {
  value = module.sql.user
}

output "console_url" {
  value = local.console_url
}

output "console_service" {
  value = local.console_name
}

output "iap_audience" {
  value = local.iap_audience
}

output "worker_pools" {
  description = "Temporal task queue -> Cloud Run worker pool name."
  value       = var.deploy_workloads ? module.run[0].pool_names : { for k, p in local.pools : p.queue => p.queue }
}

output "wif_provider" {
  value = module.iam.wif_provider
}

output "image" {
  description = "Image reference the pools and the console run (digest-pinned after the first deploy)."
  value       = local.image
}

output "image_digest" {
  value = local.image_digest
}

output "tfstate_bucket" {
  value = local.tfstate_bucket
}

output "temporal_address" {
  value = var.temporal_address
}

output "temporal_namespace" {
  value = var.temporal_namespace
}

output "github_repository" {
  value = var.github_repository
}

output "index_maintenance_job" {
  value = var.deploy_workloads ? module.run[0].index_maintenance_job : "swesim-index-maintenance"
}
