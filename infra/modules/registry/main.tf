# Artifact Registry repository for the worker image (`<region>-docker.pkg.dev/<project>/swesim/worker`).
resource "google_artifact_registry_repository" "swesim" {
  project       = var.project
  location      = var.region
  repository_id = "swesim"
  format        = "DOCKER"
  description   = "swesim worker/console images"

  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 20
    }
  }
  cleanup_policies {
    id     = "delete-untagged-after-30d"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "2592000s"
    }
  }
}
