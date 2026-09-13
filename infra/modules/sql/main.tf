# Cloud SQL Postgres 16 for the rebuildable index. Public IP with NO authorized networks: the only path in is the
# Cloud Run Cloud SQL connection (IAM-authenticated proxy, unix socket /cloudsql/<connection_name>). No VPC tonight.
resource "random_id" "suffix" {
  byte_length = 2
}

resource "random_password" "db" {
  length  = 32
  special = false # URL-safe: the password is embedded in SWESIM_DATABASE_URL without escaping
}

resource "google_sql_database_instance" "index" {
  project             = var.project
  name                = "swesim-index-${random_id.suffix.hex}" # instance names are unusable for a week after deletion
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = var.deletion_protection

  settings {
    tier                        = var.tier
    edition                     = "ENTERPRISE"
    availability_type           = "ZONAL"
    disk_type                   = "PD_SSD"
    disk_size                   = 20
    disk_autoresize             = true
    deletion_protection_enabled = false

    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }
    backup_configuration {
      enabled                        = true
      start_time                     = "09:00"
      point_in_time_recovery_enabled = true
      backup_retention_settings {
        retained_backups = 7
      }
    }
    insights_config {
      query_insights_enabled = true
    }
    maintenance_window {
      day  = 7
      hour = 9
    }
  }
}

resource "google_sql_database" "swesim" {
  project  = var.project
  name     = "swesim"
  instance = google_sql_database_instance.index.name
}

resource "google_sql_user" "swesim" {
  project         = var.project
  name            = "swesim"
  instance        = google_sql_database_instance.index.name
  password        = random_password.db.result
  deletion_policy = "ABANDON"
}
