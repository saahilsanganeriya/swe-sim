# Observability, kept small: a log-based metric for TrialWorkflow reconcile decisions (expected only across worker
# restarts) and an alert when worker pool containers exit repeatedly.
resource "google_logging_metric" "trial_reconcile" {
  project     = var.project
  name        = "swesim/trial_reconcile"
  description = "TrialWorkflow launch decisions that reconciled an existing trial instead of launching a new one."
  filter      = <<-EOT
    resource.type="cloud_run_worker_pool"
    resource.labels.worker_pool_name="swesim-trials"
    (textPayload:"decision=reconcile" OR jsonPayload.decision="reconcile")
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

locals {
  crash_filter = <<-EOT
    resource.type="cloud_run_worker_pool"
    resource.labels.worker_pool_name=~"^swesim-"
    severity>=ERROR
    (textPayload:"Container terminated" OR textPayload:"exited with" OR textPayload:"exit(")
  EOT
}

resource "google_logging_metric" "worker_crashes" {
  project     = var.project
  name        = "swesim/worker_crashes"
  description = "Worker pool containers that exited (crash loop signal)."
  filter      = local.crash_filter

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key        = "worker_pool"
      value_type = "STRING"
    }
  }
  label_extractors = {
    worker_pool = "EXTRACT(resource.labels.worker_pool_name)"
  }
}

resource "google_monitoring_alert_policy" "crash_loop" {
  project      = var.project
  display_name = "swesim worker crash loop"
  combiner     = "OR"

  # A log-match condition rather than a threshold on the user metric: the metric type is not queryable for minutes
  # after creation, which made a fresh bootstrap fail; matched-log alerts need no metric and fire on the first exit.
  conditions {
    display_name = "a swesim worker pool container exited"
    condition_matched_log {
      filter = local.crash_filter
      label_extractors = {
        worker_pool = "EXTRACT(resource.labels.worker_pool_name)"
      }
    }
  }

  notification_channels = var.notification_channels
  alert_strategy {
    auto_close = "1800s"
    notification_rate_limit {
      period = "1800s"
    }
  }
  documentation {
    content = "A swesim worker pool is restarting repeatedly. Check the pool's logs in Cloud Logging; the usual causes are a missing secret version or a Temporal Cloud auth failure. Trials in flight are reconciled, never relaunched."
  }
}
