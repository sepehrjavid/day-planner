"""Test fixtures.

Environment is populated at import time, before anything pulls in
`app.config` — Settings is lru_cached, so the first read wins.

Scope note: FakeStore is an in-memory stand-in, so these tests cover the
*routes* — auth boundaries, the nonce lifecycle, multi-account fan-out. They
do not cover Store's Firestore-specific behaviour (the transactional email
claim, merge semantics, TTL). Covering that properly needs the Firestore
emulator; see README.

This service has exactly one router — no APP_SURFACE, no second test app to
build. That's the point of the split: main.app *is* the whole deployable.
"""

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("GCP_PROJECT_ID", "test-proj")
os.environ.setdefault("KMS_KEY_NAME", "projects/p/locations/l/keyRings/r/cryptoKeys/k")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_ID", "abc.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRET", "shh")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8080")
os.environ.setdefault("AGENT_CALLER_SERVICE_ACCOUNTS", "agent@test.iam.gserviceaccount.com")
os.environ.setdefault("SENDGRID_API_KEY", "SG.test-key")
os.environ.setdefault("PASSWORD_RESET_FROM_EMAIL", "noreply@test.invalid")
os.environ.setdefault(
    "AGENT_ENGINE_NAME",
    "projects/test-proj/locations/us-central1/reasoningEngines/1",
)
# Must be a region vertexai.init() actually recognizes — AgentClient's
# constructor validates it eagerly (see app/services/agent_client.py), and
# that constructor runs in every test that boots the app via the lifespan.
os.environ.setdefault("AGENT_ENGINE_LOCATION", "us-central1")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.services import crypto  # noqa: E402
from app.db.models import (  # noqa: E402
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
    next_utc_midnight,
    normalize_email,
)

REDIRECT_URI = "http://localhost:8080/auth/google/callback"
GOOD_PASSWORD = "correct-horse-battery-staple"


def _now():
    return datetime.now(timezone.utc)


class _FakeUsers:
    """Mirrors app/db/repositories/users.py's UserRepository (A6.5)."""

    def __init__(self, store: "FakeStore") -> None:
        self._store = store

    async def create(self, *, email, password_hash):
        store = self._store
        normalized = normalize_email(email)
        if normalized in store._emails:
            raise EmailAlreadyRegistered(normalized)
        user_id = store._next("user")
        store._emails[normalized] = user_id
        store._users[user_id] = {
            "email": normalized,
            "password_hash": password_hash,
            "email_verified": False,
            "default_account_id": None,
        }
        store._accounts[user_id] = {}
        return user_id

    async def get(self, user_id):
        user = self._store._users.get(user_id)
        return None if user is None else {"user_id": user_id, **user}

    async def get_by_email(self, email):
        user_id = self._store._emails.get(normalize_email(email))
        return None if user_id is None else await self.get(user_id)

    async def update_password_hash(self, *, user_id, password_hash):
        self._store._users[user_id]["password_hash"] = password_hash

    async def get_agent_session(self, user_id):
        user = self._store._users.get(user_id, {})
        return user.get("agent_session_id"), user.get("agent_session_last_active_at")

    async def set_agent_session(self, *, user_id, session_id):
        self._store._users[user_id]["agent_session_id"] = session_id
        self._store._users[user_id]["agent_session_last_active_at"] = _now()

    async def clear_agent_session(self, user_id):
        self._store._users[user_id]["agent_session_id"] = None
        self._store._users[user_id]["agent_session_last_active_at"] = None

    async def check_and_consume_quota(self, user_id, *, daily_limit):
        now = _now()
        today = now.date().isoformat()
        reset_at = next_utc_midnight(now)
        user = self._store._users[user_id]
        count = user.get("quota_count", 0) if user.get("quota_date") == today else 0

        if count >= daily_limit:
            return QuotaState(
                allowed=False, limit=daily_limit, remaining=0, reset_at=reset_at
            )

        count += 1
        user["quota_date"] = today
        user["quota_count"] = count
        return QuotaState(
            allowed=True,
            limit=daily_limit,
            remaining=daily_limit - count,
            reset_at=reset_at,
        )


class _FakeSessions:
    """Mirrors app/db/repositories/sessions.py's SessionRepository (A6.5)."""

    def __init__(self, store: "FakeStore") -> None:
        self._store = store

    async def create(self, *, user_id, ttl_seconds):
        store = self._store
        token = store._next("session-token")
        expires_at = _now() + timedelta(seconds=ttl_seconds)
        store._sessions[token] = {"user_id": user_id, "expires_at": expires_at}
        return token, expires_at

    async def resolve(self, token):
        session = self._store._sessions.get(token)
        if session is None or _now() >= session["expires_at"]:
            return None
        return session["user_id"]

    async def delete(self, token):
        self._store._sessions.pop(token, None)

    async def delete_all_for_user(self, *, user_id, except_token=None):
        sessions = self._store._sessions
        for token, session in list(sessions.items()):
            if session["user_id"] == user_id and token != except_token:
                del sessions[token]


class _FakeLoginThrottle:
    """Mirrors app/db/repositories/login_throttle.py's
    LoginThrottleRepository (A6.5)."""

    def __init__(self, store: "FakeStore") -> None:
        self._store = store

    async def check(self, email, *, max_attempts, lockout_seconds):
        record = self._store._login_throttle.get(normalize_email(email))
        if record and record.get("locked_until", _now()) > _now():
            return ThrottleState(
                locked=True,
                retry_after_seconds=int(
                    (record["locked_until"] - _now()).total_seconds()
                )
                + 1,
            )
        return ThrottleState(locked=False)

    async def record_failure(self, email, *, max_attempts, lockout_seconds):
        key = normalize_email(email)
        record = self._store._login_throttle.setdefault(key, {"failed_count": 0})
        record["failed_count"] += 1
        if record["failed_count"] >= max_attempts:
            record["locked_until"] = _now() + timedelta(seconds=lockout_seconds)
            record["failed_count"] = 0

    async def clear(self, email):
        self._store._login_throttle.pop(normalize_email(email), None)


class _FakePasswordResets:
    """Mirrors app/db/repositories/password_resets.py's
    PasswordResetRepository (A6.5)."""

    def __init__(self, store: "FakeStore") -> None:
        self._store = store

    async def create(self, *, user_id, ttl_seconds):
        store = self._store
        token = store._next("reset-token")
        expires_at = _now() + timedelta(seconds=ttl_seconds)
        store._password_resets[token] = {"user_id": user_id, "expires_at": expires_at}
        return token, expires_at

    async def consume(self, token):
        data = self._store._password_resets.pop(token, None)
        if data is None:
            return None
        if _now() >= data["expires_at"]:
            return None
        return data["user_id"]


class _FakePasswordResetThrottle:
    """Mirrors app/db/repositories/password_reset_throttle.py's
    PasswordResetThrottleRepository (A6.5)."""

    def __init__(self, store: "FakeStore") -> None:
        self._store = store

    async def check(self, key, *, max_attempts, lockout_seconds):
        record = self._store._password_reset_throttle.get(key)
        if record and record.get("locked_until", _now()) > _now():
            return ThrottleState(
                locked=True,
                retry_after_seconds=int(
                    (record["locked_until"] - _now()).total_seconds()
                )
                + 1,
            )
        return ThrottleState(locked=False)

    async def record_attempt(self, key, *, max_attempts, lockout_seconds):
        record = self._store._password_reset_throttle.setdefault(
            key, {"attempt_count": 0}
        )
        record["attempt_count"] += 1
        if record["attempt_count"] >= max_attempts:
            record["locked_until"] = _now() + timedelta(seconds=lockout_seconds)
            record["attempt_count"] = 0


class _FakeOAuthStates:
    """Mirrors app/db/repositories/oauth_states.py's
    OAuthStateRepository (A6.5)."""

    def __init__(self, store: "FakeStore") -> None:
        self._store = store

    async def create(self, *, user_id, provider, code_verifier, ttl_seconds):
        state = OAuthState(
            nonce=self._store._next("nonce"),
            user_id=user_id,
            provider=provider,
            code_verifier=code_verifier,
            expires_at=_now() + timedelta(seconds=ttl_seconds),
        )
        self._store._oauth_states[state.nonce] = state
        return state

    async def peek(self, nonce):
        return self._store._oauth_states.get(nonce)

    async def consume(self, nonce):
        return self._store._oauth_states.pop(nonce, None)


class _FakeAccounts:
    """Mirrors app/db/repositories/accounts.py's AccountRepository (A6.5)."""

    def __init__(self, store: "FakeStore") -> None:
        self._store = store

    async def save(
        self,
        *,
        user_id,
        provider,
        credential_type,
        provider_account_id,
        email,
        encrypted_refresh_token,
        kms_key_name,
        scopes,
        calendars,
    ):
        store = self._store
        account_id = account_id_for(provider, provider_account_id)
        owned = store._accounts.setdefault(user_id, {})
        previous = owned.get(account_id)

        previously_selected = (
            {c.calendar_id for c in previous["calendars"] if c.selected}
            if previous
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

        owned[account_id] = {
            "provider": provider,
            "credential_type": credential_type,
            "provider_account_id": provider_account_id,
            "email": email,
            "scopes": scopes,
            "encrypted_refresh_token": encrypted_refresh_token,
            "kms_key_name": kms_key_name,
            "status": STATUS_ACTIVE,
            "calendars": merged,
            "last_error": None,
        }
        if not store._users[user_id].get("default_account_id"):
            store._users[user_id]["default_account_id"] = account_id
        return account_id

    def _to_account(self, account_id, data):
        return ConnectedAccount(
            account_id=account_id,
            provider=data["provider"],
            credential_type=data["credential_type"],
            provider_account_id=data["provider_account_id"],
            email=data.get("email"),
            status=data["status"],
            scopes=data.get("scopes", []),
            calendars=list(data.get("calendars", [])),
            encrypted_refresh_token=data.get("encrypted_refresh_token"),
            kms_key_name=data.get("kms_key_name"),
            last_error=data.get("last_error"),
        )

    async def list(self, user_id):
        return [
            self._to_account(aid, data)
            for aid, data in self._store._accounts.get(user_id, {}).items()
        ]

    async def get(self, *, user_id, account_id):
        data = self._store._accounts.get(user_id, {}).get(account_id)
        return None if data is None else self._to_account(account_id, data)

    async def set_calendar_selection(self, *, user_id, account_id, selected_calendar_ids):
        data = self._store._accounts.get(user_id, {}).get(account_id)
        if data is None:
            return None
        data["calendars"] = [
            Calendar(
                calendar_id=c.calendar_id,
                summary=c.summary,
                is_primary=c.is_primary,
                selected=c.calendar_id in selected_calendar_ids,
            )
            for c in data["calendars"]
        ]
        return self._to_account(account_id, data)

    async def mark_needs_reauth(self, *, user_id, account_id, reason):
        data = self._store._accounts.get(user_id, {}).get(account_id)
        if data is None:
            return
        data["status"] = STATUS_NEEDS_REAUTH
        data["encrypted_refresh_token"] = None
        data["last_error"] = reason

    async def delete(self, *, user_id, account_id):
        store = self._store
        store._accounts.get(user_id, {}).pop(account_id, None)
        if store._users[user_id].get("default_account_id") == account_id:
            remaining = list(store._accounts.get(user_id, {}))
            store._users[user_id]["default_account_id"] = (
                remaining[0] if remaining else None
            )


class _FakeHabits:
    """Mirrors app/db/repositories/habits.py's HabitRepository (A6.5)."""

    def __init__(self, store: "FakeStore") -> None:
        self._store = store

    async def create(self, *, user_id, label, goal):
        store = self._store
        habit_id = store._next("habit")
        now = _now()
        data = {
            "label": label,
            "goal": goal,
            "status": HABIT_STATUS_ACTIVE,
            "created_at": now,
            "updated_at": now,
        }
        store._habits.setdefault(user_id, {})[habit_id] = data
        return Habit.from_dict(habit_id, data)

    async def list(self, user_id, *, status=None):
        items = self._store._habits.get(user_id, {})
        return [
            Habit.from_dict(habit_id, data)
            for habit_id, data in items.items()
            if status is None or data["status"] == status
        ]

    async def update(
        self, *, user_id, habit_id, label=None, goal=None, status=None, allowed_zones=None
    ):
        data = self._store._habits.get(user_id, {}).get(habit_id)
        if data is None:
            return None
        if label is not None:
            data["label"] = label
        if goal is not None:
            data["goal"] = goal
        if status is not None:
            data["status"] = status
        if allowed_zones is not None:
            data["allowed_zones"] = allowed_zones
        data["updated_at"] = _now()
        return Habit.from_dict(habit_id, data)


class _FakeHabitSessions:
    """Mirrors app/db/repositories/habit_sessions.py's
    HabitSessionRepository (A6.5)."""

    def __init__(self, store: "FakeStore") -> None:
        self._store = store

    async def upsert(
        self, *, user_id, habit_id, event_id, calendar_id, planned_start, planned_end
    ):
        bucket = self._store._habit_sessions.setdefault(user_id, {})
        session_id = habit_session_id_for(calendar_id, event_id)
        existing = bucket.get(session_id)
        now = _now()
        data = {
            "habit_id": habit_id,
            "event_id": event_id,
            "calendar_id": calendar_id,
            "planned_start": planned_start,
            "planned_end": planned_end,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
            # Completion state (A1.5) is preserved from any existing
            # record, exactly like created_at above — this is what makes
            # a reschedule (this method called again for the same
            # calendar_id/event_id) not silently wipe out a completion.
            "status": existing["status"] if existing else HABIT_SESSION_STATUS_PENDING,
            "completed_at": existing.get("completed_at") if existing else None,
            "marked_by": existing.get("marked_by") if existing else None,
        }
        bucket[session_id] = data
        return HabitSession.from_dict(session_id, data)

    async def set_status(self, *, user_id, calendar_id, event_id, status, marked_by):
        session_id = habit_session_id_for(calendar_id, event_id)
        bucket = self._store._habit_sessions.get(user_id, {})
        data = bucket.get(session_id)
        if data is None:
            return None
        if data.get("status") == status:
            return HabitSession.from_dict(session_id, data)
        data["status"] = status
        data["marked_by"] = marked_by
        data["updated_at"] = _now()
        data["completed_at"] = _now() if status == HABIT_SESSION_STATUS_COMPLETED else None
        return HabitSession.from_dict(session_id, data)

    async def list(self, user_id, *, planned_from, planned_to):
        return [
            HabitSession.from_dict(session_id, data)
            for session_id, data in self._store._habit_sessions.get(user_id, {}).items()
            if planned_from <= data["planned_start"] < planned_to
        ]


class _FakeZones:
    """Mirrors app/db/repositories/zones.py's ZoneRepository (A6.5)."""

    def __init__(self, store: "FakeStore") -> None:
        self._store = store

    async def create(self, *, user_id, label, start_time, end_time, days_of_week):
        store = self._store
        zone_id = store._next("zone")
        now = _now()
        data = {
            "label": label,
            "start_time": start_time,
            "end_time": end_time,
            "days_of_week": days_of_week,
            "created_at": now,
            "updated_at": now,
        }
        store._zones.setdefault(user_id, {})[zone_id] = data
        return Zone.from_dict(zone_id, data)

    async def list(self, user_id):
        return [
            Zone.from_dict(zone_id, data)
            for zone_id, data in self._store._zones.get(user_id, {}).items()
        ]

    async def update(
        self, *, user_id, zone_id, label=None, start_time=None, end_time=None, days_of_week=None
    ):
        data = self._store._zones.get(user_id, {}).get(zone_id)
        if data is None:
            return None
        if label is not None:
            data["label"] = label
        if start_time is not None:
            data["start_time"] = start_time
        if end_time is not None:
            data["end_time"] = end_time
        if days_of_week is not None:
            data["days_of_week"] = days_of_week
        data["updated_at"] = _now()
        return Zone.from_dict(zone_id, data)

    async def delete(self, *, user_id, zone_id):
        bucket = self._store._zones.get(user_id, {})
        if zone_id not in bucket:
            return False
        del bucket[zone_id]
        return True


class _FakeSleepSchedule:
    """Mirrors app/db/repositories/sleep_schedule.py's
    SleepScheduleRepository (A6.5)."""

    def __init__(self, store: "FakeStore") -> None:
        self._store = store

    async def get(self, user_id):
        data = self._store._sleep_schedules.get(user_id)
        return None if data is None else SleepSchedule.from_dict(data)

    async def set(
        self,
        *,
        user_id,
        sleep_time=None,
        wake_time=None,
        cool_down_minutes=None,
        wake_up_buffer_minutes=None,
        day_overrides=None,
    ):
        store = self._store
        data = store._sleep_schedules.get(user_id)
        now = _now()
        if data is None:
            data = {"created_at": now}
            store._sleep_schedules[user_id] = data
        if sleep_time is not None:
            data["sleep_time"] = sleep_time
        if wake_time is not None:
            data["wake_time"] = wake_time
        if cool_down_minutes is not None:
            data["cool_down_minutes"] = cool_down_minutes
        if wake_up_buffer_minutes is not None:
            data["wake_up_buffer_minutes"] = wake_up_buffer_minutes
        if day_overrides is not None:
            data["day_overrides"] = day_overrides
        data["updated_at"] = now
        return SleepSchedule.from_dict(data)


class FakeStore:
    """In-memory stand-in for Firestore, mirroring the real document shape.

    Restructured in lockstep with the real Store's A6.5 repository split —
    `store.habits.create(...)`, `store.zones.list(...)`, etc. — since route
    code under test calls exactly that shape. The underlying dicts
    (`_users`, `_sessions`, ...) are private to this class; a handful of
    tests reach into them directly for setup/assertions that have no
    public-API equivalent (expiring a session early, reading a raw
    password hash) — that's why they're still plain dicts and not hidden
    behind the fakes above, just no longer named the same as the public
    repository attributes.
    """

    def __init__(self) -> None:
        self._users: dict[str, dict] = {}
        self._emails: dict[str, str] = {}
        self._sessions: dict[str, dict] = {}
        self._login_throttle: dict[str, dict] = {}
        self._password_resets: dict[str, dict] = {}
        self._password_reset_throttle: dict[str, dict] = {}
        self._oauth_states: dict[str, OAuthState] = {}
        self._accounts: dict[str, dict[str, dict]] = {}
        self._habits: dict[str, dict[str, dict]] = {}
        self._habit_sessions: dict[str, dict[str, dict]] = {}
        self._zones: dict[str, dict[str, dict]] = {}
        self._sleep_schedules: dict[str, dict] = {}
        self._seq = 0

        self.users = _FakeUsers(self)
        self.sessions = _FakeSessions(self)
        self.login_throttle = _FakeLoginThrottle(self)
        self.password_resets = _FakePasswordResets(self)
        self.password_reset_throttle = _FakePasswordResetThrottle(self)
        self.oauth_states = _FakeOAuthStates(self)
        self.accounts = _FakeAccounts(self)
        self.habits = _FakeHabits(self)
        self.habit_sessions = _FakeHabitSessions(self)
        self.zones = _FakeZones(self)
        self.sleep_schedule = _FakeSleepSchedule(self)

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def anon_client(store):
    """Client with no auth overrides — for testing the gates themselves."""
    with TestClient(main.app) as client:
        # Must land *after* entering the context manager: that's what runs the
        # lifespan, which builds a real Firestore-backed Store and would
        # otherwise clobber the fake.
        main.app.state.store = store
        yield client


@pytest.fixture
def user(anon_client):
    """A signed-up user. Returns (user_id, auth headers)."""
    response = anon_client.post(
        "/auth/signup", json={"email": "me@example.com", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["user_id"], {"Authorization": f"Bearer {body['access_token']}"}


@pytest.fixture
def agent_client(store):
    """Client that passes the /agent/* caller gate (A6.2) — mirrors
    day_planner_backend_internal's own `client` fixture pattern for
    require_internal_caller: override the dependency rather than mint a
    real OIDC token, so route *logic* is testable without Google
    verification in the loop. The auth check itself (require_agent_caller
    unmodified) is covered separately, via anon_client, in
    test_agent_routes.py."""
    from app.api.deps import require_agent_caller

    main.app.dependency_overrides[require_agent_caller] = lambda: "agent@test.iam"
    with TestClient(main.app) as c:
        main.app.state.store = store
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def fake_crypto(monkeypatch):
    """Reversible stand-in for KMS that also encodes the user binding, so a
    ciphertext decrypted under the wrong user_id is detectable — the same
    property KMS gives us via additional authenticated data."""

    async def encrypt(key, plaintext, user_id):
        return f"enc({plaintext}|{user_id})"

    async def decrypt(key, ciphertext, user_id):
        prefix, suffix = "enc(", f"|{user_id})"
        if not (ciphertext.startswith(prefix) and ciphertext.endswith(suffix)):
            raise AssertionError(f"ciphertext {ciphertext!r} not bound to {user_id!r}")
        return ciphertext[len(prefix) : -len(suffix)]

    monkeypatch.setattr(crypto, "encrypt", encrypt)
    monkeypatch.setattr(crypto, "decrypt", decrypt)
