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
from typing import Literal

from pydantic import BaseModel, Field

from .calendars import InternalUserRequest

# "pending" is deliberately not settable here — it's the implicit starting
# state (see db/models.py's HABIT_SESSION_STATUS_PENDING), never something
# to explicitly mark back to. No un-marking; see A1.5's "explicit marks
# only" scope boundary.
HabitSessionStatus = Literal["completed", "skipped"]
MarkedBy = Literal["user", "agent"]


class UpsertHabitSessionRequest(InternalUserRequest):
    habit_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    calendar_id: str = Field(min_length=1)
    planned_start: datetime
    planned_end: datetime


class SetHabitSessionStatusRequest(InternalUserRequest):
    calendar_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    status: HabitSessionStatus
    # Declared by the caller (day_planner_agent's tool hardcodes "agent";
    # day_planner_backend_app's user-facing route hardcodes "user" — never
    # taken from an end user's own request body or a model argument), the
    # same trust pattern as user_id on every other route here.
    marked_by: MarkedBy


class HabitSessionOut(BaseModel):
    session_id: str
    habit_id: str
    event_id: str
    calendar_id: str
    planned_start: datetime
    planned_end: datetime
    created_at: datetime
    updated_at: datetime
    status: str
    completed_at: datetime | None = None
    marked_by: str | None = None


class HabitSessionsResponse(BaseModel):
    sessions: list[HabitSessionOut] = Field(default_factory=list)
