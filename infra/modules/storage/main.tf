# The record: one versioned bucket mounted at /workspace on the pools, the trial Jobs (read-write) and the console
# (read-only). Object versioning is the snapshot mechanism (brief §2.3); noncurrent versions are pruned by lifecycle.
resource "google_storage_bucket" "workspace" {
  project                     = var.project
  name                        = var.bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.force_destroy

  versioning {
    enabled = true
  }
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      with_state         = "ARCHIVED"
      num_newer_versions = 5
    }
  }
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      with_state                 = "ARCHIVED"
      days_since_noncurrent_time = 30
    }
  }
}

# `gcloud builds submit` stages sources in gs://<project>_cloudbuild by default; owning it here lets Terraform grant the
# deployer access and expire old sources.
resource "google_storage_bucket" "cloudbuild" {
  project                     = var.project
  name                        = "${var.project}_cloudbuild"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 7
    }
  }
}
