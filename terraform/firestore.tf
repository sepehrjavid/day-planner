resource "google_firestore_database" "default" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.firestore_location
  type        = "FIRESTORE_NATIVE"

  # This database holds every user's calendar connection. Deleting it is not
  # something an errant `terraform destroy` should be able to do.
  delete_protection_state = "DELETE_PROTECTION_ENABLED"
  deletion_policy         = "ABANDON"
}

locals {
  firestore_database = google_firestore_database.default.name
}

# Firestore has no IAM-policy resource of its own — the project-level policy
# is the only attachment point the API exposes (confirmed against
# https://docs.cloud.google.com/firestore/native/docs/manage-databases, no
# google_firestore_database_iam_member resource exists in this provider).
# The condition is what actually narrows this from "every database in the
# project" to just the one this backend touches, which is the real
# least-privilege lever Firestore gives you.
resource "google_project_iam_member" "backend_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.backend.email}"

  condition {
    title       = "day-planner-database-only"
    description = "Scopes datastore.user to the day-planner Firestore database, not every database in the project."
    expression  = "resource.name == \"projects/${var.project_id}/databases/${local.firestore_database}\""
  }
}

# Garbage-collects connect links that were minted and never clicked. The
# callback deletes them explicitly on use, so this is purely the abandoned
# case — but without it, unused single-use nonces accumulate forever.
resource "google_firestore_field" "oauth_states_ttl" {
  project    = var.project_id
  database   = local.firestore_database
  collection = "oauth_states"
  field      = "expires_at"

  ttl_config {}
}

# Expired login sessions. Note that Firestore's TTL sweeper is best-effort and
# can lag by hours, so the app also enforces expiry on read — this policy is
# about not accumulating rows, not about correctness.
resource "google_firestore_field" "sessions_ttl" {
  project    = var.project_id
  database   = local.firestore_database
  collection = "sessions"
  field      = "expires_at"

  ttl_config {}
}

# Failed-login counters, cleared on success and irrelevant once the lockout
# window passes.
resource "google_firestore_field" "login_throttle_ttl" {
  project    = var.project_id
  database   = local.firestore_database
  collection = "login_throttle"
  field      = "locked_until"

  ttl_config {}
}
