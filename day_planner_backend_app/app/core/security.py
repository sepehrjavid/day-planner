"""Password hashing, plus (as of A6.2) OIDC service-to-service token
verification for /agent/*.

Pure functions only — no FastAPI dependencies, no HTTP status codes. The
dependency wiring that turns these into 401s lives in `app.api.deps`, so this
module stays usable from scripts and tests.

verify_agent_token mirrors day_planner_backend_internal's own
verify_internal_token (app/core/security.py there) — same shape, same
"fail closed on an empty allowlist" reasoning, different audience (this
service's own public URL, not the internal service's). A6.2's own scope
note is explicit that /me and /agent must never share a dependency, a
router, or a user_id derivation — this function backs only
require_agent_caller (app/api/deps.py), never current_user_id.
"""

from __future__ import annotations

import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from google.auth.transport import requests as ga_requests
from google.oauth2 import id_token as google_id_token

_hasher = PasswordHasher()

# Argon2 cost is deliberate, so hashing an unbounded input is a cheap way to
# burn the server's CPU. The input is capped before it reaches the hasher.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024

# A precomputed hash of a value nobody can supply. Verifying against this when
# the account doesn't exist keeps the failed-login path the same shape (and
# roughly the same duration) whether or not the email is registered, so login
# timing can't be used to enumerate accounts.
_DUMMY_HASH = _hasher.hash("not-a-real-password-placeholder")


class PasswordPolicyError(ValueError):
    """Password failed the length policy."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> tuple[bool, bool]:
    """Returns (is_valid, needs_rehash).

    Pass password_hash=None for a non-existent account: the dummy verification
    still runs so the timing profile matches a real failure.
    """
    if password_hash is None:
        try:
            _hasher.verify(_DUMMY_HASH, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            pass
        return False, False

    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False

    return True, _hasher.check_needs_rehash(password_hash)


def validate_password_strength(password: str) -> None:
    """Length-based only, per NIST SP 800-63B: composition rules ("must
    contain a symbol") measurably push users toward weaker, more predictable
    passwords. Length is the requirement worth enforcing."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"password must be at most {MAX_PASSWORD_LENGTH} characters"
        )


class AgentTokenError(Exception):
    """The caller's OIDC token was missing, unverifiable, or not allowlisted."""


async def verify_agent_token(
    token: str, *, audience: str, allowed_service_accounts: set[str]
) -> str:
    """Verify a Google-signed OIDC token and return the caller's SA email.

    This is the only thing standing between the internet and /agent/*, on
    top of whichever principals Cloud Run IAM itself allows to invoke
    this service (see terraform/cloud_run.tf) — and unlike /me/*, IAM
    alone isn't enough here: once publicly_exposed flips, this service
    takes an allUsers invoker binding for the browser-facing surface, so
    this application-level check is what actually keeps /agent/* closed
    to everyone but the allowlisted service account.
    """
    if not allowed_service_accounts:
        # Fail closed. An empty allowlist is a misconfiguration, and
        # treating it as "allow everyone" would silently expose every
        # domain operation to any caller with an OIDC token at all.
        raise AgentTokenError("no agent callers are configured")

    try:
        claims = await asyncio.to_thread(
            google_id_token.verify_oauth2_token,
            token,
            ga_requests.Request(),
            audience,
        )
    except Exception as exc:  # noqa: BLE001 - any verification failure is a 401
        raise AgentTokenError(f"token verification failed: {exc}") from exc

    email = claims.get("email")
    if not claims.get("email_verified") or email not in allowed_service_accounts:
        raise AgentTokenError(f"caller not allowed: {email}")

    return email
