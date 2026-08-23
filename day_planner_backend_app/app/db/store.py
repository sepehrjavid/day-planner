"""Thin facade over app/db/repositories/* (A6.5).

Store's only job is building the shared Firestore client lazily and handing
it to whichever repository a caller asks for — `store.habits`, `store.zones`,
`store.accounts`, and so on, one property per repository. See
repositories/__init__.py for the collection-layout map (moved there so it
sits next to the code that actually implements it) and for why this split
exists at all.

Each property below returns a fresh, stateless repository instance wrapping
the same shared client — repositories hold no per-request state of their own,
so there is nothing to gain from caching the instance, only bookkeeping to
avoid.
"""

from __future__ import annotations

from google.cloud import firestore

from .repositories import (
    AccountRepository,
    HabitRepository,
    HabitSessionRepository,
    LoginThrottleRepository,
    OAuthStateRepository,
    PasswordResetRepository,
    PasswordResetThrottleRepository,
    SessionRepository,
    SleepScheduleRepository,
    UserRepository,
    ZoneRepository,
)


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
        answer during it. Every repository property below reads this, so the
        client is still built on first *use* post-split, exactly as before —
        merely constructing a Store, or accessing a repository property
        without calling a method on it, still touches nothing.
        """
        if self._client is None:
            self._client = firestore.AsyncClient(
                project=self._project_id, database=self._database
            )
        return self._client

    @property
    def users(self) -> UserRepository:
        return UserRepository(self._db)

    @property
    def sessions(self) -> SessionRepository:
        return SessionRepository(self._db)

    @property
    def login_throttle(self) -> LoginThrottleRepository:
        return LoginThrottleRepository(self._db)

    @property
    def oauth_states(self) -> OAuthStateRepository:
        return OAuthStateRepository(self._db)

    @property
    def password_resets(self) -> PasswordResetRepository:
        return PasswordResetRepository(self._db)

    @property
    def password_reset_throttle(self) -> PasswordResetThrottleRepository:
        return PasswordResetThrottleRepository(self._db)

    @property
    def accounts(self) -> AccountRepository:
        return AccountRepository(self._db)

    @property
    def habits(self) -> HabitRepository:
        return HabitRepository(self._db)

    @property
    def habit_sessions(self) -> HabitSessionRepository:
        return HabitSessionRepository(self._db)

    @property
    def zones(self) -> ZoneRepository:
        return ZoneRepository(self._db)

    @property
    def sleep_schedule(self) -> SleepScheduleRepository:
        return SleepScheduleRepository(self._db)
