"""Password hashing.

Pure functions only — no FastAPI dependencies, no HTTP status codes. The
dependency wiring that turns these into 401s lives in `app.api.deps`, so this
module stays usable from scripts and tests.

No OIDC/service-to-service verification here — this service never receives
internal calls. That logic (and its own google-auth dependency) lives only in
the sibling day_planner_backend_internal service.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

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
