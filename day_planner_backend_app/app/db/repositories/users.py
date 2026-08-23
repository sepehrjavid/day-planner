"""The account document: identity, agent-session pointer, chat quota.

Three concerns share one Firestore document (users/{user_id}) rather than
three repositories, because they genuinely are one aggregate — every method
here reads or writes fields on that same document, never a subcollection.
See ../store.py's module docstring for why this file exists at all (A6.5).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from google.cloud import firestore

from ..models import (
    EmailAlreadyRegistered,
    QuotaState,
    next_utc_midnight,
    normalize_email,
    utcnow,
)

USERS = "users"
USER_EMAILS = "user_emails"


class UserRepository:
    def __init__(self, db: firestore.AsyncClient) -> None:
        self._db = db

    async def create(self, *, email: str, password_hash: str) -> str:
        """Claim the email and create the user atomically.

        Raises EmailAlreadyRegistered if the address is taken.
        """
        normalized = normalize_email(email)
        user_id = uuid.uuid4().hex
        email_ref = self._db.collection(USER_EMAILS).document(normalized)
        user_ref = self._db.collection(USERS).document(user_id)

        @firestore.async_transactional
        async def _create(transaction) -> None:
            # All reads must precede all writes inside a Firestore transaction.
            existing = await email_ref.get(transaction=transaction)
            if existing.exists:
                raise EmailAlreadyRegistered(normalized)
            transaction.set(email_ref, {"user_id": user_id, "created_at": utcnow()})
            transaction.set(
                user_ref,
                {
                    "email": normalized,
                    "password_hash": password_hash,
                    "email_verified": False,
                    "default_account_id": None,
                    "created_at": utcnow(),
                    "updated_at": utcnow(),
                },
            )

        await _create(self._db.transaction())
        return user_id

    async def get(self, user_id: str) -> dict | None:
        snapshot = await self._db.collection(USERS).document(user_id).get()
        if not snapshot.exists:
            return None
        return {"user_id": user_id, **(snapshot.to_dict() or {})}

    async def get_by_email(self, email: str) -> dict | None:
        pointer = (
            await self._db.collection(USER_EMAILS)
            .document(normalize_email(email))
            .get()
        )
        if not pointer.exists:
            return None
        return await self.get((pointer.to_dict() or {})["user_id"])

    async def update_password_hash(self, *, user_id: str, password_hash: str) -> None:
        await self._db.collection(USERS).document(user_id).update(
            {"password_hash": password_hash, "updated_at": utcnow()}
        )

    # ------------------------------------------------------------------
    # Agent Engine session
    #
    # The mapping from a signed-in user to their Agent Engine session_id,
    # plus when it was last used (so /me/chat can decide whether it's gone
    # idle and should be rolled over). This is what lets /me/chat never
    # accept a session_id from the client: the client says "continue my
    # chat" by presenting its session token, same as every other /me route,
    # and the actual session_id is resolved here instead. See
    # app/services/agent_client.py for why that matters.
    # ------------------------------------------------------------------

    async def get_agent_session(self, user_id: str) -> tuple[str | None, datetime | None]:
        """Returns (session_id, last_active_at) — both None if there's no
        session yet or the user doesn't exist."""
        snapshot = await self._db.collection(USERS).document(user_id).get()
        if not snapshot.exists:
            return None, None
        data = snapshot.to_dict() or {}
        return data.get("agent_session_id"), data.get("agent_session_last_active_at")

    async def set_agent_session(self, *, user_id: str, session_id: str) -> None:
        """Records session_id as current and touches its last-active time —
        called on every message, not just when the session_id changes, since
        the idle clock has to reset on activity."""
        await self._db.collection(USERS).document(user_id).set(
            {
                "agent_session_id": session_id,
                "agent_session_last_active_at": utcnow(),
                "updated_at": utcnow(),
            },
            merge=True,
        )

    async def clear_agent_session(self, user_id: str) -> None:
        await self._db.collection(USERS).document(user_id).set(
            {
                "agent_session_id": None,
                "agent_session_last_active_at": None,
                "updated_at": utcnow(),
            },
            merge=True,
        )

    # ------------------------------------------------------------------
    # Chat message quota
    #
    # One global daily limit per user for now (settings.chat_daily_quota) —
    # not yet tier-aware. See docs/pricing-ideas.md for where this is headed
    # (per-tier limits, pay-as-you-go overage) once there's billing to back
    # it.
    # ------------------------------------------------------------------

    async def check_and_consume_quota(
        self, user_id: str, *, daily_limit: int
    ) -> QuotaState:
        """Atomically check the caller's remaining daily messages and, if any
        are left, consume one. The check and the increment happen in the same
        transaction so two requests racing on the last unit can't both pass.

        The window is the UTC calendar day: a stored quota_date that doesn't
        match today means the count is stale and starts over, rather than
        needing a separate cleanup job to zero it out.
        """
        ref = self._db.collection(USERS).document(user_id)
        now = utcnow()
        today = now.date().isoformat()
        reset_at = next_utc_midnight(now)

        @firestore.async_transactional
        async def _consume(transaction) -> QuotaState:
            snapshot = await ref.get(transaction=transaction)
            data = snapshot.to_dict() or {}
            count = data.get("quota_count", 0) if data.get("quota_date") == today else 0

            if count >= daily_limit:
                return QuotaState(
                    allowed=False, limit=daily_limit, remaining=0, reset_at=reset_at
                )

            count += 1
            transaction.set(
                ref,
                {"quota_date": today, "quota_count": count, "updated_at": now},
                merge=True,
            )
            return QuotaState(
                allowed=True,
                limit=daily_limit,
                remaining=daily_limit - count,
                reset_at=reset_at,
            )

        return await _consume(self._db.transaction())
