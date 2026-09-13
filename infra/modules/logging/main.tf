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

resource "google_logging_metric" "worker_crashes" {
  project     = var.project
  name        = "swesim/worker_crashes"
  description = "Worker pool containers that exited (crash loop signal)."
  filter      = <<-EOT
    resource.type="cloud_run_worker_pool"
    resource.labels.worker_pool_name=~"^swesim-"
    severity>=ERROR
    (textPayload:"Container terminated" OR textPayload:"exited with" OR textPayload:"exit(")
  EOT

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

  conditions {
    display_name = "more than 3 worker container exits in 10 minutes"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.worker_crashes.name}\" AND resource.type=\"cloud_run_worker_pool\""
      comparison      = "COMPARISON_GT"
      threshold_value = 3
      duration        = "0s"
      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.worker_pool_name"]
      }
      trigger {
        count = 1
      }
    }
  }

  notification_channels = var.notification_channels
  alert_strategy {
    auto_close = "1800s"
  }
  documentation {
    content = "A swesim worker pool is restarting repeatedly. Check the pool's logs in Cloud Logging; the usual causes are a missing secret version or a Temporal Cloud auth failure. Trials in flight are reconciled, never relaunched."
  }
}
