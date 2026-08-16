# Turn-record telemetry (day_planner_backend_app/app/services/turn_log.py,
# see docs/roadmaps/1-agent.md A1.1/A1.2). Token spend, turn latency, and
# tool error rates were previously unmeasured entirely. Cloud Run's stdout
# is shared across everything the app service prints, so the sink's filter
# below — not the dataset or table — is what actually narrows this to turn
# records; nothing else the service logs is duplicated here.
#
# Requires bigquery.googleapis.com already enabled — owned by the same
# "another layer" main.tf's top comment describes for run/firestore/etc.

resource "google_bigquery_dataset" "turn_logs" {
  project    = var.project_id
  dataset_id = "day_planner_turns"
  location   = var.region

  # 90 days: comfortably covers A1.2's "queryable for the trailing 30 days"
  # acceptance criterion with room for slower investigation, without
  # keeping per-turn data forever. This is a dataset-default, not
  # per-table — revisit once A1.4/A3.4 add more tables here with their own
  # retention needs.
  default_table_expiration_ms = 90 * 24 * 60 * 60 * 1000
}

resource "google_logging_project_sink" "turn_logs" {
  project     = var.project_id
  name        = "day-planner-turn-logs"
  destination = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.turn_logs.dataset_id}"

  # jsonPayload.turn_id:* is the actual selector — every turn_log.py record
  # has one, nothing else the app service logs does. Restricted to the app
  # service specifically (var.service_name): turn_log.py only ever runs
  # there, never on the internal service.
  filter = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${var.service_name}"
    jsonPayload.turn_id:*
  EOT

  bigquery_options {
    # A single date-partitioned table, not one table per day — BigQuery's
    # current recommendation over legacy date-sharded tables, and what
    # every query in docs/turn-log-queries.sql assumes ($PARTITIONTIME /
    # _PARTITIONDATE, not a wildcard table suffix).
    use_partitioned_tables = true
  }

  # Writes as its own identity rather than the backend service account, so
  # a compromised backend SA can't also tamper with exported log history.
  # Needs bigquery.dataEditor on the dataset, granted below.
  unique_writer_identity = true
}

resource "google_bigquery_dataset_iam_member" "turn_logs_sink_writer" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.turn_logs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.turn_logs.writer_identity
}
