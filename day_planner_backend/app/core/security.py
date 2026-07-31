"""Password hashing and internal service-to-service token verification.

Pure functions only — no FastAPI dependencies, no HTTP status codes. The
dependency wiring that turns these into 401s lives in `app.api.deps`, so this
module stays usable from scripts and tests.
"""

from __future__ import annotations

import asyncio
import logging

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from google.auth.transport import requests as ga_requests
from google.oauth2 import id_token as google_id_token

logger = logging.getLogger(__name__)

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


class InternalTokenError(Exception):
    """The caller's OIDC token was missing, unverifiable, or not allowlisted."""


async def verify_internal_token(
    token: str, *, audience: str, allowed_service_accounts: set[str]
) -> str:
    """Verify a Google-signed OIDC token and return the caller's SA email.

    Cloud Run's own IAM can't gate these routes: /auth/{provider}/callback has
    to be reachable by an anonymous browser redirect from Google, and Cloud Run
    IAM is service-wide, not per-route. So the service is deployed
    allow-unauthenticated and internal callers present the same kind of token
    Cloud Run IAM would have checked, verified here instead.
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
