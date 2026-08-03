"""Service-to-service token verification.

Pure functions only — no FastAPI dependencies, no HTTP status codes. The
dependency wiring that turns this into a 401/503 lives in `app.api.deps`, so
this module stays usable from scripts and tests.

No password hashing here — this service never handles a password, so it
carries no argon2-cffi dependency at all. That logic (and its own separate
copy of this file, minus this OIDC piece) lives only in the sibling
day_planner_backend_app service.
"""

from __future__ import annotations

import asyncio

from google.auth.transport import requests as ga_requests
from google.oauth2 import id_token as google_id_token


class InternalTokenError(Exception):
    """The caller's OIDC token was missing, unverifiable, or not allowlisted."""


async def verify_internal_token(
    token: str, *, audience: str, allowed_service_accounts: set[str]
) -> str:
    """Verify a Google-signed OIDC token and return the caller's SA email.

    This is the only thing standing between the internet and this service,
    on top of whichever principals Cloud Run IAM itself allows to invoke it
    (see terraform/cloud_run.tf — never allUsers for this service).
    """
    if not allowed_service_accounts:
        # Fail closed. An empty allowlist is a misconfiguration, and treating
        # it as "allow everyone" would silently expose connect-link minting.
        raise InternalTokenError("no internal callers are configured")

    try:
        claims = await asyncio.to_thread(
            google_id_token.verify_oauth2_token,
            token,
            ga_requests.Request(),
            audience,
        )
    except Exception as exc:  # noqa: BLE001 - any verification failure is a 401
        raise InternalTokenError(f"token verification failed: {exc}") from exc

    email = claims.get("email")
    if not claims.get("email_verified") or email not in allowed_service_accounts:
        raise InternalTokenError(f"caller not allowed: {email}")

    return email
