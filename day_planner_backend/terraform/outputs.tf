output "service_uri" {
  description = <<-EOT
    The URL Cloud Run actually assigned. Compare against public_base_url below:
    if they differ, set var.public_base_url to this and re-apply, otherwise
    every OAuth redirect will fail with redirect_uri_mismatch.
  EOT
  value       = google_cloud_run_v2_service.default.uri
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

output "kms_key_name" {
  description = "Value for the KMS_KEY_NAME env var when running locally."
  value       = google_kms_crypto_key.oauth_tokens.id
}

output "artifact_registry_repo" {
  description = "Push target for the container image."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.backend.repository_id}"
}
