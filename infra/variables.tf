variable "project" {
  type        = string
  description = "GCP project id that hosts the stack."
}

variable "region" {
  type        = string
  description = "Region for Cloud Run, Cloud SQL, Artifact Registry and the workspace bucket."
  default     = "us-central1"
}

variable "image" {
  type        = string
  description = "Worker image pinned by digest (<artifact_repo>/worker@sha256:...). Empty = <artifact_repo>/worker:latest, for the bootstrap apply only."
  default     = ""
  validation {
    condition     = var.image == "" || can(regex("@sha256:[0-9a-f]{64}$", var.image))
    error_message = "image must be empty or a digest reference ending in @sha256:<64 hex chars>."
  }
}

variable "iap_members" {
  type        = list(string)
  description = "IAM members allowed through IAP to the console, e.g. [\"user:operator@example.com\"]."
  default     = []
}

variable "evaluators" {
  type        = list(string)
  description = "Google account emails that may see evaluator-only material in the console (SWESIM_EVALUATORS)."
  default     = []
}

variable "temporal_address" {
  type        = string
  description = "Temporal Cloud gRPC endpoint, e.g. us-central1.gcp.api.temporal.io:7233."
  default     = ""
}

variable "temporal_namespace" {
  type        = string
  description = "Temporal Cloud namespace, e.g. swesim.abcde."
  default     = ""
}

variable "stakeholder_url" {
  type        = string
  description = "Base URL of the stakeholder service on Modal (SWESIM_STAKEHOLDER_URL)."
  default     = ""
}

variable "spend_cap_usd" {
  type        = number
  description = "SWESIM_SPEND_CAP_USD for the workers; null leaves it unset."
  default     = null
  nullable    = true
}

variable "iap_programmatic_clients" {
  type        = list(string)
  default     = []
  description = "OAuth client IDs allowed to authenticate programmatic console requests; the IAP user access policy still applies."
}

variable "pool_instances" {
  type = object({
    authoring = number
    build     = number
    trials    = number
    control   = number
  })
  description = "Fixed instance count per worker pool (one pool per Temporal task queue)."
  default     = { authoring = 2, build = 2, trials = 1, control = 1 }
}

variable "github_repository" {
  type        = string
  description = "owner/name of the GitHub repository allowed to deploy through Workload Identity Federation (`git remote get-url origin`). Empty = no repository is trusted."
  default     = ""
}

variable "deploy_workloads" {
  type        = bool
  description = "false only for the bootstrap apply: everything except the Cloud Run pools/console/logging, which need pushed secret versions and a built image."
  default     = true
}

variable "bucket_name" {
  type        = string
  description = "Workspace bucket name (the record). Empty = swesim-<project>-workspace."
  default     = ""
}

variable "bucket_force_destroy" {
  type        = bool
  description = "Allow `terraform destroy` to delete the workspace bucket with its objects (make cloud-destroy PURGE=1)."
  default     = false
}

variable "sql_tier" {
  type        = string
  description = "Cloud SQL machine tier for the index."
  default     = "db-custom-2-7680"
}

variable "sql_deletion_protection" {
  type        = bool
  description = "Terraform-side deletion protection for the Cloud SQL instance."
  default     = false
}

variable "console_min_instances" {
  type    = number
  default = 1
}

variable "console_max_instances" {
  type    = number
  default = 3
}

variable "worker_cpu" {
  type        = string
  description = "CPU limit per worker pool instance (Harbor is a client; the heavy work runs on Modal)."
  default     = "1"
}

variable "worker_memory" {
  type    = string
  default = "2Gi"
}

variable "notification_channels" {
  type        = list(string)
  description = "Monitoring notification channel ids for the worker crash-loop alert (empty = alert exists, nobody is paged)."
  default     = []
}
