"""Single-use OAuth connect-link state (oauth_states/{nonce}, TTL'd)."""

from __future__ import annotations

import secrets
from datetime import timedelta

from google.cloud import firestore

from ..models import OAuthState, utcnow

OAUTH_STATES = "oauth_states"


class OAuthStateRepository:
    def __init__(self, db: firestore.AsyncClient) -> None:
        self._db = db

    async def create(
        self, *, user_id: str, provider: str, code_verifier: str, ttl_seconds: int
    ) -> OAuthState:
        # This nonce is the only thing binding a consent screen to a user_id,
        # so it has to be unguessable — user_id must never appear in the
        # connect URL, or anyone could edit it and attach their own Google
        # account to somebody else's record.
        nonce = secrets.token_urlsafe(32)
        state = OAuthState(
            nonce=nonce,
            user_id=user_id,
            provider=provider,
            code_verifier=code_verifier,
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )
        await self._db.collection(OAUTH_STATES).document(nonce).set(
            {
                "user_id": state.user_id,
                "provider": state.provider,
                "code_verifier": state.code_verifier,
                "expires_at": state.expires_at,
                "created_at": utcnow(),
            }
        )
        return state

    async def peek(self, nonce: str) -> OAuthState | None:
        """Read without consuming — /start may be hit more than once if the
        user clicks the link, wanders off, and comes back."""
        snapshot = await self._db.collection(OAUTH_STATES).document(nonce).get()
        if not snapshot.exists:
            return None
        return self._to_state(nonce, snapshot.to_dict() or {})

    async def consume(self, nonce: str) -> OAuthState | None:
        """Read and delete atomically. Single-use is what stops a replayed
        callback from re-binding an account."""
        ref = self._db.collection(OAUTH_STATES).document(nonce)

        @firestore.async_transactional
        async def _consume(transaction) -> dict | None:
            snapshot = await ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            data = snapshot.to_dict() or {}
            transaction.delete(ref)
            return data

        data = await _consume(self._db.transaction())
        return None if data is None else self._to_state(nonce, data)

    @staticmethod
    def _to_state(nonce: str, data: dict) -> OAuthState:
        return OAuthState(
            nonce=nonce,
            user_id=data["user_id"],
            provider=data["provider"],
            code_verifier=data["code_verifier"],
            expires_at=data["expires_at"],
        )
