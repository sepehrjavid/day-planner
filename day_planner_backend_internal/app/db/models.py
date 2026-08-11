"""Domain types and the small amount of key-derivation logic that goes with them.

Kept separate from `store.py` so the shapes can be imported — by services,
schemas, or tests — without dragging in the Firestore client.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

STATUS_ACTIVE = "active"
STATUS_NEEDS_REAUTH = "needs_reauth"

HABIT_STATUS_ACTIVE = "active"
HABIT_STATUS_PAUSED = "paused"
HABIT_STATUS_ARCHIVED = "archived"


class EmailAlreadyRegistered(Exception):
    """Signup lost the race, or the address was already taken."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_session_token(token: str) -> str:
    """Sessions are stored as digests, so a database dump can't be replayed
    as a set of live logins."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def account_id_for(provider: str, provider_account_id: str) -> str:
    """Deterministic document ID for a connected account.

    Keying on (provider, provider_account_id) rather than an auto-ID is what
    makes "connect the same Google account twice" an idempotent update instead
    of a duplicate row — Firestore can't enforce a unique index, but it can
    enforce a document ID.
    """
    return f"{provider}__{provider_account_id}"


@dataclass(frozen=True)
class OAuthState:
    nonce: str
    user_id: str
    provider: str
    code_verifier: str
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        return utcnow() >= self.expires_at


@dataclass(frozen=True)
class ThrottleState:
    locked: bool
    retry_after_seconds: int = 0


@dataclass(frozen=True)
class Calendar:
    calendar_id: str
    summary: str | None
    is_primary: bool
    selected: bool

    def to_dict(self) -> dict:
        return {
            "calendar_id": self.calendar_id,
            "summary": self.summary,
            "is_primary": self.is_primary,
            "selected": self.selected,
        }

    @staticmethod
    def from_dict(data: dict) -> "Calendar":
        return Calendar(
            calendar_id=data["calendar_id"],
            summary=data.get("summary"),
            is_primary=bool(data.get("is_primary")),
            selected=bool(data.get("selected", True)),
        )


@dataclass(frozen=True)
class ConnectedAccount:
    account_id: str
    provider: str
    credential_type: str
    provider_account_id: str
    email: str | None
    status: str
    scopes: list[str] = field(default_factory=list)
    calendars: list[Calendar] = field(default_factory=list)
    encrypted_refresh_token: str | None = None
    kms_key_name: str | None = None
    last_error: str | None = None

    @property
    def is_usable(self) -> bool:
        return self.status == STATUS_ACTIVE and bool(self.encrypted_refresh_token)

    @staticmethod
    def from_dict(account_id: str, data: dict) -> "ConnectedAccount":
        return ConnectedAccount(
            account_id=account_id,
            provider=data["provider"],
            credential_type=data.get("credential_type", "oauth2"),
            provider_account_id=data["provider_account_id"],
            email=data.get("email"),
            status=data.get("status", STATUS_ACTIVE),
            scopes=data.get("scopes", []),
            calendars=[Calendar.from_dict(c) for c in data.get("calendars", [])],
            encrypted_refresh_token=data.get("encrypted_refresh_token"),
            kms_key_name=data.get("kms_key_name"),
            last_error=data.get("last_error"),
        )


@dataclass(frozen=True)
class Habit:
    """A user's recurring, schedulable goal — "180 min/week of exercise,
    sessions 30-60 min", "read 20-40 min most nights".

    Deliberately not part of the Memory Bank profile (see
    ../../docs/feature-ideas.md item 2): that store is LLM-written and
    semantically merged, which gives no stable id to tag a calendar event
    with and no guarantee exact wording survives a regeneration. Habits
    need a plain, addressable record instead — same reasoning that already
    kept calendar identity out of Memory Bank (../../docs/oauth-design.md
    §7).

    `goal` is kept as free text rather than structured frequency/duration
    fields on purpose: the placement logic in the agent's instructions
    already reasons over natural language, and locking down a numeric
    shape before real usage data exists risks fields that don't fit every
    habit (a "3x/week" habit and a "most nights" habit don't share an
    obvious shape).
    """

    habit_id: str
    label: str
    goal: str
    status: str
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def from_dict(habit_id: str, data: dict) -> "Habit":
        return Habit(
            habit_id=habit_id,
            label=data["label"],
            goal=data["goal"],
            status=data.get("status", HABIT_STATUS_ACTIVE),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )


def habit_session_id_for(calendar_id: str, event_id: str) -> str:
    """Deterministic document ID for a planned habit session, keyed on
    (calendar_id, event_id) rather than a random one.

    This is what makes create_calendar_event tagging a session and
    update_calendar_event later re-tagging it (after the agent moves the
    event) an idempotent upsert on the same document, instead of two
    different writers needing to coordinate a lookup first — the same
    reasoning as account_id_for above.
    """
    return f"{calendar_id}__{event_id}"


@dataclass(frozen=True)
class HabitSession:
    """A single planned occurrence of a habit — the record
    review_habit_week diffs against actual calendar state later.

    Deliberately keyed on the *calendar event*, not a random id (see
    habit_session_id_for) — the whole point of this record is to survive
    even if that event is later deleted, so review_habit_week has
    something to compare against ("gone") when a plain get_calendar_events
    call would come back empty.

    planned_start/planned_end are stored as real datetimes (Firestore
    Timestamps), not the raw strings Google's API returns, specifically so
    list_habit_sessions can run a native chronological range query rather
    than a string-lexicographic one — two events in different UTC offsets
    don't sort the same way as strings that they do as instants.
    """

    session_id: str
    habit_id: str
    event_id: str
    calendar_id: str
    planned_start: datetime
    planned_end: datetime
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def from_dict(session_id: str, data: dict) -> "HabitSession":
        return HabitSession(
            session_id=session_id,
            habit_id=data["habit_id"],
            event_id=data["event_id"],
            calendar_id=data["calendar_id"],
            planned_start=data["planned_start"],
            planned_end=data["planned_end"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )
