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

    # --- Vertex AI Agent Engine (day_planner_agent) ---
    # Full resource name: projects/{p}/locations/{l}/reasoningEngines/{id}
    # (terraform output: google_vertex_ai_reasoning_engine.day_planner_agent.name)
    agent_engine_name: str
    # Region the reasoning engine is deployed in (var.agent_region in
    # terraform) — not necessarily the same region as this Cloud Run
    # service, so kept as its own setting rather than reusing `region`.
    agent_engine_location: str

    # --- Google OAuth client (type: Web application) ---
    google_oauth_client_id: str
    google_oauth_client_secret: str

    # Origin of this service, no trailing slash. Must match the redirect URI
    # registered on the OAuth client *exactly*. The sibling
    # day_planner_backend_internal service also reads this same value (as its
    # own PUBLIC_BASE_URL) since /internal/connect-link builds a URL pointing
    # at /auth/{provider}/start, which lives here, not on itself.
    public_base_url: str

    # How long a connect link stays valid.
    state_ttl_seconds: int = 600

    # How long a login session lasts. 30 days suits a chat-first product where
    # re-authenticating is friction with little security payoff; sessions are
    # revocable server-side, so this isn't the only lever.
    session_ttl_seconds: int = 60 * 60 * 24 * 30

    # Failed logins before the address is locked out, and for how long.
    login_max_attempts: int = 8
    login_lockout_seconds: int = 900

    def redirect_uri(self, provider: str) -> str:
        return f"{self.public_base_url.rstrip('/')}/auth/{provider}/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
