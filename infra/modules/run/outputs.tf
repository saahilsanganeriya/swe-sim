output "pool_names" {
  description = "Temporal task queue -> worker pool name."
  value       = { for k, p in google_cloud_run_v2_worker_pool.pool : p.name => p.name }
}

output "console_uri" {
  value = google_cloud_run_v2_service.console.uri
}

output "console_urls" {
  value = google_cloud_run_v2_service.console.urls
}
