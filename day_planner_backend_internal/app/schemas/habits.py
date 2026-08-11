"""Request/response models for /internal/habits*.

Habits are the day planner's structured, addressable record of a user's
recurring goals — see ../../../docs/feature-ideas.md item 2 for why this
needed to exist as its own entity rather than living inside the free-text
preference profile in Memory Bank.

Like every other route in this service, identity is a request-body field
(InternalUserRequest, from ..schemas.calendars) filled from
tool_context.session.user_id on the agent side — never something the model
supplied. No path parameters anywhere here (habit_id lives in the body for
the update route), matching every other route in this codebase.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .calendars import InternalUserRequest

HabitStatus = Literal["active", "paused", "archived"]


class CreateHabitRequest(InternalUserRequest):
    label: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=2000)


class UpdateHabitRequest(InternalUserRequest):
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
