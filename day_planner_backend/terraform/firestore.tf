resource "google_firestore_database" "default" {
  count = var.create_firestore_database ? 1 : 0

  project     = var.project_id
  name        = "(default)"
  location_id = var.firestore_location
  type        = "FIRESTORE_NATIVE"

  # This database holds every user's calendar connection. Deleting it is not
  # something an errant `terraform destroy` should be able to do.
  delete_protection_state = "DELETE_PROTECTION_ENABLED"
  deletion_policy         = "ABANDON"

  depends_on = [google_project_service.apis]
}

locals {
  firestore_database = var.create_firestore_database ? google_firestore_database.default[0].name : "(default)"
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

  depends_on = [google_project_service.apis]
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

  depends_on = [google_project_service.apis]
}

# Failed-login counters, cleared on success and irrelevant once the lockout
# window passes.
resource "google_firestore_field" "login_throttle_ttl" {
  project    = var.project_id
  database   = local.firestore_database
  collection = "login_throttle"
  field      = "locked_until"

  ttl_config {}

  depends_on = [google_project_service.apis]
}
