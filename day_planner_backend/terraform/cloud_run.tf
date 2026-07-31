data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # Cloud Run's deterministic URL format. It's derived rather than read from
  # the resource because the service needs its own public URL as an env var
  # (for the OAuth redirect_uri and the internal-token audience), and reading
  # google_cloud_run_v2_service.default.uri here would be a cycle.
  #
  # Verify against the `service_uri` output after the first apply — some older
  # services still get the legacy `-<hash>-` form. If they differ, set
  # var.public_base_url explicitly and re-apply.
  public_base_url = coalesce(
    var.public_base_url,
    "https://${var.service_name}-${data.google_project.this.number}.${var.region}.run.app"
  )
}

resource "google_service_account" "backend" {
  project      = var.project_id
  account_id   = "day-planner-backend"
  display_name = "Day Planner backend (OAuth connection service)"
}

resource "google_project_iam_member" "backend_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.backend.email}"
}

resource "google_cloud_run_v2_service" "default" {
  project  = var.project_id
  name     = var.service_name
  location = var.region

  # Google's OAuth servers redirect a user's browser to /auth/*/callback, so
  # the service has to accept anonymous ingress. The /internal/* routes are
  # therefore protected in application code by verifying a Google-signed OIDC
  # token (app/security.py) rather than by Cloud Run IAM, which is service-wide
  # and cannot exempt a single public route.
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.backend.email
    timeout         = "60s"

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      ports {
        container_port = 8080
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = "(default)"
      }
      env {
        name  = "KMS_KEY_NAME"
        value = google_kms_crypto_key.oauth_tokens.id
      }
      env {
        name  = "PUBLIC_BASE_URL"
        value = local.public_base_url
      }
      env {
        name  = "INTERNAL_CALLER_SERVICE_ACCOUNTS"
        value = var.agent_service_account_email
      }
      env {
        name  = "STATE_TTL_SECONDS"
        value = tostring(var.state_ttl_seconds)
      }
      env {
        name  = "GOOGLE_OAUTH_CLIENT_ID"
        value = var.google_oauth_client_id
      }

      # Mounted from Secret Manager at start-up rather than baked into the
      # service config, so the value never appears in Terraform state.
      env {
        name = "GOOGLE_OAUTH_CLIENT_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.google_oauth_client_secret.secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 3
        period_seconds        = 3
        failure_threshold     = 10
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.backend_accessor,
    google_kms_crypto_key_iam_member.backend,
  ]
}

# Anonymous invocation is a requirement, not an oversight — see the ingress
# comment above. Authorization for the privileged routes lives in the app.
resource "google_cloud_run_v2_service_iam_member" "public" {
  project  = var.project_id
  location = google_cloud_run_v2_service.default.location
  name     = google_cloud_run_v2_service.default.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
