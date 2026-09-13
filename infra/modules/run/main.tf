# Compute: one Cloud Run worker pool per Temporal task queue (same image, SWESIM_TASK_QUEUE differs) and the console
# service behind Cloud Run's native IAP. Trial Jobs are created at runtime by the launcher, not here.
# Env names follow docs/superpowers/specs/2026-09-13-interfaces.md §9; empty values are omitted so Settings sees "unset".
locals {
  common_env = { for k, v in merge(
    {
      SWESIM_WORKSPACE              = "/workspace"
      SWESIM_REGISTRY               = "/workspace/registry.json"
      SWESIM_BUCKET                 = var.bucket
      SWESIM_TRIAL_LAUNCHER         = "cloudrun"
      SWESIM_ORIGIN                 = "cloudrun"
      SWESIM_GCP_PROJECT            = var.project
      SWESIM_GCP_REGION             = var.region
      SWESIM_WORKER_IMAGE           = var.image
      SWESIM_WORKER_SERVICE_ACCOUNT = var.worker_service_account
      SWESIM_SQL_CONNECTION_NAME    = var.sql_connection_name
      SWESIM_EVALUATORS             = join(",", var.evaluators)
      SWESIM_STAKEHOLDER_URL        = var.stakeholder_url
      SWESIM_CONSOLE_URL            = var.console_url
      SWESIM_IAP_AUDIENCE           = var.iap_audience
      TEMPORAL_ADDRESS              = var.temporal_address
      TEMPORAL_NAMESPACE            = var.temporal_namespace
    },
    var.image_digest != "" ? { SWESIM_WORKER_IMAGE_DIGEST = var.image_digest } : {},
    var.spend_cap_usd != null ? { SWESIM_SPEND_CAP_USD = tostring(var.spend_cap_usd) } : {},
  ) : k => v if v != "" }

  worker_secret_env  = ["TEMPORAL_API_KEY", "SWESIM_WORKER_SECRET", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GH_TOKEN", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "SWESIM_DATABASE_URL"]
  console_secret_env = ["TEMPORAL_API_KEY", "SWESIM_WORKER_SECRET", "SWESIM_DATABASE_URL"] # no provider keys on the console

  # The image runs as uid/gid 1000 (Dockerfile.worker); the FUSE mount must present files to that user.
  gcs_mount_options = jsondecode(file("${path.module}/../../../swesim/storage/mount_options.json"))
}

resource "google_cloud_run_v2_worker_pool" "pool" {
  for_each            = var.pools
  project             = var.project
  location            = var.region
  name                = each.value.queue
  deletion_protection = false
  labels              = { app = "swesim", queue = each.value.queue }

  scaling {
    manual_instance_count = each.value.instances
  }

  template {
    service_account = var.worker_service_account
    labels          = { app = "swesim", queue = each.value.queue }

    containers {
      name  = "worker"
      image = var.image
      resources {
        limits = { cpu = var.worker_cpu, memory = var.worker_memory }
      }
      dynamic "env" {
        for_each = merge(local.common_env, { SWESIM_TASK_QUEUE = each.value.queue })
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = toset(local.worker_secret_env)
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = var.secret_ids[env.key]
              version = "latest"
            }
          }
        }
      }
      volume_mounts {
        name       = "workspace"
        mount_path = "/workspace"
      }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
    }

    volumes {
      name = "workspace"
      gcs {
        bucket        = var.bucket
        read_only     = false
        mount_options = local.gcs_mount_options
      }
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [var.sql_connection_name]
      }
    }
  }
}

# Operator maintenance uses the same image and mounted record as the workers. The default migrates the index;
# `gcloud run jobs execute ... --args=rebuild,--origin=vm-systemd` imports the mirrored legacy record afterwards.
resource "google_cloud_run_v2_job" "index_maintenance" {
  project             = var.project
  location            = var.region
  name                = "swesim-index-maintenance"
  deletion_protection = false
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = var.worker_service_account
      max_retries     = 0
      timeout         = "3600s"
      containers {
        image   = var.image
        command = ["python", "-m", "swesim.index.cli"]
        args    = ["migrate"]
        resources { limits = { cpu = "1", memory = "2Gi" } }
        dynamic "env" {
          for_each = local.common_env
          content {
            name  = env.key
            value = env.value
          }
        }
        env {
          name = "SWESIM_DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = var.secret_ids["SWESIM_DATABASE_URL"]
              version = "latest"
            }
          }
        }
        volume_mounts {
          name       = "workspace"
          mount_path = "/workspace"
        }
        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }
      volumes {
        name = "workspace"
        gcs {
          bucket        = var.bucket
          read_only     = false
          mount_options = local.gcs_mount_options
        }
      }
      volumes {
        name = "cloudsql"
        cloud_sql_instance { instances = [var.sql_connection_name] }
      }
    }
  }
}

resource "google_cloud_run_v2_service" "console" {
  project             = var.project
  location            = var.region
  name                = var.console_name
  ingress             = "INGRESS_TRAFFIC_ALL"
  iap_enabled         = true
  deletion_protection = false
  labels              = { app = "swesim" }

  template {
    service_account                  = var.worker_service_account
    timeout                          = "300s"
    max_instance_request_concurrency = 40
    labels                           = { app = "swesim" }

    scaling {
      min_instance_count = var.console_min_instances
      max_instance_count = var.console_max_instances
    }

    containers {
      name    = "console"
      image   = var.image
      command = ["uvicorn"]
      args    = ["swesim.dashboard.app:app", "--host", "0.0.0.0", "--port", "8080"]
      ports {
        container_port = 8080
      }
      resources {
        limits            = { cpu = "1", memory = "1Gi" }
        cpu_idle          = true
        startup_cpu_boost = true
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = toset(local.console_secret_env)
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = var.secret_ids[env.key]
              version = "latest"
            }
          }
        }
      }
      volume_mounts {
        name       = "workspace"
        mount_path = "/workspace"
      }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
    }

    volumes {
      name = "workspace"
      gcs {
        bucket        = var.bucket
        read_only     = true
        mount_options = local.gcs_mount_options
      }
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [var.sql_connection_name]
      }
    }
  }
}

# IAP fronts the service: the IAP service agent invokes it, the allow-listed people pass IAP.
resource "google_cloud_run_v2_service_iam_member" "iap_invoker" {
  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.console.name
  role     = "roles/run.invoker"
  member   = var.iap_service_agent_member
}

resource "google_iap_web_cloud_run_service_iam_member" "members" {
  for_each               = toset(var.iap_members)
  project                = var.project
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.console.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.key
}
