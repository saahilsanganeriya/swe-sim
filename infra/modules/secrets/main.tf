# Secret Manager resources. Values of `pushed_names` are added by scripts/cloud/push_secrets.py from the operator's env
# file (never by Terraform, never in state). Only DB_PASSWORD and SWESIM_DATABASE_URL have Terraform-managed versions,
# because Terraform generated the password.
resource "google_secret_manager_secret" "pushed" {
  for_each  = toset(var.pushed_names)
  project   = var.project
  secret_id = each.key
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "managed" {
  for_each  = toset(["DB_PASSWORD", "SWESIM_DATABASE_URL"])
  project   = var.project
  secret_id = each.key
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.managed["DB_PASSWORD"].id
  secret_data = var.db_password
}

resource "google_secret_manager_secret_version" "database_url" {
  secret      = google_secret_manager_secret.managed["SWESIM_DATABASE_URL"].id
  secret_data = var.database_url
}
