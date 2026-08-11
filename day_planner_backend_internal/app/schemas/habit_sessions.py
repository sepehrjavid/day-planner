"""Request/response models for /internal/habit-sessions*.

A habit session is the plan-log record review_habit_week diffs against
actual calendar state — see docs/feature-ideas.md item 2 and
db/models.py's HabitSession docstring for why it's a separate record
rather than something derived live from the calendar each time.

planned_start/planned_end are typed as datetime (not str) so Pydantic
parses whatever ISO8601 string the caller sends into a real, comparable
value before it ever reaches the Store — the same reasoning as
db/store.py's list_habit_sessions using a Firestore Timestamp range query
instead of a string comparison.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from .calendars import InternalUserRequest


class UpsertHabitSessionRequest(InternalUserRequest):
    habit_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    calendar_id: str = Field(min_length=1)
    planned_start: datetime
    planned_end: datetime


class HabitSessionOut(BaseModel):
    session_id: str
    habit_id: str
    event_id: str
    calendar_id: str
    planned_start: datetime
    planned_end: datetime
    created_at: datetime
    updated_at: datetime


class HabitSessionsResponse(BaseModel):
    sessions: list[HabitSessionOut] = Field(default_factory=list)
