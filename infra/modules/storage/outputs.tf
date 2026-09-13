output "bucket" {
  value = google_storage_bucket.workspace.name
}

output "cloudbuild_bucket" {
  value = google_storage_bucket.cloudbuild.name
}
