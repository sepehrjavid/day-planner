"""Request models for /agent/* — the agent runtime's own path to domain
data (A6.2), distinct from /me/*'s schemas in ../habits.py, ../zones.py,
../sleep_schedule.py, and ../habit_sessions.py.

Unlike those, every request here takes `user_id` in the body —
AgentUserRequest below, mirroring day_planner_backend_internal's own
InternalUserRequest. That's not an oversight or a shortcut: the caller
is a trusted service (verified by require_agent_caller's OIDC check)
acting on a user's behalf, the same trust model /internal/* already
used before A6.1 moved this data here. On the agent side, the value
must come from `tool_context.session.user_id`, which Agent Engine sets
from the invocation and never from anything the model produced — see
day_planner_agent/zone_tools.py and habit_tools.py.

A6.2's own scope is explicit: do not reuse /me schemas for agent routes
if they differ in user_id handling — a shared schema is how the two
auth models leak into each other. Response shapes (HabitOut, ZoneOut,
SleepScheduleOut, HabitSessionOut, and their *Response wrappers) carry
no user_id either way, so those are reused as-is from the /me schema
modules rather than duplicated here. The same goes for the value types
below that aren't user_id-shaped at all — `HabitStatus`,
`HabitSessionStatus`, `TIME_PATTERN`, and `DayOfWeek` are imported from
the /me schema modules rather than redefined, so /me and /agent can never
independently drift on what counts as a valid status or a valid
days_of_week (A6.3's own explicit requirement). `MarkedBy` has no /me
equivalent to import — day_planner_backend_app/app/api/routes/habit_sessions.py
hardcodes "user" as a Python literal rather than accepting it as a field.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .habit_sessions import HabitSessionStatus
from .habits import HabitStatus
from .zones import TIME_PATTERN, DayOfWeek


class AgentUserRequest(BaseModel):
    # Must be tool_context.session.user_id on the caller's side, never a
    # model-supplied value — this is the whole tenant boundary. See this
    # module's own docstring.
    user_id: str = Field(min_length=1, max_length=256)


class AgentCreateHabitRequest(AgentUserRequest):
    label: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=2000)


class AgentUpdateHabitRequest(AgentUserRequest):
    habit_id: str = Field(min_length=1)
    label: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = Field(default=None, min_length=1, max_length=2000)
    status: HabitStatus | None = None
    allowed_zones: list[str] | None = None


MarkedBy = Literal["user", "agent"]


class AgentUpsertHabitSessionRequest(AgentUserRequest):
    habit_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    calendar_id: str = Field(min_length=1)
    planned_start: datetime
    planned_end: datetime


class AgentSetHabitSessionStatusRequest(AgentUserRequest):
    calendar_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    status: HabitSessionStatus
    # Declared by the caller (day_planner_agent's mark_habit_session tool
    # hardcodes "agent"; day_planner_backend_app's own /me route hardcodes
    # "user" — never taken from an end user's own request body or a model
    # argument), same trust pattern as user_id above.
    marked_by: MarkedBy


class AgentCreateZoneRequest(AgentUserRequest):
    label: str = Field(min_length=1, max_length=200)
    start_time: str = Field(pattern=TIME_PATTERN)
    end_time: str = Field(pattern=TIME_PATTERN)
    days_of_week: list[DayOfWeek] = Field(min_length=1)


class AgentUpdateZoneRequest(AgentUserRequest):
    zone_id: str = Field(min_length=1)
    label: str | None = Field(default=None, min_length=1, max_length=200)
    start_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    end_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    days_of_week: list[DayOfWeek] | None = Field(default=None, min_length=1)


class AgentDayOverride(BaseModel):
    sleep_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    wake_time: str | None = Field(default=None, pattern=TIME_PATTERN)


class AgentSetSleepScheduleRequest(AgentUserRequest):
    sleep_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    wake_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    cool_down_minutes: int | None = Field(default=None, ge=0, le=600)
    wake_up_buffer_minutes: int | None = Field(default=None, ge=0, le=600)
    day_overrides: dict[DayOfWeek, AgentDayOverride] | None = None
