"""Plain data shapes for the scheduling package (A4.1).

Deliberately not the dict shapes zone_tools.py/habit_tools.py pass to and
from the model, and not day_planner_backend_app's own Firestore-facing
dataclasses either — this package has no agent, tool, or backend
dependency at all (see this package's own __init__.py). A future task
(A4.2) adapts those dict/dataclass shapes into these ones at the tool
boundary; nothing in here should ever import ADK, httpx, or Firestore.

Every wall-clock field (`start_time`, `end_time`, `sleep_time`,
`wake_time`) is a "HH:MM" string, exactly as stored — see zones.py and
sleep_schedule.py in day_planner_backend_app/app/schemas/ for the same
convention and TIME_PATTERN. None of these types carry a timezone of
their own: the functions that consume them take one explicitly (see
intervals.py), because nothing in this codebase's data model has a
per-user timezone yet (a gap already flagged in A6.3's zones.py
docstring) — resolving *which* timezone a given wall-clock string means
is the caller's job, not this package's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# Canonical day-of-week codes, Monday first — matches date.weekday()'s
# own 0-6 ordering and day_planner_backend_app's db/models.py
# DAYS_OF_WEEK, so a weekday index doubles as an index into this tuple.
DAYS_OF_WEEK = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Interval:
    """A concrete, tz-aware half-open time span [start, end).

    end == start is a valid, zero-length interval (e.g. a zone whose
    start_time equals its end_time, or a sleep schedule with
    cool_down_minutes=0) — it blocks nothing and collides with nothing.
    end < start is never valid; callers that need to express "wraps past
    midnight" construct two forward-only Intervals, or rely on the
    wall-clock helpers in intervals.py, which already do that.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(
                f"Interval end {self.end!r} is before start {self.start!r}; "
                "a wraparound must be expressed as two forward intervals."
            )

    @property
    def duration_minutes(self) -> float:
        """Real elapsed minutes, computed via UTC-normalized subtraction
        rather than `self.end - self.start` directly.

        This is not a style preference: CPython's datetime subtraction
        takes a fast path whenever both operands share the *identical*
        tzinfo object — true of every pair of zoneinfo.ZoneInfo("...")
        calls for the same key, since ZoneInfo caches and interns by key
        — and that fast path subtracts the naive wall-clock components
        directly, silently ignoring that a variable-offset zone's actual
        UTC offset differs between the two instants. Across a DST
        transition this is wrong: two midnights either side of a US
        spring-forward are 23 real hours apart, but `end - start` on
        zoneinfo-tagged datetimes reports 24. Converting both sides to a
        fixed-offset zone first (UTC) defeats that fast path and forces
        the correct, offset-aware subtraction. See test_scheduling.py's
        DST cases, which fail without this.
        """
        return (
            self.end.astimezone(timezone.utc) - self.start.astimezone(timezone.utc)
        ).total_seconds() / 60


@dataclass(frozen=True)
class Zone:
    """A named, recurring restriction — work hours, commute, or any other
    block of the week a user names. Mirrors
    day_planner_backend_app/app/schemas/zones.py's ZoneOut, minus the
    fields (zone_id, created_at, updated_at) this package has no use
    for."""

    label: str
    start_time: str
    end_time: str
    days_of_week: tuple[str, ...]


@dataclass(frozen=True)
class DayOverride:
    """A single day's replacement for the default sleep_time and/or
    wake_time — either field may be absent, meaning "use the default for
    this field on this day." Mirrors SleepScheduleOut's day_overrides
    value shape."""

    sleep_time: str | None = None
    wake_time: str | None = None


@dataclass(frozen=True)
class SleepSchedule:
    """Mirrors day_planner_backend_app/app/schemas/sleep_schedule.py's
    SleepScheduleOut, minus created_at/updated_at. sleep_time/wake_time
    are the default applied every day; day_overrides replaces one or
    both for specific day codes (see DayOverride). cool_down_minutes and
    wake_up_buffer_minutes are >= 0; 0 means that window is disabled
    (zero-length, blocks nothing) rather than absent."""

    sleep_time: str
    wake_time: str
    cool_down_minutes: int = 0
    wake_up_buffer_minutes: int = 0
    day_overrides: dict[str, DayOverride] = field(default_factory=dict)
