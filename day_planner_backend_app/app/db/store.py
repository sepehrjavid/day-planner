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

  users/{user_id}/habits/{habit_id}              one per recurring goal the
                                                 agent tracks and schedules
                                                 (see db/models.py's Habit
                                                 docstring for why this is a
                                                 plain record and not part of
                                                 the Memory Bank profile).
                                                 Moved here from
                                                 day_planner_backend_internal
                                                 by A6.1 — this is ordinary
                                                 user data with no credential
                                                 exposure, unlike everything
                                                 else in this collection
                                                 layout's neighbourhood.

  users/{user_id}/habit_sessions/{session_id}    one per calendar event the
                                                 agent created for a habit,
                                                 keyed on (calendar_id,
                                                 event_id) via
                                                 habit_session_id_for — see
                                                 db/models.py's HabitSession
                                                 docstring. Outlives the
                                                 calendar event on purpose:
                                                 review_habit_week needs a
                                                 record of what was planned
                                                 even after the event itself
                                                 is deleted.

  users/{user_id}/zones/{zone_id}                one per named scheduling
                                                 restriction (work hours,
                                                 commute, ...) — see
                                                 db/models.py's Zone
                                                 docstring. No documents at
                                                 all means no restriction of
                                                 this kind exists for the
                                                 user.

  users/{user_id}/sleep_schedule/{fixed id}      singleton: the user's
                                                 sleep/wake times and the
                                                 cool-down/wake-up windows
                                                 derived from them — see
                                                 db/models.py's SleepSchedule
                                                 docstring. A subcollection
                                                 with one fixed-id document,
                                                 the same shape every other
                                                 user-scoped resource here
                                                 uses, rather than a bare
                                                 field on the user doc.

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
    HABIT_SESSION_STATUS_COMPLETED,
    HABIT_SESSION_STATUS_PENDING,
    HABIT_STATUS_ACTIVE,
    STATUS_ACTIVE,
    STATUS_NEEDS_REAUTH,
    Calendar,
    ConnectedAccount,
    EmailAlreadyRegistered,
    Habit,
    HabitSession,
    OAuthState,
    QuotaState,
    SleepSchedule,
    ThrottleState,
    Zone,
    account_id_for,
    habit_session_id_for,
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
HABITS = "habits"
HABIT_SESSIONS = "habit_sessions"
ZONES = "zones"
SLEEP_SCHEDULE = "sleep_schedule"
SLEEP_SCHEDULE_DOC_ID = "current"


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

    # ------------------------------------------------------------------
    # Habits
    #
    # Moved here from day_planner_backend_internal by A6.1 — see that
    # task's own reasoning in docs/roadmaps/1-agent.md: this is ordinary
    # user data with no credential exposure, unlike everything else that
    # service protects. Code-only move; no Firestore path changed.
    # ------------------------------------------------------------------

    def _habits(self, user_id: str):
        return self._db.collection(USERS).document(user_id).collection(HABITS)

    async def create_habit(self, *, user_id: str, label: str, goal: str) -> Habit:
        habit_id = uuid.uuid4().hex
        now = utcnow()
        payload = {
            "label": label,
            "goal": goal,
            "status": HABIT_STATUS_ACTIVE,
            "created_at": now,
            "updated_at": now,
        }
        await self._habits(user_id).document(habit_id).set(payload)
        return Habit.from_dict(habit_id, payload)

    async def list_habits(self, user_id: str, *, status: str | None = None) -> list[Habit]:
        query = self._habits(user_id)
        if status is not None:
            query = query.where("status", "==", status)
        return [
            Habit.from_dict(doc.id, doc.to_dict() or {}) async for doc in query.stream()
        ]

    async def update_habit(
        self,
        *,
        user_id: str,
        habit_id: str,
        label: str | None = None,
        goal: str | None = None,
        status: str | None = None,
        allowed_zones: list[str] | None = None,
    ) -> Habit | None:
        """Partial update. Returns None if habit_id doesn't exist for this
        user, so the route can turn that into a 404 rather than silently
        creating a new document under a caller-chosen id."""
        ref = self._habits(user_id).document(habit_id)
        snapshot = await ref.get()
        if not snapshot.exists:
            return None

        payload: dict = {"updated_at": utcnow()}
        if label is not None:
            payload["label"] = label
        if goal is not None:
            payload["goal"] = goal
        if status is not None:
            payload["status"] = status
        if allowed_zones is not None:
            payload["allowed_zones"] = allowed_zones
        await ref.set(payload, merge=True)

        updated = await ref.get()
        return Habit.from_dict(habit_id, updated.to_dict() or {})

    # ------------------------------------------------------------------
    # Habit sessions (the plan log review_habit_week diffs against
    # actual calendar state)
    # ------------------------------------------------------------------

    def _habit_sessions(self, user_id: str):
        return self._db.collection(USERS).document(user_id).collection(HABIT_SESSIONS)

    async def upsert_habit_session(
        self,
        *,
        user_id: str,
        habit_id: str,
        event_id: str,
        calendar_id: str,
        planned_start: datetime,
        planned_end: datetime,
    ) -> HabitSession:
        """Create a session record, or — for the same (calendar_id,
        event_id), e.g. after the agent reschedules its own event — update
        its plan in place. created_at is set once and preserved across
        later upserts; everything else always reflects the latest plan.

        status/completed_at/marked_by (A1.5) are deliberately absent from
        `payload` below — merge=True then leaves them completely untouched
        on an existing document, which is what makes completion survive a
        reschedule (see HabitSession's docstring for the invariant this
        protects). Only a brand-new document gets an explicit starting
        status, since there's no prior value to preserve."""
        session_id = habit_session_id_for(calendar_id, event_id)
        ref = self._habit_sessions(user_id).document(session_id)

        existing = await ref.get()
        now = utcnow()
        payload = {
            "habit_id": habit_id,
            "event_id": event_id,
            "calendar_id": calendar_id,
            "planned_start": planned_start,
            "planned_end": planned_end,
            "updated_at": now,
        }
        if not existing.exists:
            payload["created_at"] = now
            payload["status"] = HABIT_SESSION_STATUS_PENDING
        await ref.set(payload, merge=True)

        updated = await ref.get()
        return HabitSession.from_dict(session_id, updated.to_dict() or {})

    async def set_habit_session_status(
        self,
        *,
        user_id: str,
        calendar_id: str,
        event_id: str,
        status: str,
        marked_by: str,
    ) -> HabitSession | None:
        """Explicitly mark a session's completion state. Returns None if no
        session exists for this (calendar_id, event_id) under this user, so
        the route can turn that into a 404 rather than creating a record
        via a side door that skips upsert_habit_session's own plan fields.

        Idempotent: calling this again with the *same* status is a true
        no-op — the existing record is returned unchanged, without even a
        write, so completed_at doesn't keep drifting forward on repeated
        calls. completed_at is only ever set when transitioning *to*
        completed; transitioning to skipped clears it, since it would
        otherwise misreport when a since-abandoned completion happened.
        """
        session_id = habit_session_id_for(calendar_id, event_id)
        ref = self._habit_sessions(user_id).document(session_id)

        snapshot = await ref.get()
        if not snapshot.exists:
            return None

        current = snapshot.to_dict() or {}
        if current.get("status") == status:
            return HabitSession.from_dict(session_id, current)

        now = utcnow()
        payload = {
            "status": status,
            "marked_by": marked_by,
            "updated_at": now,
            "completed_at": now if status == HABIT_SESSION_STATUS_COMPLETED else None,
        }
        await ref.set(payload, merge=True)

        updated = await ref.get()
        return HabitSession.from_dict(session_id, updated.to_dict() or {})

    async def list_habit_sessions(
        self, user_id: str, *, planned_from: datetime, planned_to: datetime
    ) -> list[HabitSession]:
        """Every session planned to start in [planned_from, planned_to) —
        a native Firestore Timestamp range query, not a string comparison,
        so this stays correct regardless of which UTC offset a given
        session's planned_start happens to carry."""
        query = (
            self._habit_sessions(user_id)
            .where("planned_start", ">=", planned_from)
            .where("planned_start", "<", planned_to)
        )
        return [
            HabitSession.from_dict(doc.id, doc.to_dict() or {})
            async for doc in query.stream()
        ]

    # ------------------------------------------------------------------
    # Zones
    # ------------------------------------------------------------------

    def _zones(self, user_id: str):
        return self._db.collection(USERS).document(user_id).collection(ZONES)

    async def create_zone(
        self,
        *,
        user_id: str,
        label: str,
        start_time: str,
        end_time: str,
        days_of_week: list[str],
    ) -> Zone:
        zone_id = uuid.uuid4().hex
        now = utcnow()
        payload = {
            "label": label,
            "start_time": start_time,
            "end_time": end_time,
            "days_of_week": days_of_week,
            "created_at": now,
            "updated_at": now,
        }
        await self._zones(user_id).document(zone_id).set(payload)
        return Zone.from_dict(zone_id, payload)

    async def list_zones(self, user_id: str) -> list[Zone]:
        return [
            Zone.from_dict(doc.id, doc.to_dict() or {})
            async for doc in self._zones(user_id).stream()
        ]

    async def update_zone(
        self,
        *,
        user_id: str,
        zone_id: str,
        label: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        days_of_week: list[str] | None = None,
    ) -> Zone | None:
        """Partial update. Returns None if zone_id doesn't exist for this
        user, same 404-vs-silent-create reasoning as update_habit."""
        ref = self._zones(user_id).document(zone_id)
        snapshot = await ref.get()
        if not snapshot.exists:
            return None

        payload: dict = {"updated_at": utcnow()}
        if label is not None:
            payload["label"] = label
        if start_time is not None:
            payload["start_time"] = start_time
        if end_time is not None:
            payload["end_time"] = end_time
        if days_of_week is not None:
            payload["days_of_week"] = days_of_week
        await ref.set(payload, merge=True)

        updated = await ref.get()
        return Zone.from_dict(zone_id, updated.to_dict() or {})

    # ------------------------------------------------------------------
    # Sleep schedule (singleton per user)
    # ------------------------------------------------------------------

    def _sleep_schedule_ref(self, user_id: str):
        return (
            self._db.collection(USERS)
            .document(user_id)
            .collection(SLEEP_SCHEDULE)
            .document(SLEEP_SCHEDULE_DOC_ID)
        )

    async def get_sleep_schedule(self, user_id: str) -> SleepSchedule | None:
        snapshot = await self._sleep_schedule_ref(user_id).get()
        if not snapshot.exists:
            return None
        return SleepSchedule.from_dict(snapshot.to_dict() or {})

    async def set_sleep_schedule(
        self,
        *,
        user_id: str,
        sleep_time: str | None = None,
        wake_time: str | None = None,
        cool_down_minutes: int | None = None,
        wake_up_buffer_minutes: int | None = None,
        day_overrides: dict[str, dict[str, str]] | None = None,
    ) -> SleepSchedule:
        """Create-or-update, unlike update_zone/update_habit — there's
        always exactly one sleep schedule per user, so the first call
        naturally creates it rather than needing a separate create step.
        Partial update like the others; day_overrides replaces the whole
        map when provided rather than merging per-day, so clearing an
        override means passing the full remaining set back, not just the
        one key you want gone."""
        ref = self._sleep_schedule_ref(user_id)
        existing = await ref.get()
        now = utcnow()

        payload: dict = {"updated_at": now}
        if not existing.exists:
            payload["created_at"] = now
        if sleep_time is not None:
            payload["sleep_time"] = sleep_time
        if wake_time is not None:
            payload["wake_time"] = wake_time
        if cool_down_minutes is not None:
            payload["cool_down_minutes"] = cool_down_minutes
        if wake_up_buffer_minutes is not None:
            payload["wake_up_buffer_minutes"] = wake_up_buffer_minutes
        if day_overrides is not None:
            payload["day_overrides"] = day_overrides
        await ref.set(payload, merge=True)

        updated = await ref.get()
        return SleepSchedule.from_dict(updated.to_dict() or {})
