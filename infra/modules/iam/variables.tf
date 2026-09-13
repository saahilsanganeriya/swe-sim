variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "workspace_bucket" {
  type = string
}

variable "tfstate_bucket" {
  type = string
}

variable "cloudbuild_bucket" {
  type = string
}

variable "artifact_repo_id" {
  type = string
}

variable "secret_ids" {
  type = map(string)
}

variable "deployer_secret_names" {
  type = list(string)
}

variable "github_repository" {
  type = string
}
