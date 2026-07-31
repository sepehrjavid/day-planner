variable "project_id" {
  type        = string
  description = "GCP project hosting the backend."
}

variable "region" {
  type        = string
  description = "Region for Cloud Run, Artifact Registry, and KMS."
  default     = "europe-north1"
}

variable "service_name" {
  type        = string
  description = "Cloud Run service name."
  default     = "day-planner-backend"
}

variable "image" {
  type        = string
  description = <<-EOT
    Fully qualified container image to deploy, ideally digest-pinned:
    europe-north1-docker.pkg.dev/PROJECT/day-planner/backend@sha256:...
    On the very first apply, before you've pushed anything, point this at a
    placeholder and re-apply after the first build.
  EOT
}

variable "firestore_location" {
  type        = string
  description = "Firestore location. Cannot be changed after creation."
  default     = "eur3"
}

variable "create_firestore_database" {
  type        = bool
  description = <<-EOT
    Set false if the project already has a (default) Firestore database —
    Terraform cannot create one twice, and importing is usually easier than
    fighting it.
  EOT
  default     = true
}

variable "agent_service_account_email" {
  type        = string
  description = <<-EOT
    Service account the Vertex AI Agent Engine runtime (and/or Slack adapter)
    runs as. This is the only identity permitted to call /internal/*.
  EOT
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
    Public origin of this service, no trailing slash. Leave null to derive
    Cloud Run's deterministic URL; set it explicitly if the `service_uri`
    output disagrees, or once a custom domain is mapped. Whatever this is must
    be registered verbatim as the OAuth client's redirect URI, suffixed with
    /auth/google/callback.
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
  description = "Bounded per deployment-gcp.md's cost-control note."
  default     = 2
}
