resource "google_kms_key_ring" "main" {
  project  = var.project_id
  name     = "day-planner"
  location = var.region

  depends_on = [google_project_service.apis]
}

resource "google_kms_crypto_key" "oauth_tokens" {
  name     = "oauth-tokens"
  key_ring = google_kms_key_ring.main.id
  purpose  = "ENCRYPT_DECRYPT"

  # Rotation mints a new primary version for future encrypts; existing
  # ciphertext stays decryptable under its original version, so no migration
  # is needed.
  rotation_period = "7776000s" # 90 days

  lifecycle {
    # Destroying this key permanently bricks every stored refresh token — no
    # recovery, every user has to reconnect. Terraform should refuse.
    prevent_destroy = true
  }
}

# The backend both encrypts (at connect) and decrypts (on every token refresh),
# so it needs the combined role. The encrypter/decrypter split only buys
# something once those two operations live in different services.
resource "google_kms_crypto_key_iam_member" "backend" {
  crypto_key_id = google_kms_crypto_key.oauth_tokens.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_service_account.backend.email}"
}
