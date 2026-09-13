output "url" {
  value = "${var.region}-docker.pkg.dev/${var.project}/${google_artifact_registry_repository.swesim.repository_id}"
}

output "repository_id" {
  value = google_artifact_registry_repository.swesim.repository_id
}
