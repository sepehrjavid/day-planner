data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # Cloud Run's deterministic URL format. It's derived rather than read from
  # the resource because the internal service also needs the app service's
  # URL as an env var (see internal service comment below), and reading
  # google_cloud_run_v2_service.default.uri here would be a cycle.
  #
  # Verify against the `service_uri` output after the first apply — some older
  # services still get the legacy `-<hash>-` form. If they differ, set
  # var.public_base_url explicitly and re-apply.
  public_base_url = coalesce(
    var.public_base_url,
    "https://${var.service_name}-${data.google_project.this.number}.${var.region}.run.app"
  )

  # The internal service's own URL. Nothing external needs this to match
  # anything — it's only used so the service can correctly identify itself
  # as the audience of incoming internal OIDC tokens (SELF_BASE_URL). Same
  # deterministic-URL caveat as public_base_url above: verify against the
  # `internal_service_uri` output and set var.internal_base_url if they differ.
  internal_url = coalesce(
    var.internal_base_url,
    "https://${var.internal_service_name}-${data.google_project.this.number}.${var.region}.run.app"
  )
}

resource "google_service_account" "backend" {
  project      = var.project_id
  account_id   = "day-planner-backend"
  display_name = "Day Planner backend (OAuth connection service)"
}

# ---------------------------------------------------------------------------
# App surface: health, auth, oauth, me — everything a browser or end user
# calls. Gated by var.publicly_exposed (see the IAM binding below): closed
# during the test phase, one flag flip to open at launch.
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "default" {
  project  = var.project_id
  name     = var.service_name
  location = var.region

  # Ingress stays open regardless of var.publicly_exposed — what actually
  # gates access is the IAM invoker binding below. Google's OAuth server
  # needs to redirect a user's browser to /auth/*/callback with no GCP
  # identity attached at all, so once publicly_exposed is true this is the
  # only thing standing between the internet and every route on this
  # service.
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.backend.email
    timeout         = "900s"

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    containers {
      image = var.app_image

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

      # /me/chat's target. This service's own identity (google_service_account
      # .backend, via google_vertex_ai_reasoning_engine_iam_member.backend_query
      # below) is what's allowed to call it — end users never get credentials
      # capable of invoking Agent Engine directly, which is what makes it safe
      # for this route to resolve user_id from the session token and trust it.
      env {
        name = "AGENT_ENGINE_NAME"
        # .id, not .name: confirmed against an actual apply (see
        # outputs.tf's agent_engine_name) that .name on this resource is
        # just the bare short ID, while .id is the full
        # "projects/{p}/locations/{l}/reasoningEngines/{id}" path — the
        # reverse of google_cloud_run_v2_service. AgentClient's own
        # vertexai.init() call would make the bare ID work too, but the
        # full path is what Settings.agent_engine_name documents, so this
        # stays consistent with that contract instead of relying on it.
        value = google_vertex_ai_reasoning_engine.day_planner_agent.id
      }
      env {
        name  = "AGENT_ENGINE_LOCATION"
        value = var.agent_region
      }
      env {
        # A1.5: the app service's own new /me/habit-sessions/status route
        # calls into the internal service (services/internal_client.py).
        # Same value as the internal service's own SELF_BASE_URL below —
        # that's the audience incoming OIDC tokens are checked against,
        # so the caller's token has to be minted for exactly this.
        name  = "INTERNAL_BACKEND_URL"
        value = local.internal_url
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
    google_secret_manager_secret_iam_member.backend_accessor,
    google_kms_crypto_key_iam_member.backend,
    google_project_iam_member.backend_firestore,
    google_vertex_ai_reasoning_engine_iam_member.backend_query,
  ]

  # var.app_image only ever sets the image on first create — CI deploys new
  # revisions directly with `gcloud run deploy --image`, and this stops
  # terraform apply from reverting that back to whatever's in tfvars.
  # Deliberately narrow: only the image is ignored, so plan still catches
  # real drift on env vars, resources, scaling, and everything else.
  lifecycle {
    ignore_changes = [template[0].containers[0].image]
  }
}

# The single switch for going live. False (the default, test phase) means
# nobody without an explicit roles/run.invoker grant can reach the service —
# not even Google's own OAuth redirect, so the live browser-based OAuth flow
# can't be exercised against this deployment until this flips. Test that
# flow locally instead (uvicorn has no IAM layer in front of it) until
# you're ready to expose everything at once: flip to true and re-apply.
resource "google_cloud_run_v2_service_iam_member" "public" {
  count = var.publicly_exposed ? 1 : 0

  project  = var.project_id
  location = google_cloud_run_v2_service.default.location
  name     = google_cloud_run_v2_service.default.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------------------
# Internal surface: /internal/* only. Unlike the app surface above, this one
# has no launch-day toggle — it's machine-to-machine by nature (it mints
# tokens capable of reading/writing a user's calendar) and is never meant to
# be reachable by an end user's browser, at any point. IAM Conditions cannot
# be combined with allUsers (confirmed against
# https://docs.cloud.google.com/iam/docs/conditions-overview), so there is no
# way to carve this out of the app service's own binding — it has to be its
# own service to stay off the public surface permanently.
#
# A separate image entirely (var.internal_image, built from
# ../day_planner_backend_internal — its own Dockerfile, its own
# requirements.txt with no argon2-cffi). Same service account as the app
# service, since it needs the same GCP permissions (Firestore, KMS, the
# OAuth client secret) to actually refresh and revoke tokens — but no code
# is shared between the two containers at all.
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "internal" {
  project  = var.project_id
  name     = var.internal_service_name
  location = var.region

  # Network-restricted on top of the IAM invoker grant below — belt and
  # suspenders. This requires the Agent Engine runtime to reach this service
  # via a Private Service Connect interface (PSC-I) with a Network
  # Attachment into this project's VPC; Agent Engine has no VPC presence of
  # its own by default, and without PSC-I configured on the agent side, its
  # calls will be rejected here before they ever reach the IAM/OIDC check.
  # See https://docs.cloud.google.com/agent-builder/agent-engine/private-service-connect-interface.
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = false

  template {
    service_account = google_service_account.backend.email
    timeout         = "60s"

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    containers {
      image = var.internal_image

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
        # The app service's URL, not this one's — /internal/connect-link
        # builds a link pointing at /auth/{provider}/start, which lives on
        # the app service.
        name  = "PUBLIC_BASE_URL"
        value = local.public_base_url
      }
      env {
        # This service's own URL — the audience incoming OIDC tokens are
        # checked against. Distinct from PUBLIC_BASE_URL above on purpose.
        name  = "SELF_BASE_URL"
        value = local.internal_url
      }
      env {
        # Comma-separated (see day_planner_backend_internal's
        # Settings.internal_callers). google_service_account.backend is
        # added alongside the agent's own SA for A1.5's user-facing
        # completion route — day_planner_backend_app also runs as
        # `backend` (see service_account above and on the app service
        # earlier in this file), so this is what lets it call
        # /internal/habit-sessions/status on the user's behalf, gated by
        # the internal_invoker_backend IAM binding below.
        name  = "INTERNAL_CALLER_SERVICE_ACCOUNTS"
        value = "${google_service_account.agent.email},${google_service_account.backend.email}"
      }
      env {
        name  = "STATE_TTL_SECONDS"
        value = tostring(var.state_ttl_seconds)
      }
      env {
        name  = "GOOGLE_OAUTH_CLIENT_ID"
        value = var.google_oauth_client_id
      }

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
    google_secret_manager_secret_iam_member.backend_accessor,
    google_kms_crypto_key_iam_member.backend,
    google_project_iam_member.backend_firestore,
  ]

  # var.internal_image only ever sets the image on first create — CI deploys
  # new revisions directly with `gcloud run deploy --image`, and this stops
  # terraform apply from reverting that back to whatever's in tfvars.
  lifecycle {
    ignore_changes = [template[0].containers[0].image]
  }
}

# Never allUsers. google_service_account.agent is the SA agent.tf creates
# for the agent itself, provisioned by this same module.
resource "google_cloud_run_v2_service_iam_member" "internal_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.internal.location
  name     = google_cloud_run_v2_service.internal.name
  role     = "roles/run.invoker"
  member   = google_service_account.agent.member
}

# Added for A1.5: day_planner_backend_app's new user-facing route calls
# /internal/habit-sessions/status on the caller's behalf (after resolving
# and verifying user_id from the session token itself — the internal
# route still trusts whatever user_id the body carries, same as every
# other /internal/* route, so that verification has to happen here, not
# there). google_service_account.backend is what both the app and
# internal services already run as (see service_account = ... on each
# google_cloud_run_v2_service above) — this grant is what lets that same
# identity, acting as a caller rather than as the internal service
# itself, actually invoke it. Paired with the
# INTERNAL_CALLER_SERVICE_ACCOUNTS env var above, which is the
# application-level allowlist require_internal_caller checks in addition
# to this IAM layer.
resource "google_cloud_run_v2_service_iam_member" "internal_invoker_backend" {
  project  = var.project_id
  location = google_cloud_run_v2_service.internal.location
  name     = google_cloud_run_v2_service.internal.name
  role     = "roles/run.invoker"
  member   = google_service_account.backend.member
}
