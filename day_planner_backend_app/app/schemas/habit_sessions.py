"""Request/response models for /me/habit-sessions/status (A1.5).

status only accepts "completed"/"skipped" — "pending" is the implicit
starting state a session already has, never something to mark it back to
(no un-marking; see day_planner_backend_internal's identical
HabitSessionStatus for the same reasoning). marked_by is not a field a
client can set — the route hardcodes "user", since this is the human
directly marking their own session, mirroring day_planner_agent's
mark_habit_session tool hardcoding "agent" on its side of the same
underlying /internal/habit-sessions/status call.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

HabitSessionStatus = Literal["completed", "skipped"]


class MarkHabitSessionRequest(BaseModel):
    calendar_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    status: HabitSessionStatus


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
