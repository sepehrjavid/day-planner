# Vertex AI Agent Engine (Reasoning Engine) deployment for day_planner_agent.
#
# google_vertex_ai_reasoning_engine only exists in the google-beta provider
# (confirmed against the actual provider schema, not assumed) — this is
# genuinely newer, less-trodden ground than the rest of this module. Two
# things are worth knowing before touching this file:
#
# 1. class_methods below must be kept in sync with ADK by hand. The provider
#    normally lets the ADK/Vertex AI SDK auto-populate this by introspecting
#    the installed ADK version at deploy time; deploying via Terraform means
#    this list is a static snapshot instead. It's transcribed here from the
#    live ADK source (https://github.com/google/adk-python/blob/main/src/google/adk/cli/cli_deploy.py,
#    verified against source directly — the Terraform provider's own docs
#    example for this field is already stale, missing
#    async_add_session_to_memory/async_search_memory). Re-verify against that
#    file whenever requirements.txt's google-adk pin changes.
# 2. day_planner_agent's package layout (day_planner_agent/__init__.py doing
#    `from . import agent`) means the source archive keeps day_planner_agent/
#    as a real subdirectory rather than flattening its contents to the
#    archive root — entrypoint_module is set explicitly to
#    "day_planner_agent.agent" rather than relying on the "agent" default.

locals {

  # Transcribed from adk-python's _AGENT_ENGINE_CLASS_METHODS — see file
  # header. Descriptions are trimmed for readability; names, api_mode, and
  # parameter schemas are what the API actually dispatches on and are kept
  # exact.
  agent_class_methods = [
    {
      name        = "get_session"
      api_mode    = ""
      description = "Deprecated. Use async_get_session instead. Get a session for the given user."
      parameters = {
        type     = "object"
        required = ["user_id", "session_id"]
        properties = {
          user_id    = { type = "string" }
          session_id = { type = "string" }
        }
      }
    },
    {
      name        = "list_sessions"
      api_mode    = ""
      description = "Deprecated. Use async_list_sessions instead. List sessions for the given user."
      parameters = {
        type       = "object"
        required   = ["user_id"]
        properties = { user_id = { type = "string" } }
      }
    },
    {
      name        = "create_session"
      api_mode    = ""
      description = "Deprecated. Use async_create_session instead. Creates a new session."
      parameters = {
        type     = "object"
        required = ["user_id"]
        properties = {
          user_id     = { type = "string" }
          session_id  = { type = "string", nullable = true }
          state       = { type = "object", nullable = true }
          ttl         = { type = "string", nullable = true }
          expire_time = { type = "string", nullable = true }
        }
      }
    },
    {
      name        = "delete_session"
      api_mode    = ""
      description = "Deprecated. Use async_delete_session instead. Deletes a session for the given user."
      parameters = {
        type     = "object"
        required = ["user_id", "session_id"]
        properties = {
          user_id    = { type = "string" }
          session_id = { type = "string" }
        }
      }
    },
    {
      name        = "async_get_session"
      api_mode    = "async"
      description = "Get a session for the given user. Returns None if not found."
      parameters = {
        type     = "object"
        required = ["user_id", "session_id"]
        properties = {
          user_id    = { type = "string" }
          session_id = { type = "string" }
        }
      }
    },
    {
      name        = "async_list_sessions"
      api_mode    = "async"
      description = "List sessions for the given user."
      parameters = {
        type       = "object"
        required   = ["user_id"]
        properties = { user_id = { type = "string" } }
      }
    },
    {
      name        = "async_create_session"
      api_mode    = "async"
      description = "Creates a new session."
      parameters = {
        type     = "object"
        required = ["user_id"]
        properties = {
          user_id     = { type = "string" }
          session_id  = { type = "string", nullable = true }
          state       = { type = "object", nullable = true }
          ttl         = { type = "string", nullable = true }
          expire_time = { type = "string", nullable = true }
        }
      }
    },
    {
      name        = "async_delete_session"
      api_mode    = "async"
      description = "Deletes a session for the given user."
      parameters = {
        type     = "object"
        required = ["user_id", "session_id"]
        properties = {
          user_id    = { type = "string" }
          session_id = { type = "string" }
        }
      }
    },
    {
      name        = "async_add_session_to_memory"
      api_mode    = "async"
      description = "Generates memories from a completed session for Memory Bank."
      parameters = {
        type       = "object"
        required   = ["session"]
        properties = { session = { type = "object", additionalProperties = true } }
      }
    },
    {
      name        = "async_search_memory"
      api_mode    = "async"
      description = "Searches Memory Bank memories for the given user."
      parameters = {
        type     = "object"
        required = ["user_id", "query"]
        properties = {
          user_id = { type = "string" }
          query   = { type = "string" }
        }
      }
    },
    {
      name        = "stream_query"
      api_mode    = "stream"
      description = "Deprecated. Use async_stream_query instead. Streams responses in response to a message."
      parameters = {
        type     = "object"
        required = ["message", "user_id"]
        properties = {
          message = {
            anyOf = [
              { type = "string" },
              { type = "object", additionalProperties = true },
            ]
          }
          user_id    = { type = "string" }
          session_id = { type = "string", nullable = true }
          run_config = { type = "object", nullable = true }
        }
      }
    },
    {
      name        = "async_stream_query"
      api_mode    = "async_stream"
      description = "Streams responses asynchronously from the ADK application."
      parameters = {
        type     = "object"
        required = ["message", "user_id"]
        properties = {
          message = {
            anyOf = [
              { type = "string" },
              { type = "object", additionalProperties = true },
            ]
          }
          user_id        = { type = "string" }
          session_id     = { type = "string", nullable = true }
          session_events = { type = "array", items = { type = "object" }, nullable = true }
          run_config     = { type = "object", nullable = true }
        }
      }
    },
    {
      name        = "streaming_agent_run_with_events"
      api_mode    = "async_stream"
      description = "Streams responses asynchronously; primarily for invocation from AgentSpace. Prefer async_stream_query."
      parameters = {
        type       = "object"
        required   = ["request_json"]
        properties = { request_json = { type = "string" } }
      }
    },
  ]
}

# ---------------------------------------------------------------------------
# The agent's own runtime identity.
# ---------------------------------------------------------------------------

resource "google_service_account" "agent" {
  project      = var.project_id
  account_id   = "day-planner-agent"
  display_name = "Day Planner agent (Vertex AI Agent Engine runtime)"
}

# Per google_vertex_ai_reasoning_engine's own docs: the artifact's runtime
# identity needs these two for reading its packaged source from Cloud
# Storage and for using Vertex AI (model calls, Memory Bank). Both are
# project-scoped — Vertex AI doesn't expose a narrower IAM Condition
# equivalent to Firestore's database-scoped one in firestore.tf for these
# roles, unlike that case this isn't a workaround, it's the only option.
resource "google_project_iam_member" "agent_storage_viewer" {
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = google_service_account.agent.member
}

resource "google_project_iam_member" "agent_aiplatform_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = google_service_account.agent.member
}

# Google's own Vertex AI service agents (not the agent's runtime SA above) —
# auto-provisioned per project, need Compute Engine permissions to actually
# wire up PSC-I during deployment, which nothing in roles/aiplatform.*
# covers. No resource-scoped IAM binding exists for Network Attachments in
# this provider (checked), so project-level is the only option.
#
# gcp-sa-aiplatform-re (the reasoning-engine-specific agent) only ever hit
# the .get 403 in testing, so roles/compute.networkViewer covers it.
#
# gcp-sa-aiplatform (the generic Vertex AI agent, format documented at
# https://docs.cloud.google.com/vertex-ai/docs/general/vpc-psc-i-setup) is
# the one that actually drives PSC-I setup: it first hit the same .get 403,
# and after that was granted networkViewer, hit a second 403 on
# compute.networkAttachments.update (a NetworkAttachmentsService.Patch call
# — it registers itself as a consumer of the attachment, which the API
# treats as an update to the attachment resource). Google's docs confirm
# this and say to grant roles/compute.networkAdmin to this exact service
# agent for PSC-I; a narrower custom role is offered as an alternative but
# was not worth chasing here given the docs' own primary recommendation.
resource "google_project_iam_member" "reasoning_engine_agent_network_viewer" {
  project = var.project_id
  role    = "roles/compute.networkViewer"
  member  = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
}

resource "google_project_iam_member" "vertex_ai_agent_network_admin" {
  project = var.project_id
  role    = "roles/compute.networkAdmin"
  member  = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-aiplatform.iam.gserviceaccount.com"
}

# Needed for spec.deployment_spec.psc_interface_config.dns_peering_configs:
# without it, apply fails with "Permission denied when creating DNS peering
# zone... make sure the Vertex AI service account has the dns.peer role" —
# confirmed by an actual failed apply, not assumed. Same service agent as
# vertex_ai_agent_network_admin above, since it's the one that drives PSC-I
# setup generally.
resource "google_project_iam_member" "vertex_ai_agent_dns_peer" {
  project = var.project_id
  role    = "roles/dns.peer"
  member  = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-aiplatform.iam.gserviceaccount.com"
}

# ---------------------------------------------------------------------------
# The deployment itself.
# ---------------------------------------------------------------------------

resource "google_vertex_ai_reasoning_engine" "day_planner_agent" {
  provider = google-beta

  project      = var.project_id
  region       = var.agent_region
  display_name = "Day Planner Agent"
  description  = "Conversational day-planning assistant. Reads connected Google Calendars via day_planner_backend_internal; never holds an OAuth credential itself."

  spec {
    # Not inferred from source_code_spec — Terraform has no idea the
    # entrypoint is an ADK AdkApp, unlike the `adk deploy`/SDK path which
    # sets this automatically. Without it, the Cloud Console's Playground
    # ("available for agents built with ADK") stays disabled even though the
    # agent is, in fact, ADK.
    agent_framework = "google-adk"
    class_methods   = jsonencode(local.agent_class_methods)
    identity_type   = "SERVICE_ACCOUNT"
    service_account = google_service_account.agent.email

    deployment_spec {
      # GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION, and GOOGLE_GENAI_USE_VERTEXAI
      # are deliberately not set here: Agent Engine injects
      # GOOGLE_CLOUD_PROJECT itself and rejects it as a reserved name if you
      # also set it ("Error 400: Environment variable name 'GOOGLE_CLOUD_PROJECT'
      # is reserved" — confirmed by an actual failed apply, not assumed). The
      # other two are the same category of "platform already knows this"
      # value, removed proactively for the same reason.
      env {
        name  = "INTERNAL_BACKEND_URL"
        value = local.internal_url
      }

      # AdkApp(agent=_llm_agent) in agent.py leaves enable_tracing unset
      # (None), so this env var is what actually turns tracing on — per the
      # ADK truth table, enable_tracing=None + this env true only takes
      # effect on ADK >= 1.17 (requirements.txt is currently unpinned;
      # verify the resolved google-adk version if traces don't show up).
      # Exports spans to Cloud Trace in this project.
      env {
        name  = "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"
        value = "true"
      }

      # Requires day-planner-agent-psc (network.tf) to already exist in the
      # same region — see that file for why this is what makes Agent
      # Engine's calls to day-planner-internal (ingress = INTERNAL_ONLY)
      # count as internal VPC traffic instead of being rejected outright.
      #
      # network_attachment alone only gives Agent Engine's tenant network a
      # path INTO day-planner-vpc — it does not make Agent Engine's own DNS
      # resolution consult this VPC's Cloud DNS zones. Without
      # dns_peering_configs, calls to *.run.app resolve via Agent Engine's
      # own (public) resolver regardless of any private zone this project
      # has for run.app, so the request never uses the internal path in the
      # first place. Confirmed empirically: private_ip_google_access plus a
      # private run.app zone had zero effect until this was added.
      psc_interface_config {
        network_attachment = google_compute_network_attachment.agent_psc.name

        dns_peering_configs {
          domain         = "run.app."
          target_project = var.project_id
          target_network = google_compute_network.day_planner_vpc.name
        }
      }
    }

    source_code_spec {
      python_spec {
        entrypoint_module = "day_planner_agent.agent"
        requirements_file = "day_planner_agent/requirements.txt"
        version           = "3.12"
      }
      inline_source {
        source_archive = filebase64("${path.module}/${var.agent_source_archive_path}")
      }
    }
  }

  depends_on = [
    google_project_iam_member.agent_storage_viewer,
    google_project_iam_member.agent_aiplatform_user,
    google_project_iam_member.reasoning_engine_agent_network_viewer,
    google_project_iam_member.vertex_ai_agent_network_admin,
    google_compute_network_attachment.agent_psc,
  ]
}
