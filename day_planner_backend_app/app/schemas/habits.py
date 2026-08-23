"""Request/response models for habit domain operations.

Habits are the day planner's structured, addressable record of a user's
recurring goals — see ../../../docs/feature-ideas.md item 2 for why this
needed to exist as its own entity rather than living inside the free-text
preference profile in Memory Bank.

Moved here from day_planner_backend_internal by A6.1. Unlike that
service's identical-looking schemas, request models here carry no
user_id field — every route on this service resolves identity from
somewhere else (the session token for /me/*, an OIDC service identity
for /agent/*), never a request body field a caller could set to any
value. No route in this codebase uses these yet (A6.1 moves the data
and its shapes; A6.2/A6.3 build the routes that consume them) — see each
of those tasks in docs/roadmaps/1-agent.md for its own request schema,
since /me and /agent must never share one (A6.2's own explicit rule).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

HabitStatus = Literal["active", "paused", "archived"]


class CreateHabitRequest(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=2000)


class UpdateHabitRequest(BaseModel):
    habit_id: str = Field(min_length=1)
    label: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = Field(default=None, min_length=1, max_length=2000)
    status: HabitStatus | None = None
    allowed_zones: list[str] | None = None
    """Zone labels this habit may be placed in, in addition to any
    unzoned (open) time — see ../schemas/zones.py. Replaces the stored
    list wholesale when provided, same as day_overrides on
    SetSleepScheduleRequest; omit to leave it untouched, pass [] to clear
    it. Not on CreateHabitRequest — a habit starts with no zone
    exceptions and gains them, if any, through an explicit update."""


class HabitOut(BaseModel):
    habit_id: str
    label: str
    goal: str
    status: str
    created_at: datetime
    updated_at: datetime
    allowed_zones: list[str] = Field(default_factory=list)


class HabitsResponse(BaseModel):
    habits: list[HabitOut] = Field(default_factory=list)
