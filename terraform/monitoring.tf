# Alerting on turn_log.py's records (A1.1/A1.2) — see
# docs/roadmaps/1-agent.md A1.3. Two things were previously invisible: a
# runaway tool loop (expensive and currently undetectable) and a preload
# failure (A0.2 made it fail closed instead of open, but nothing noticed
# when it actually happened). Both alert to the same channel; latency and
# cost alerts deliberately wait for A1.2's baselines, not added here.

resource "google_monitoring_notification_channel" "alert_email" {
  project      = var.project_id
  display_name = "Day Planner alerts"
  type         = "email"
  labels = {
    email_address = var.alert_notification_email
  }
}

# turn_log.py sets loop_detected=true (and logs at WARNING) when the same
# tool is called with the same arguments (by fingerprint — see
# turn_log._fingerprint_args) three or more times in one turn. The boolean
# field is what this filters on; WARNING severity is belt and suspenders
# for visibility in the Cloud Logging UI, not load-bearing here.
resource "google_logging_metric" "loop_detected" {
  project = var.project_id
  name    = "day_planner_loop_detected"
  filter  = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${var.service_name}"
    jsonPayload.loop_detected=true
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "loop_detected" {
  project      = var.project_id
  display_name = "Day Planner: tool call loop detected"
  combiner     = "OR"

  conditions {
    display_name = "loop_detected log entries > 0"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.loop_detected.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      # Fire on the first occurrence rather than waiting to see a sustained
      # rate — a single loop is already a real, billed bug.
      duration = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_COUNT"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.alert_email.id]

  documentation {
    content   = <<-EOT
      A single turn issued the same tool with the same arguments 3+ times.
      Find it with turn_log_queries.sql (day_planner_backend_app/app/services/)
      query 6, using the turn_id from the log entry that triggered this —
      or query jsonPayload.tool_calls where args_fingerprint repeats.
    EOT
    mime_type = "text/markdown"
  }
}

# preload_ok is written by day_planner_agent's before_agent_callbacks (see
# agent.py's _PRELOAD_OK_KEY) and passed through by turn_log.py. false
# means a guardrail (zones/sleep or profile) failed to load for that turn —
# A0.2 made the agent fail closed instead of proceeding as though the user
# has none, but that's only safe if someone actually notices it happened.
resource "google_logging_metric" "preload_failed" {
  project = var.project_id
  name    = "day_planner_preload_failed"
  filter  = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${var.service_name}"
    jsonPayload.preload_ok=false
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "preload_failed" {
  project      = var.project_id
  display_name = "Day Planner: preload failure"
  combiner     = "OR"

  conditions {
    display_name = "preload_ok=false log entries > 0"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.preload_failed.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_COUNT"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.alert_email.id]

  documentation {
    content   = <<-EOT
      A turn's zones/sleep or profile guardrails failed to load from the
      internal backend (see agent.py's _preload_profile/_preload_zones,
      A0.2). The agent should have failed closed rather than scheduling as
      though the user has no constraints — confirm that held, and check
      day_planner_backend_internal for what's actually failing.
    EOT
    mime_type = "text/markdown"
  }
}
