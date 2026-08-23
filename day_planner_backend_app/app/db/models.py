"""Domain types and the small amount of key-derivation logic that goes with them.

Kept separate from `store.py` so the shapes can be imported — by services,
schemas, or tests — without dragging in the Firestore client.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

STATUS_ACTIVE = "active"
STATUS_NEEDS_REAUTH = "needs_reauth"

HABIT_STATUS_ACTIVE = "active"
HABIT_STATUS_PAUSED = "paused"
HABIT_STATUS_ARCHIVED = "archived"

# HabitSession.status — three states, never two. PENDING means unknown,
# not failed; nothing may impute SKIPPED from an untouched PENDING session
# (see A1.5 in docs/roadmaps/1-agent.md — that would silently poison every
# metric built on this field).
HABIT_SESSION_STATUS_PENDING = "pending"
HABIT_SESSION_STATUS_COMPLETED = "completed"
HABIT_SESSION_STATUS_SKIPPED = "skipped"

MARKED_BY_USER = "user"
MARKED_BY_AGENT = "agent"

# Canonical day-of-week codes shared by Zone.days_of_week and
# SleepSchedule.day_overrides, so both entities key days the same way.
DAYS_OF_WEEK = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class EmailAlreadyRegistered(Exception):
    """Signup lost the race, or the address was already taken."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def next_utc_midnight(now: datetime) -> datetime:
    """Start of the next UTC calendar day — when a daily quota resets."""
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_of_today + timedelta(days=1)


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
class QuotaState:
    """Result of a daily message-quota check, made atomically with the
    consuming increment so two concurrent requests can't both slip through
    on the last unit — see Store.check_and_consume_quota."""

    allowed: bool
    limit: int
    remaining: int
    reset_at: datetime

    @property
    def retry_after_seconds(self) -> int:
        return max(0, int((self.reset_at - utcnow()).total_seconds()))


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
    allowed_zones: list[str] = field(default_factory=list)
    """Zone labels this habit may additionally be placed in, on top of the
    default of any unzoned (open) time — e.g. a lunchtime-workout habit
    allowed into a "Work" zone. See db/models.py's Zone docstring; a
    standing override, not the same as a one-off conversational exception
    for a single day (that never gets written here — see docs/todo.md §1's
    "Behavioral requirements")."""

    @staticmethod
    def from_dict(habit_id: str, data: dict) -> "Habit":
        return Habit(
            habit_id=habit_id,
            label=data["label"],
            goal=data["goal"],
            status=data.get("status", HABIT_STATUS_ACTIVE),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            allowed_zones=data.get("allowed_zones", []),
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

    status/completed_at/marked_by (A1.5) are first-class completion state,
    separate from whether the calendar event still exists — the calendar
    diff review_habit_week already did (kept/moved/gone) measures plan
    *durability*, not whether the user actually did the thing. Set only via
    Store.set_habit_session_status, never implied from calendar state.

    Invariant: completion must survive a reschedule. upsert_habit_session
    is called again on every update_calendar_event patch to a
    habit-tagged event, and since that patches the event in place,
    (calendar_id, event_id) — and so habit_session_id_for's key — never
    changes across a reschedule, and upsert_habit_session preserves
    status/completed_at/marked_by rather than resetting them. A path that
    *deletes and recreates* the event instead would produce a new key and
    orphan the completion; no such path exists in this codebase today
    (calendar_tool.py's update_calendar_event always patches), and adding
    one is forbidden by this contract unless it's also taught to carry
    status forward explicitly.
    """

    session_id: str
    habit_id: str
    event_id: str
    calendar_id: str
    planned_start: datetime
    planned_end: datetime
    created_at: datetime
    updated_at: datetime
    status: str = HABIT_SESSION_STATUS_PENDING
    completed_at: datetime | None = None
    marked_by: str | None = None

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
            # .get with a default: documents written before A1.5 have
            # neither field — must read back as PENDING (unknown), not
            # crash and not silently read as some other status.
            status=data.get("status", HABIT_SESSION_STATUS_PENDING),
            completed_at=data.get("completed_at"),
            marked_by=data.get("marked_by"),
        )


@dataclass(frozen=True)
class Zone:
    """A structured, reusable scheduling constraint — work hours, commute,
    or any other named block of a user's week — replacing the free-text
    "I work 9-5 weekdays" sentences that used to live only in the Memory
    Bank profile (see docs/todo.md §1).

    A zone is a restriction by default: no habit may be placed inside one
    unless its label appears in that habit's own `allowed_zones` (see the
    Habit docstring above). No row at all for a user means no restriction
    of that kind exists for them at all — there is no "empty work zone"
    sentinel to special-case.

    Deliberately excludes sleep: cool-down and wake-up aren't independent
    blocks, they're offsets from sleep's own boundaries, which a plain
    label/start/end/days shape can't express without inventing a
    relative-to-another-zone concept only sleep would ever use. See
    SleepSchedule below instead.
    """

    zone_id: str
    label: str
    start_time: str
    """24-hour "HH:MM" wall-clock time, e.g. "09:00"."""
    end_time: str
    days_of_week: list[str]
    """Subset of DAYS_OF_WEEK this zone applies to. A weekday-only zone
    simply doesn't restrict weekends — no separate weekend-awareness
    needed elsewhere for that case (see docs/todo.md §2)."""
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def from_dict(zone_id: str, data: dict) -> "Zone":
        return Zone(
            zone_id=zone_id,
            label=data["label"],
            start_time=data["start_time"],
            end_time=data["end_time"],
            days_of_week=data.get("days_of_week", []),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )


@dataclass(frozen=True)
class SleepSchedule:
    """A user's sleep/wake times, per day of week, plus the two windows
    derived from them — cool-down before sleep and a buffer after waking.
    A singleton per user (there's only ever one), unlike Zone's plain list.

    `sleep_time`/`wake_time` are the default applied to every day;
    `day_overrides` holds exceptions only for the days that actually
    differ (e.g. `{"sun": {"wake_time": "09:00"}}` for sleeping in on
    Sundays) — so the common case of one schedule all week needs no extra
    input. Any field left unset (None, or missing from day_overrides)
    means "not configured yet" rather than a hard-coded default — nothing
    here is enforced as a scheduling constraint until the user actually
    sets it, same as Zone's "no row = no restriction" rule.

    Deliberately doesn't support alternating/rotating schedules (e.g.
    night-shift every other week) — see docs/known-issues.md for why that
    needs a recurrence-rule engine out of scope here; a one-off shift is
    handled the same way any other single-occasion exception is, stated
    conversationally rather than encoded in the standing schedule.
    """

    sleep_time: str | None
    wake_time: str | None
    day_overrides: dict[str, dict[str, str]]
    cool_down_minutes: int | None
    wake_up_buffer_minutes: int | None
    created_at: datetime
    updated_at: datetime

    @staticmethod
    def from_dict(data: dict) -> "SleepSchedule":
        return SleepSchedule(
            sleep_time=data.get("sleep_time"),
            wake_time=data.get("wake_time"),
            day_overrides=data.get("day_overrides", {}),
            cool_down_minutes=data.get("cool_down_minutes"),
            wake_up_buffer_minutes=data.get("wake_up_buffer_minutes"),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )
