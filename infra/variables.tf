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

# Optional GitHub App for repository discovery: one more 5,000/h quota bucket next to GH_TOKEN (docs/setup.md, "GitHub
# App"). Both ids or neither; the App's private key is the GH_APP_PRIVATE_KEY secret, pushed by `make push-secrets`, and
# is mounted on the worker pools only when the App is configured, so a stack without an App never references it.
variable "gh_app_id" {
  type        = string
  description = "GitHub App id (GH_APP_ID) for discovery; null = no App."
  default     = null
  nullable    = true
}

variable "gh_app_installation_id" {
  type        = string
  description = "Installation id of that App on the mined account (GH_APP_INSTALLATION_ID); null = no App."
  default     = null
  nullable    = true
  validation {
    condition     = (var.gh_app_id == null) == (var.gh_app_installation_id == null)
    error_message = "gh_app_id and gh_app_installation_id must be set together (or both left null)."
  }
}

variable "spend_cap_usd" {
  type        = number
  description = "SWESIM_SPEND_CAP_USD for the workers; null leaves it unset."
  default     = null
  nullable    = true
}

# Programmatic (non-browser) console access through IAP is an explicit opt-in, ORGANIZATION OAUTH CLIENTS ONLY: IAP accepts
# a client only when the project that owns it is in the same organization as this project, so list clients created in
# this project (<project_number>-<id>.apps.googleusercontent.com) or in another project of the organization whose number
# is in `iap_programmatic_client_projects`. Google's public clients (the gcloud SDK's 32555940559 and 764086051850
# clients, Google's own apps) fail with "not in the same organization as resource"; the run module refuses them at plan
# time (a precondition) instead of failing the apply. Empty = browser sign-in only; `iap_members` still gates every request.
variable "iap_programmatic_clients" {
  type        = list(string)
  default     = []
  description = "Opt-in: organization-owned OAuth client IDs whose tokens IAP accepts for programmatic console requests (see the comment above). Empty by default."
  validation {
    condition     = alltrue([for c in var.iap_programmatic_clients : can(regex("^[0-9]+-[a-z0-9]+\\.apps\\.googleusercontent\\.com$", c))])
    error_message = "Every iap_programmatic_clients entry must be an OAuth client created in a GCP project, <project_number>-<id>.apps.googleusercontent.com; Google's public clients such as 32555940559.apps.googleusercontent.com are not organization OAuth clients and IAP rejects them."
  }
}

variable "iap_programmatic_client_projects" {
  type        = list(string)
  default     = []
  description = "Project numbers, in this project's organization, whose OAuth clients may appear in iap_programmatic_clients besides this project's own. Never list a Google-owned project number (32555940559, 764086051850)."
  validation {
    condition     = alltrue([for n in var.iap_programmatic_client_projects : can(regex("^[0-9]+$", n))])
    error_message = "iap_programmatic_client_projects holds project numbers (digits only)."
  }
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
