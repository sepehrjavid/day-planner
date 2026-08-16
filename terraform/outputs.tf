output "service_uri" {
  description = <<-EOT
    The URL Cloud Run actually assigned to the app service. Compare against
    public_base_url below: if they differ, set var.public_base_url to this
    and re-apply, otherwise every OAuth redirect will fail with
    redirect_uri_mismatch.
  EOT
  value       = google_cloud_run_v2_service.default.uri
}

output "internal_service_uri" {
  description = <<-EOT
    The URL Cloud Run actually assigned to the internal service. Compare
    against var.internal_base_url — if they differ, set it explicitly and
    re-apply, otherwise SELF_BASE_URL won't match what the agent's OIDC
    tokens are actually audienced to. This is also the value to give the
    agent's identity provisioning as the invocation target.
  EOT
  value       = google_cloud_run_v2_service.internal.uri
}

output "public_base_url" {
  description = "What the service was told its own origin is."
  value       = local.public_base_url
}

output "oauth_redirect_uri" {
  description = "Register this verbatim on the OAuth client's Authorized redirect URIs."
  value       = "${local.public_base_url}/auth/google/callback"
}

output "backend_service_account" {
  description = "Identity the service runs as."
  value       = google_service_account.backend.email
}

output "agent_engine_name" {
  description = "Value for the AGENT_ENGINE_NAME env var when running the app service locally."
  # .name is just the short numeric ID; config.py's agent_engine_name wants
  # the full "projects/{p}/locations/{l}/reasoningEngines/{id}" path, which
  # is .id on this resource (confirmed against an actual apply's plan
  # output, not assumed).
  value = google_vertex_ai_reasoning_engine.day_planner_agent.id
}

output "kms_key_name" {
  description = "Value for the KMS_KEY_NAME env var when running locally."
  value       = google_kms_crypto_key.oauth_tokens.id
}

output "artifact_registry_repo" {
  description = "Base push target — append /backend-app or /backend-internal:tag."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.backend.repository_id}"
}

output "app_image_push_target" {
  description = "Full push target for the app service image (../day_planner_backend_app)."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.backend.repository_id}/backend-app"
}

output "internal_image_push_target" {
  description = "Full push target for the internal service image (../day_planner_backend_internal)."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.backend.repository_id}/backend-internal"
}

output "turn_logs_dataset" {
  description = <<-EOT
    BigQuery dataset turn_log.py's records land in (see bigquery.tf). The
    table itself is created lazily by the log sink on its first matching
    delivery, not at apply time — find its actual name via `bq ls
    day_planner_turns` or the console once a turn has been logged, and use
    it in place of {{TABLE}} in
    day_planner_backend_app/app/services/turn_log_queries.sql.
  EOT
  value       = google_bigquery_dataset.turn_logs.dataset_id
}
