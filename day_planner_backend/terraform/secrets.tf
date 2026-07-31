# The OAuth *client* secret — one per provider, not one per user. Per-user
# refresh tokens live in Firestore under KMS encryption; Secret Manager is
# priced and quota'd per secret and is the wrong shape for per-user material.
resource "google_secret_manager_secret" "google_oauth_client_secret" {
  project   = var.project_id
  secret_id = "day-planner-google-oauth-client-secret"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
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
