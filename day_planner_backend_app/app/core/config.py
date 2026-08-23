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

    # How long a chat session can sit idle before /me/chat rolls it over to
    # a fresh one (archiving the old one to Memory Bank first — see
    # AgentClient.archive_session). 6h: long enough that picking a
    # conversation back up an hour later still has context, short enough
    # that "yesterday" never leaks into "today" for a day planner. Tune via
    # AGENT_SESSION_IDLE_TIMEOUT_SECONDS without a redeploy of agent logic.
    agent_session_idle_timeout_seconds: int = 60 * 60 * 6

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

    # --- Password reset (A6.4) ---
    # SendGrid Mail Send API key. Restricted Access, Mail Send only — see
    # terraform/secrets.tf for why this has no default (forcing every
    # environment to configure it explicitly, the same as
    # google_oauth_client_secret) and why the value itself never appears
    # here, in Terraform state, or in git.
    sendgrid_api_key: str
    # Must be a Single Sender Verified (or domain-authenticated) address
    # in the SendGrid account sendgrid_api_key belongs to, or every send
    # is rejected. No default for the same reason as the key itself.
    password_reset_from_email: str

    # How long a reset link is valid. Short relative to session_ttl_seconds
    # (30 days) on purpose — a captured or logged link should have a
    # narrow window to be useful.
    password_reset_ttl_seconds: int = 1800

    # Reset-request attempts before a key (an email, or an IP — see
    # check_reset_throttle/record_reset_attempt in db/store.py) is locked
    # out, and for how long. Same shape and same defaults as
    # login_max_attempts/login_lockout_seconds above.
    password_reset_max_attempts: int = 8
    password_reset_lockout_seconds: int = 900

    # Messages /me/chat will forward to the agent per user per UTC day. One
    # flat limit for everyone for now — no tiers or billing yet, see
    # docs/pricing-ideas.md for where this is headed. 50 is a generous chat
    # allowance for a single person's day planning, cheap enough to eat as a
    # free tier while still bounding the worst case (a stuck client retry
    # loop, or a compromised session) on a per-request-billed backend.
    chat_daily_quota: int = 50

    # Diagnostic mode for turn_log.py's per-turn structured logging: off by
    # default, since tool arguments carry event titles, times, and
    # locations. Some tools' arguments are withheld from the log even when
    # this is true — see turn_log._NEVER_LOG_ARGS_FOR.
    log_tool_call_args: bool = False

    # Service accounts allowed to call /agent/* (A6.2) — comma-separated
    # emails, same shape and same "fail closed on empty" reasoning as
    # day_planner_backend_internal's Settings.internal_callers. In
    # practice: just the Agent Engine runtime SA. public_base_url (above)
    # doubles as the audience these tokens are checked against — unlike
    # that service, this one already knows its own public URL, so there's
    # no separate self_base_url setting to keep in sync with it.
    agent_caller_service_accounts: str = ""

    @property
    def agent_callers(self) -> set[str]:
        raw = self.agent_caller_service_accounts
        return {e.strip() for e in raw.split(",") if e.strip()}

    def redirect_uri(self, provider: str) -> str:
        return f"{self.public_base_url.rstrip('/')}/auth/{provider}/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
