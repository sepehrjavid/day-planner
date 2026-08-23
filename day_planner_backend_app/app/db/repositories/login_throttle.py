"""Failed-login attempt counters (login_throttle/{normalized_email}, TTL'd)."""

from __future__ import annotations

from datetime import timedelta

from google.cloud import firestore

from ..models import ThrottleState, normalize_email, utcnow

LOGIN_THROTTLE = "login_throttle"


class LoginThrottleRepository:
    def __init__(self, db: firestore.AsyncClient) -> None:
        self._db = db

    async def check(
        self, email: str, *, max_attempts: int, lockout_seconds: int
    ) -> ThrottleState:
        snapshot = (
            await self._db.collection(LOGIN_THROTTLE)
            .document(normalize_email(email))
            .get()
        )
        if not snapshot.exists:
            return ThrottleState(locked=False)

        locked_until = (snapshot.to_dict() or {}).get("locked_until")
        if locked_until and utcnow() < locked_until:
            return ThrottleState(
                locked=True,
                retry_after_seconds=int((locked_until - utcnow()).total_seconds()) + 1,
            )
        return ThrottleState(locked=False)

    async def record_failure(
        self, email: str, *, max_attempts: int, lockout_seconds: int
    ) -> None:
        ref = self._db.collection(LOGIN_THROTTLE).document(normalize_email(email))

        @firestore.async_transactional
        async def _bump(transaction) -> None:
            snapshot = await ref.get(transaction=transaction)
            data = (snapshot.to_dict() or {}) if snapshot.exists else {}
            failures = int(data.get("failed_count", 0)) + 1
            payload: dict = {"failed_count": failures, "updated_at": utcnow()}
            if failures >= max_attempts:
                payload["locked_until"] = utcnow() + timedelta(seconds=lockout_seconds)
                payload["failed_count"] = 0
            transaction.set(ref, payload, merge=True)

        await _bump(self._db.transaction())

    async def clear(self, email: str) -> None:
        await self._db.collection(LOGIN_THROTTLE).document(
            normalize_email(email)
        ).delete()
