"""Firestore persistence.

This service and day_planner_backend_app point at the same database — see
that service's own store.py for the account/signup/session/login-throttle
collections and the code that owns them (account creation, password
hashing, session issuance, throttling). None of that lives here: this
service only ever reads a user document, via get_user, to resolve
mint_access_token's default_account_id. It never creates a user, never
issues a session, and has no login surface of its own to throttle — there
was no auth route in this codebase for any of that code to back, so it's
gone rather than left to invite the assumption that this service handles
sessions.

Collection layout (the two this service actually reads and writes):

  users/{user_id}                                the account document
                                                 app-service creates and
                                                 maintains — read-only from
                                                 here, via get_user, for
                                                 default_account_id.

  users/{user_id}/connected_accounts/{acct_id}   one per linked calendar
                                                 account. A user can have as
                                                 many as they like — personal
                                                 Google, work Google, later a
                                                 CalDAV one. Each holds its own
                                                 credential and calendars.

  oauth_states/{nonce}                           single-use connect links, TTL'd

Habits, habit sessions, zones, and the sleep schedule used to live here too
(users/{user_id}/habits, .../habit_sessions, .../zones, .../sleep_schedule) —
moved to day_planner_backend_app by A6.1. That data has no credential
exposure, unlike everything this service still owns; see
docs/roadmaps/1-agent.md's A6.1 for the reasoning. No Firestore path
changed — the documents live at the same location, only the code that
reads and writes them moved.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from google.cloud import firestore

from .models import (
    STATUS_ACTIVE,
    STATUS_NEEDS_REAUTH,
    Calendar,
    ConnectedAccount,
    OAuthState,
    account_id_for,
    utcnow,
)

USERS = "users"
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

    async def get_user(self, user_id: str) -> dict | None:
        """The only account read this service performs — mint_access_token's
        default_account_id lookup. Signup, login, sessions, and throttling
        all belong to day_planner_backend_app; this service creates no
        user, issues no session, and has no login route of its own."""
        snapshot = await self._db.collection(USERS).document(user_id).get()
        if not snapshot.exists:
            return None
        return {"user_id": user_id, **(snapshot.to_dict() or {})}

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
