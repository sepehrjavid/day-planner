"""Password-reset request attempt counters
(password_reset_throttle/{email-or-ip key}, TTL'd) (A6.4).

Deliberately a separate collection and separate repository from
LoginThrottleRepository, not a generalisation of it: reset requests are
throttled per an arbitrary caller-supplied key (an email, or an IP
address — see routes/auth.py's request_password_reset), where login
throttling is always keyed by email specifically. Same
counter-plus-locked_until shape as LoginThrottleRepository.check/
record_failure, just keyed more generally.
"""

from __future__ import annotations

from datetime import timedelta

from google.cloud import firestore

from ..models import ThrottleState, utcnow

PASSWORD_RESET_THROTTLE = "password_reset_throttle"


class PasswordResetThrottleRepository:
    def __init__(self, db: firestore.AsyncClient) -> None:
        self._db = db

    async def check(
        self, key: str, *, max_attempts: int, lockout_seconds: int
    ) -> ThrottleState:
        snapshot = await self._db.collection(PASSWORD_RESET_THROTTLE).document(key).get()
        if not snapshot.exists:
            return ThrottleState(locked=False)

        locked_until = (snapshot.to_dict() or {}).get("locked_until")
        if locked_until and utcnow() < locked_until:
            return ThrottleState(
                locked=True,
                retry_after_seconds=int((locked_until - utcnow()).total_seconds()) + 1,
            )
        return ThrottleState(locked=False)

    async def record_attempt(
        self, key: str, *, max_attempts: int, lockout_seconds: int
    ) -> None:
        ref = self._db.collection(PASSWORD_RESET_THROTTLE).document(key)

        @firestore.async_transactional
        async def _bump(transaction) -> None:
            snapshot = await ref.get(transaction=transaction)
            data = (snapshot.to_dict() or {}) if snapshot.exists else {}
            attempts = int(data.get("attempt_count", 0)) + 1
            payload: dict = {"attempt_count": attempts, "updated_at": utcnow()}
            if attempts >= max_attempts:
                payload["locked_until"] = utcnow() + timedelta(seconds=lockout_seconds)
                payload["attempt_count"] = 0
            transaction.set(ref, payload, merge=True)

        await _bump(self._db.transaction())
