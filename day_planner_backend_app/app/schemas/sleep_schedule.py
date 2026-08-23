"""Request/response models for sleep-schedule domain operations.

Sleep is a singleton, separate from the generic Zone shape — cool-down
and wake-up are offsets from sleep's own boundaries, not independent
zones, so this gets its own request/response pair instead of reusing
CreateZoneRequest/ZoneOut. See ../../../docs/todo.md §1's "Sleep is a
special, singular zone" section and db/models.py's SleepSchedule
docstring for the full reasoning.

Moved here from day_planner_backend_internal by A6.1 — see
./habits.py's module docstring for why the request model here carries
no user_id field, unlike that service's identical-looking schema. Used
directly by /me/sleep-schedule (A6.3); /agent/sleep-schedule (A6.2) uses
its own Agent*-prefixed request class instead, built from the same
`TIME_PATTERN`/`DayOfWeek` imported here rather than a second copy.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from .zones import TIME_PATTERN, DayOfWeek


class DayOverride(BaseModel):
    sleep_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    wake_time: str | None = Field(default=None, pattern=TIME_PATTERN)


class SetSleepScheduleRequest(BaseModel):
    sleep_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    wake_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    cool_down_minutes: int | None = Field(default=None, ge=0, le=600)
    wake_up_buffer_minutes: int | None = Field(default=None, ge=0, le=600)
    day_overrides: dict[DayOfWeek, DayOverride] | None = None
    """Replaces the stored overrides map wholesale when provided — not a
    per-day merge. Omit entirely to leave existing overrides untouched;
    pass {} to clear all of them."""


class SleepScheduleOut(BaseModel):
    sleep_time: str | None
    wake_time: str | None
    day_overrides: dict[str, DayOverride]
    cool_down_minutes: int | None
    wake_up_buffer_minutes: int | None
    created_at: datetime
    updated_at: datetime


class SleepScheduleResponse(BaseModel):
    exists: bool
    schedule: SleepScheduleOut | None = None
