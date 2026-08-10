variable "project_id" {
  type        = string
  description = "GCP project hosting the backend."
}

variable "region" {
  type        = string
  description = "Region for Cloud Run, Artifact Registry, and KMS."
  default     = "europe-north1"
}

variable "agent_region" {
  type        = string
  description = <<-EOT
    Region for the Vertex AI Agent Engine (Reasoning Engine) deployment, and
    for the subnet + Network Attachment backing its PSC-I egress (network.tf).
    Deliberately separate from var.region: Agent Engine has much narrower
    region availability than Cloud Run/KMS and does not support every region
    those do (notably not europe-north2). As of writing, supported European
    regions are europe-west1/2/3/4/6/8 and europe-southwest1 — verify against
    current Vertex AI Agent Engine docs before changing this, availability
    shifts over time.
  EOT
  default     = "europe-west1"
}

variable "service_name" {
  type        = string
  description = <<-EOT
    Cloud Run service name for the app-facing surface (health, auth, oauth,
    me). Gated by var.publicly_exposed — closed during the test phase, one
    flag flip to open at launch.
  EOT
  default     = "day-planner-backend"
}

variable "internal_service_name" {
  type        = string
  description = <<-EOT
    Cloud Run service name for the internal surface (/internal/* only).
    Invoker access is granted only to the agent's own service account
    (agent.tf), never allUsers — this one has no launch-day toggle, because
    it's never meant to be reachable by an end user's browser, at any point.
  EOT
  default     = "day-planner-internal"
}

variable "publicly_exposed" {
  type        = bool
  description = <<-EOT
    Whether the service is granted allUsers invoker access. False (the
    default, for the test phase) means only explicitly authorized principals
    can reach it at all — including Google's OAuth redirect, so the live
    browser-based OAuth flow can't be tested against this deployment until
    this is true. Flip to true and re-apply when you're ready to go live.
  EOT
  default     = false
}

variable "app_image" {
  type        = string
  description = <<-EOT
    Fully qualified image for day_planner_backend_app, ideally
    digest-pinned:
    europe-north1-docker.pkg.dev/PROJECT/day-planner/backend-app@sha256:...
    Built from ../day_planner_backend_app — a separate Dockerfile and
    dependency set from the internal image below, not two tags of the same
    build. On the very first apply, before you've pushed anything, point
    this at a placeholder and re-apply after the first build.
  EOT
}

variable "internal_image" {
  type        = string
  description = <<-EOT
    Fully qualified image for day_planner_backend_internal, ideally
    digest-pinned:
    europe-north1-docker.pkg.dev/PROJECT/day-planner/backend-internal@sha256:...
    Built from ../day_planner_backend_internal. Same placeholder-then-real
    pattern as var.app_image applies here independently — the two services
    are built, pushed, and re-applied on their own schedules.
  EOT
}

variable "firestore_location" {
  type        = string
  description = "Firestore location. Cannot be changed after creation."
  default     = "eur3"
}

variable "agent_source_archive_path" {
  type        = string
  description = <<-EOT
    Path to the agent's pre-built source tarball (.tar.gz, base64-read via
    filebase64()), relative to this directory. Build it with
    ../day_planner_agent/build_archive.sh, which produces
    ../day_planner_agent.tar.gz — Terraform doesn't build application
    artifacts itself, matching how var.app_image/var.internal_image are
    also built externally and only referenced here.
  EOT
  default     = "../day_planner_agent.tar.gz"
}

variable "google_oauth_client_id" {
  type        = string
  description = <<-EOT
    Client ID of an OAuth client of type "Web application". A Desktop client
    will reject the Cloud Run redirect URI. Not a secret — the client *secret*
    goes into Secret Manager out of band.
  EOT
}

variable "public_base_url" {
  type        = string
  description = <<-EOT
    Origin of the app-facing service (var.service_name), no trailing slash.
    Leave null to derive Cloud Run's deterministic URL; set it explicitly if
    the `service_uri` output disagrees, or once a custom domain is mapped.
    Whatever this is must be registered verbatim as the OAuth client's
    redirect URI, suffixed with /auth/google/callback.
  EOT
  default     = null
}

variable "internal_base_url" {
  type        = string
  description = <<-EOT
    Origin of the internal service (var.internal_service_name), no trailing
    slash. Only used as SELF_BASE_URL, the audience the internal service
    expects on incoming OIDC tokens — it's never dialled by a browser, so
    nothing outside this module needs it to match anything registered
    anywhere. Leave null to derive Cloud Run's deterministic URL; set it
    explicitly if the `internal_service_uri` output disagrees (some projects
    get the legacy `-<hash>-` form instead of `-<project number>-`).
  EOT
  default     = null
}

variable "state_ttl_seconds" {
  type        = number
  description = "How long a connect link stays valid."
  default     = 600
}

variable "max_instances" {
  type        = number
  description = "Bounded per docs/deployment-gcp.md's cost-control note."
  default     = 2
}
