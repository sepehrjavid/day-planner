"""Runtime configuration, read from the environment.

On Cloud Run these come from the service's env block; the OAuth client
secret is mounted from Secret Manager rather than set literally (see
terraform/cloud_run.tf).
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- GCP ---
    gcp_project_id: str
    firestore_database: str = "(default)"

    # Full KMS resource name:
    # projects/{p}/locations/{l}/keyRings/{r}/cryptoKeys/{k}
    kms_key_name: str

    # --- Google OAuth client (type: Web application) ---
    google_oauth_client_id: str
    google_oauth_client_secret: str

    # Origin of the sibling day_planner_backend_app service, no trailing
    # slash — used only to build the URL /internal/connect-link hands back
    # (pointing at /auth/{provider}/start, which lives there, not here).
    public_base_url: str

    # This service's own origin, no trailing slash — the audience incoming
    # OIDC tokens are checked against. Unlike the app service, this is
    # required: every route here is gated by require_internal_caller, so
    # there's no path where an unset value would silently go unused.
    self_base_url: str

    # Service accounts allowed to call this service. Comma-separated emails.
    # In practice: the Agent Engine runtime SA and the Slack adapter SA.
    internal_caller_service_accounts: str = ""

    # How long a connect link stays valid.
    state_ttl_seconds: int = 600

    @property
    def internal_callers(self) -> set[str]:
        raw = self.internal_caller_service_accounts
        return {e.strip() for e in raw.split(",") if e.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
