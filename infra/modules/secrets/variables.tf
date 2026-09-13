variable "project" {
  type = string
}

variable "pushed_names" {
  type        = list(string)
  description = "Secrets whose values come from push_secrets.py."
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "database_url" {
  type      = string
  sensitive = true
}
