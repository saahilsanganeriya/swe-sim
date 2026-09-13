output "worker_email" {
  value = google_service_account.worker.email
}

output "worker_member" {
  value = google_service_account.worker.member
}

output "deployer_email" {
  value = google_service_account.deployer.email
}

output "wif_provider" {
  description = "Full resource name for google-github-actions/auth `workload_identity_provider`."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "iap_service_agent_member" {
  value = google_project_service_identity.iap.member
}
