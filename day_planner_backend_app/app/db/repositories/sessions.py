"""Opaque login sessions (sessions/{token_hash}, TTL'd)."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from google.cloud import firestore

from ..models import hash_session_token, utcnow

SESSIONS = "sessions"


class SessionRepository:
    def __init__(self, db: firestore.AsyncClient) -> None:
        self._db = db

    async def create(self, *, user_id: str, ttl_seconds: int) -> tuple[str, datetime]:
        """Returns (raw token, expiry). Only the token's hash is persisted."""
        token = secrets.token_urlsafe(32)
        expires_at = utcnow() + timedelta(seconds=ttl_seconds)
        await self._db.collection(SESSIONS).document(hash_session_token(token)).set(
            {"user_id": user_id, "created_at": utcnow(), "expires_at": expires_at}
        )
        return token, expires_at

    async def resolve(self, token: str) -> str | None:
        snapshot = (
            await self._db.collection(SESSIONS)
            .document(hash_session_token(token))
            .get()
        )
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        # TTL deletion is asynchronous and can lag by hours, so expiry is
        # enforced on read rather than trusted to the sweeper.
        if data.get("expires_at") and utcnow() >= data["expires_at"]:
            return None
        return data.get("user_id")

    async def delete(self, token: str) -> None:
        await self._db.collection(SESSIONS).document(
            hash_session_token(token)
        ).delete()

    async def delete_all_for_user(
        self, *, user_id: str, except_token: str | None = None
    ) -> None:
        """Evict every session for this user, optionally keeping the one
        whose raw token is except_token. Called after a password change
        (keeping the caller's own session) and after a password reset
        (keeping none — A6.4's "I lost control of this account" case).

        sessions/{token_hash} has no user_id-scoped subcollection (see
        create's own docstring for why session tokens are opaque and
        unkeyed by account), so this queries the flat `sessions`
        collection by its user_id field. Firestore indexes every field
        for single-field equality queries by default, so this needs no
        Terraform change to work."""
        keep = hash_session_token(except_token) if except_token is not None else None
        async for doc in self._db.collection(SESSIONS).where(
            "user_id", "==", user_id
        ).stream():
            if doc.id != keep:
                await doc.reference.delete()
