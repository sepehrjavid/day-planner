"""Password reset tokens (password_resets/{token_hash}, TTL'd) (A6.4).

Same shape as sessions: only the token's hash is persisted, and only its
hash is ever looked up, so a Firestore dump can't be replayed as a set of
live reset links. Consumption mirrors OAuthStateRepository.consume's
read-and-delete transaction — a captured or logged reset link must never
be usable twice.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from google.cloud import firestore

from ..models import hash_session_token, utcnow

PASSWORD_RESETS = "password_resets"


class PasswordResetRepository:
    def __init__(self, db: firestore.AsyncClient) -> None:
        self._db = db

    async def create(self, *, user_id: str, ttl_seconds: int) -> tuple[str, datetime]:
        """Returns (raw token, expiry). hash_session_token is reused here
        despite its name — it's a generic sha256 digest of an opaque
        token, the same operation this needs, and introducing a second
        identical function under a different name would just be the kind
        of drift A6.3's shared-validation reasoning warns about."""
        token = secrets.token_urlsafe(32)
        expires_at = utcnow() + timedelta(seconds=ttl_seconds)
        await self._db.collection(PASSWORD_RESETS).document(
            hash_session_token(token)
        ).set({"user_id": user_id, "created_at": utcnow(), "expires_at": expires_at})
        return token, expires_at

    async def consume(self, token: str) -> str | None:
        """Read and delete atomically. Returns the user_id, or None if the
        token is unknown, already used, or expired.

        Expiry is enforced on read even though the transaction already
        deleted the document by the time that check runs — the same
        "TTL sweeper can lag by hours, don't trust it for correctness"
        reasoning SessionRepository.resolve documents, applied to a token
        that's single-use to begin with, so there's no separate branch
        that skips the delete."""
        ref = self._db.collection(PASSWORD_RESETS).document(hash_session_token(token))

        @firestore.async_transactional
        async def _consume(transaction) -> dict | None:
            snapshot = await ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            data = snapshot.to_dict() or {}
            transaction.delete(ref)
            return data

        data = await _consume(self._db.transaction())
        if data is None:
            return None
        if data.get("expires_at") and utcnow() >= data["expires_at"]:
            return None
        return data.get("user_id")
