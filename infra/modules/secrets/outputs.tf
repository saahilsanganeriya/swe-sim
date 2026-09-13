output "secret_ids" {
  description = "Secret name -> secret_id (equal by construction) for every secret the workloads reference."
  value = merge(
    { for k, s in google_secret_manager_secret.pushed : k => s.secret_id },
    { for k, s in google_secret_manager_secret.managed : k => s.secret_id },
  )
}
