"""Firestore persistence.

Collection layout:

  users/{user_id}                                the account: email, password
                                                 hash, default calendar account,
                                                 agent_session_id + agent_session
                                                 _last_active_at (which Agent
                                                 Engine session /me/chat resumes,
                                                 and whether it's gone idle),
                                                 quota_date + quota_count (the
                                                 daily /me/chat message quota —
                                                 see check_and_consume_quota)

  users/{user_id}/connected_accounts/{acct_id}   one per linked calendar
                                                 account. A user can have as
                                                 many as they like — personal
                                                 Google, work Google, later a
                                                 CalDAV one. Each holds its own
                                                 credential and calendars.

  user_emails/{normalized_email}                 uniqueness lock for signup.
                                                 Firestore has no unique
                                                 constraint, so "query, then
                                                 write" races two concurrent
                                                 signups into duplicate
                                                 accounts. A document keyed by
                                                 the email, claimed inside a
                                                 transaction, is the constraint.

  sessions/{token_hash}                          opaque login sessions, TTL'd
  login_throttle/{normalized_email}              failed-attempt counter
  oauth_states/{nonce}                           single-use connect links, TTL'd

Two things are deliberately never stored: provider access tokens (they last an
hour; the refresh token can always mint another) and raw session tokens.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta

from google.cloud import firestore

from .models import (
    STATUS_ACTIVE,
    STATUS_NEEDS_REAUTH,
    Calendar,
    ConnectedAccount,
    EmailAlreadyRegistered,
    OAuthState,
    QuotaState,
    ThrottleState,
    account_id_for,
    hash_session_token,
    next_utc_midnight,
    normalize_email,
    utcnow,
)

USERS = "users"
USER_EMAILS = "user_emails"
SESSIONS = "sessions"
LOGIN_THROTTLE = "login_throttle"
OAUTH_STATES = "oauth_states"
CONNECTED_ACCOUNTS = "connected_accounts"


class Store:
    def __init__(self, project_id: str, database: str) -> None:
        self._project_id = project_id
        self._database = database
        self._client: firestore.AsyncClient | None = None

    @property
    def _db(self) -> firestore.AsyncClient:
        """Built on first use, not at construction.

        Instantiating an AsyncClient resolves Application Default Credentials
        eagerly, which would make credential resolution a hard startup
        dependency: a blip talking to the metadata server would crash the
        container instead of failing one request, and /healthz could never
        answer during it.
        """
        if self._client is None:
            self._client = firestore.AsyncClient(
                project=self._project_id, database=self._database
            )
        return self._client

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    async def create_user(self, *, email: str, password_hash: str) -> str:
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

    async def get_user(self, user_id: str) -> dict | None:
        snapshot = await self._db.collection(USERS).document(user_id).get()
        if not snapshot.exists:
            return None
        return {"user_id": user_id, **(snapshot.to_dict() or {})}

    async def get_user_by_email(self, email: str) -> dict | None:
        pointer = (
            await self._db.collection(USER_EMAILS)
            .document(normalize_email(email))
            .get()
        )
        if not pointer.exists:
            return None
        return await self.get_user((pointer.to_dict() or {})["user_id"])

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

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def create_session(
        self, *, user_id: str, ttl_seconds: int
    ) -> tuple[str, datetime]:
        """Returns (raw token, expiry). Only the token's hash is persisted."""
        token = secrets.token_urlsafe(32)
        expires_at = utcnow() + timedelta(seconds=ttl_seconds)
        await self._db.collection(SESSIONS).document(hash_session_token(token)).set(
            {"user_id": user_id, "created_at": utcnow(), "expires_at": expires_at}
        )
        return token, expires_at

    async def resolve_session(self, token: str) -> str | None:
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

    async def delete_session(self, token: str) -> None:
        await self._db.collection(SESSIONS).document(
            hash_session_token(token)
        ).delete()

    # ------------------------------------------------------------------
    # Login throttling
    # ------------------------------------------------------------------

    async def check_login_throttle(
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

    async def record_login_failure(
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

    async def clear_login_failures(self, email: str) -> None:
        await self._db.collection(LOGIN_THROTTLE).document(
            normalize_email(email)
        ).delete()

    # ------------------------------------------------------------------
    # OAuth state (connect links)
    # ------------------------------------------------------------------

    async def create_oauth_state(
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

    async def peek_oauth_state(self, nonce: str) -> OAuthState | None:
        """Read without consuming — /start may be hit more than once if the
        user clicks the link, wanders off, and comes back."""
        snapshot = await self._db.collection(OAUTH_STATES).document(nonce).get()
        if not snapshot.exists:
            return None
        return self._to_state(nonce, snapshot.to_dict() or {})

    async def consume_oauth_state(self, nonce: str) -> OAuthState | None:
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

    # ------------------------------------------------------------------
    # Connected calendar accounts
    # ------------------------------------------------------------------

    def _accounts(self, user_id: str):
        return (
            self._db.collection(USERS).document(user_id).collection(CONNECTED_ACCOUNTS)
        )

    async def save_account(
        self,
        *,
        user_id: str,
        provider: str,
        credential_type: str,
        provider_account_id: str,
        email: str | None,
        encrypted_refresh_token: str,
        kms_key_name: str,
        scopes: list[str],
        calendars: list[Calendar],
    ) -> str:
        """Create or refresh a connected account. Returns its account_id.

        Reconnecting an account the user already linked lands on the same
        document, so it heals a needs_reauth account rather than growing a
        duplicate. Calendar *selection* survives a reconnect — the user's
        choice about which calendars matter shouldn't be silently reset
        because a refresh token expired.
        """
        account_id = account_id_for(provider, provider_account_id)
        ref = self._accounts(user_id).document(account_id)

        existing = await ref.get()
        is_new = not existing.exists
        previously_selected = (
            {
                c["calendar_id"]
                for c in (existing.to_dict() or {}).get("calendars", [])
                if c.get("selected")
            }
            if existing.exists
            else set()
        )

        merged = [
            Calendar(
                calendar_id=c.calendar_id,
                summary=c.summary,
                is_primary=c.is_primary,
                selected=(
                    c.calendar_id in previously_selected
                    if previously_selected
                    else c.selected
                ),
            )
            for c in calendars
        ]

        payload: dict = {
            "provider": provider,
            "credential_type": credential_type,
            "provider_account_id": provider_account_id,
            "email": email,
            "scopes": scopes,
            "encrypted_refresh_token": encrypted_refresh_token,
            "kms_key_name": kms_key_name,
            "status": STATUS_ACTIVE,
            "calendars": [c.to_dict() for c in merged],
            "last_error": None,
            "updated_at": utcnow(),
        }
        if is_new:
            payload["connected_at"] = utcnow()
        await ref.set(payload, merge=True)

        # First account connected becomes the default target for writes.
        user_ref = self._db.collection(USERS).document(user_id)
        snapshot = await user_ref.get()
        if not (snapshot.to_dict() or {}).get("default_account_id"):
            await user_ref.set(
                {"default_account_id": account_id, "updated_at": utcnow()}, merge=True
            )

        return account_id

    async def list_accounts(self, user_id: str) -> list[ConnectedAccount]:
        return [
            ConnectedAccount.from_dict(doc.id, doc.to_dict() or {})
            async for doc in self._accounts(user_id).stream()
        ]

    async def get_account(
        self, *, user_id: str, account_id: str
    ) -> ConnectedAccount | None:
        snapshot = await self._accounts(user_id).document(account_id).get()
        if not snapshot.exists:
            return None
        return ConnectedAccount.from_dict(account_id, snapshot.to_dict() or {})

    async def set_calendar_selection(
        self, *, user_id: str, account_id: str, selected_calendar_ids: set[str]
    ) -> ConnectedAccount | None:
        ref = self._accounts(user_id).document(account_id)
        snapshot = await ref.get()
        if not snapshot.exists:
            return None

        data = snapshot.to_dict() or {}
        calendars = [
            {**c, "selected": c["calendar_id"] in selected_calendar_ids}
            for c in data.get("calendars", [])
        ]
        await ref.set({"calendars": calendars, "updated_at": utcnow()}, merge=True)
        return ConnectedAccount.from_dict(account_id, {**data, "calendars": calendars})

    async def mark_needs_reauth(
        self, *, user_id: str, account_id: str, reason: str
    ) -> None:
        """Flip a dead grant into a recoverable state rather than an error.

        The credential is dropped: it is known-useless, and keeping it only
        means holding a secret we can't use.
        """
        await self._accounts(user_id).document(account_id).set(
            {
                "status": STATUS_NEEDS_REAUTH,
                "encrypted_refresh_token": None,
                "last_error": reason,
                "updated_at": utcnow(),
            },
            merge=True,
        )

    async def delete_account(self, *, user_id: str, account_id: str) -> None:
        await self._accounts(user_id).document(account_id).delete()

        user_ref = self._db.collection(USERS).document(user_id)
        snapshot = await user_ref.get()
        if (snapshot.to_dict() or {}).get("default_account_id") != account_id:
            return

        # Promote whatever is left, so "default" doesn't dangle at a deleted id.
        remaining = await self.list_accounts(user_id)
        await user_ref.set(
            {
                "default_account_id": remaining[0].account_id if remaining else None,
                "updated_at": utcnow(),
            },
            merge=True,
        )
