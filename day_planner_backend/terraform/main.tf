locals {
  apis = [
    "run.googleapis.com",
    "firestore.googleapis.com",
    "cloudkms.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "iamcredentials.googleapis.com",
    "calendar-json.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each = toset(local.apis)

  project = var.project_id
  service = each.value

  # Leave the APIs enabled if this stack is torn down; disabling them would
  # reach outside this module's blast radius.
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "backend" {
  project       = var.project_id
  location      = var.region
  repository_id = "day-planner"
  format        = "DOCKER"
  description   = "Container images for the day planner backend."

  depends_on = [google_project_service.apis]
}
