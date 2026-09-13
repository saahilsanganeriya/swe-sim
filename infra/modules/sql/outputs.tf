output "connection_name" {
  value = google_sql_database_instance.index.connection_name
}

output "instance_name" {
  value = google_sql_database_instance.index.name
}

output "database_name" {
  value = google_sql_database.swesim.name
}

output "user" {
  value = google_sql_user.swesim.name
}

output "password" {
  value     = random_password.db.result
  sensitive = true
}

output "database_url" {
  description = "SQLAlchemy URL through the Cloud Run Cloud SQL unix socket; stored as one secret so code never assembles credentials."
  value       = "postgresql+psycopg://${google_sql_user.swesim.name}:${random_password.db.result}@/${google_sql_database.swesim.name}?host=/cloudsql/${google_sql_database_instance.index.connection_name}"
  sensitive   = true
}
