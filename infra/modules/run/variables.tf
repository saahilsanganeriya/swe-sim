variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "image" {
  type = string
}

variable "image_digest" {
  type = string
}

variable "worker_service_account" {
  type = string
}

variable "iap_service_agent_member" {
  type = string
}

variable "bucket" {
  type = string
}

variable "sql_connection_name" {
  type = string
}

variable "secret_ids" {
  type = map(string)
}

variable "pools" {
  type = map(object({
    queue     = string
    instances = number
  }))
}

variable "console_name" {
  type = string
}

variable "console_url" {
  type = string
}

variable "iap_audience" {
  type = string
}

variable "iap_members" {
  type = list(string)
}

variable "evaluators" {
  type = list(string)
}

variable "spend_cap_usd" {
  type     = number
  nullable = true
}

variable "stakeholder_url" {
  type = string
}

variable "temporal_address" {
  type = string
}

variable "temporal_namespace" {
  type = string
}

variable "worker_cpu" {
  type = string
}

variable "worker_memory" {
  type = string
}

variable "console_min_instances" {
  type = number
}

variable "console_max_instances" {
  type = number
}
