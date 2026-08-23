"""Request/response models for habit-session domain operations, including
/me/habit-sessions/status (A1.5).

status accepts "pending" too, not just "completed"/"skipped" — resetting
back to unknown (correcting a mis-mark) is itself an explicit action, the
same as any other transition. marked_by is not a field MarkHabitSessionRequest
lets a client set — the route hardcodes "user", since this is the human
directly marking their own session, mirroring day_planner_agent's
mark_habit_session tool hardcoding "agent" on its side of the same
underlying Store.set_habit_session_status call.

UpsertHabitSessionRequest was moved here from day_planner_backend_internal
by A6.1 alongside the store logic it backs; no /me route uses it (only
the agent tags calendar events with a habit session — see
schemas/agent.py's AgentUpsertHabitSessionRequest for its /agent-side
counterpart). HabitSessionsResponse backs /me/habit-sessions' list route
(A6.3) as well as /agent/habit-sessions' — see ../schemas/habits.py's
module docstring for why request models here carry no user_id field
unlike day_planner_backend_internal's identical-looking schemas.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

HabitSessionStatus = Literal["pending", "completed", "skipped"]


class MarkHabitSessionRequest(BaseModel):
    calendar_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    status: HabitSessionStatus


class UpsertHabitSessionRequest(BaseModel):
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
    status: str
    completed_at: datetime | None = None
    marked_by: str | None = None


class HabitSessionsResponse(BaseModel):
    sessions: list[HabitSessionOut] = Field(default_factory=list)
