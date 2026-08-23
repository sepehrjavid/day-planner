"""Request/response models for zone domain operations.

Zones are the day planner's structured replacement for prose-parsed
scheduling constraints like "I work 9-5 weekdays" — see
../../../docs/todo.md §1 for why these needed to exist as their own
entity rather than living inside the free-text preference profile in
Memory Bank.

Moved here from day_planner_backend_internal by A6.1 — see
./habits.py's module docstring for why request models here carry no
user_id field, unlike that service's identical-looking schemas.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

DayOfWeek = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# 24-hour "HH:MM" wall-clock time, e.g. "09:00" or "17:30". Public so
# ./sleep_schedule.py can reuse the exact same rule rather than risking
# a second regex drifting out of sync with this one.
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


class CreateZoneRequest(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    start_time: str = Field(pattern=TIME_PATTERN)
    end_time: str = Field(pattern=TIME_PATTERN)
    days_of_week: list[DayOfWeek] = Field(min_length=1)


class UpdateZoneRequest(BaseModel):
    zone_id: str = Field(min_length=1)
    label: str | None = Field(default=None, min_length=1, max_length=200)
    start_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    end_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    days_of_week: list[DayOfWeek] | None = Field(default=None, min_length=1)


class ZoneOut(BaseModel):
    zone_id: str
    label: str
    start_time: str
    end_time: str
    days_of_week: list[str]
    created_at: datetime
    updated_at: datetime


class ZonesResponse(BaseModel):
    zones: list[ZoneOut] = Field(default_factory=list)
