# Service enablement (run, firestore, cloudkms, secretmanager,
# artifactregistry, iamcredentials, calendar-json) is owned by another layer,
# not this module — every resource below assumes its API is already on.

resource "google_artifact_registry_repository" "backend" {
  project       = var.project_id
  location      = var.region
  repository_id = "day-planner"
  format        = "DOCKER"
  description   = <<-EOT
    Container images for the day planner backend. One repo, two independent
    image names (backend-app, backend-internal) — an Artifact Registry
    repository is just a namespace for tags, so sharing one here doesn't
    couple the two services' code or builds in any way.
  EOT
}
