# The OAuth *client* secret — one per provider, not one per user. Per-user
# refresh tokens live in Firestore under KMS encryption; Secret Manager is
# priced and quota'd per secret and is the wrong shape for per-user material.
resource "google_secret_manager_secret" "google_oauth_client_secret" {
  project   = var.project_id
  secret_id = "day-planner-google-oauth-client-secret"

  replication {
    auto {}
  }
}

# The value is set out of band so it never lands in state or in git:
#   printf '%s' "$CLIENT_SECRET" | gcloud secrets versions add \
#     day-planner-google-oauth-client-secret --data-file=-

resource "google_secret_manager_secret_iam_member" "backend_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.google_oauth_client_secret.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.backend.email}"
}

# SendGrid Mail Send API key (A6.4) — password-reset emails only. Create it
# as a Restricted Access key scoped to Mail Send alone; never a Full Access
# key, which also grants account and billing-level actions this service has
# no business holding.
resource "google_secret_manager_secret" "sendgrid_api_key" {
  project   = var.project_id
  secret_id = "day-planner-sendgrid-api-key"

  replication {
    auto {}
  }
}

# The value is set out of band so it never lands in state or in git, same as
# google_oauth_client_secret above:
#   printf '%s' "$SENDGRID_API_KEY" | gcloud secrets versions add \
#     day-planner-sendgrid-api-key --data-file=- --project=<var.project_id>

resource "google_secret_manager_secret_iam_member" "backend_sendgrid_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.sendgrid_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.backend.email}"
}
