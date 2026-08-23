"""The agent runtime's own path to domain data (A6.2) — habits, habit
sessions, zones, and the sleep schedule, moved here from
day_planner_backend_internal by A6.1.

Unlike /me/*, these take `user_id` in the request body — the caller is a
trusted service acting on a user's behalf. That trust has two
conditions: require_agent_caller (below, as a router-level dependency)
verifies the caller's own OIDC identity against an explicit allowlist,
and on the agent side the user_id value must come from
tool_context.session.user_id, never from anything the model produced.
See ../deps.py's own module docstring for why this must never share a
dependency, a router, or a user_id derivation with /me/*, and
../../schemas/agent.py for why the request schemas here are not the
same objects /me/* uses even where the shape looks identical.

Router-level dependency, not per-route (mirrors
day_planner_backend_internal's /internal router) — a new route added to
this file cannot be unprotected by omission.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status

from ...db.store import Store
from ...schemas.agent import (
    AgentCreateHabitRequest,
    AgentCreateZoneRequest,
    AgentSetHabitSessionStatusRequest,
    AgentSetSleepScheduleRequest,
    AgentUpdateHabitRequest,
    AgentUpdateZoneRequest,
    AgentUpsertHabitSessionRequest,
)
from ...schemas.habit_sessions import HabitSessionOut, HabitSessionsResponse
from ...schemas.habits import HabitOut, HabitsResponse
from ...schemas.sleep_schedule import SleepScheduleOut, SleepScheduleResponse
from ...schemas.zones import ZoneOut, ZonesResponse
from ..deps import get_store, require_agent_caller

router = APIRouter(
    prefix="/agent",
    tags=["agent"],
    dependencies=[Depends(require_agent_caller)],
)


def _to_habit_out(habit) -> HabitOut:
    return HabitOut(
        habit_id=habit.habit_id,
        label=habit.label,
        goal=habit.goal,
        status=habit.status,
        created_at=habit.created_at,
        updated_at=habit.updated_at,
        allowed_zones=habit.allowed_zones,
    )


@router.post("/habits", response_model=HabitOut)
async def create_habit(body: AgentCreateHabitRequest, store: Store = Depends(get_store)):
    """Track a new recurring goal for a user."""
    habit = await store.create_habit(user_id=body.user_id, label=body.label, goal=body.goal)
    return _to_habit_out(habit)


@router.get("/habits", response_model=HabitsResponse)
async def list_habits(
    user_id: str, status: str | None = None, store: Store = Depends(get_store)
):
    """Every tracked habit for a user, optionally filtered to one status.

    Filtering (and any "active by default" policy) is the agent tool's
    call, not this route's — this just passes the filter through as given.
    """
    habits = await store.list_habits(user_id, status=status)
    return HabitsResponse(habits=[_to_habit_out(h) for h in habits])


@router.post("/habits/update", response_model=HabitOut)
async def update_habit(body: AgentUpdateHabitRequest, store: Store = Depends(get_store)):
    """Partial update of a tracked habit, e.g. to change its goal or retire
    it (status="paused"/"archived") — never a hard delete, so anything that
    already referenced this habit_id (a tagged calendar event, a plan log
    entry) still resolves."""
    habit = await store.update_habit(
        user_id=body.user_id,
        habit_id=body.habit_id,
        label=body.label,
        goal=body.goal,
        status=body.status,
        allowed_zones=body.allowed_zones,
    )
    if habit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _to_habit_out(habit)


def _to_habit_session_out(session) -> HabitSessionOut:
    return HabitSessionOut(
        session_id=session.session_id,
        habit_id=session.habit_id,
        event_id=session.event_id,
        calendar_id=session.calendar_id,
        planned_start=session.planned_start,
        planned_end=session.planned_end,
        created_at=session.created_at,
        updated_at=session.updated_at,
        status=session.status,
        completed_at=session.completed_at,
        marked_by=session.marked_by,
    )


@router.post("/habit-sessions", response_model=HabitSessionOut)
async def upsert_habit_session(
    body: AgentUpsertHabitSessionRequest, store: Store = Depends(get_store)
):
    """Log (or, for an event already logged, update) the plan for one
    calendar event created for a habit. Called by add_calendar_event on
    creation and by update_calendar_event when it moves an already-tagged
    event — see calendar_tool.py."""
    session = await store.upsert_habit_session(
        user_id=body.user_id,
        habit_id=body.habit_id,
        event_id=body.event_id,
        calendar_id=body.calendar_id,
        planned_start=body.planned_start,
        planned_end=body.planned_end,
    )
    return _to_habit_session_out(session)


@router.get("/habit-sessions", response_model=HabitSessionsResponse)
async def list_habit_sessions(
    user_id: str,
    planned_from: datetime,
    planned_to: datetime,
    store: Store = Depends(get_store),
):
    """Every session planned to start in [planned_from, planned_to) —
    review_habit_week's input. Sessions whose event was later deleted are
    still returned; that's the entire point of this record surviving
    independently of the calendar event it describes."""
    sessions = await store.list_habit_sessions(
        user_id, planned_from=planned_from, planned_to=planned_to
    )
    return HabitSessionsResponse(sessions=[_to_habit_session_out(s) for s in sessions])


@router.post("/habit-sessions/status", response_model=HabitSessionOut)
async def set_habit_session_status(
    body: AgentSetHabitSessionStatusRequest, store: Store = Depends(get_store)
):
    """Explicitly mark a planned habit session completed, skipped, or
    back to pending — the first-class completion state review_habit_week
    reports alongside its calendar diff (A1.5). Called by
    day_planner_agent's mark_habit_session tool (marked_by="agent").
    Idempotent — see Store.set_habit_session_status."""
    session = await store.set_habit_session_status(
        user_id=body.user_id,
        calendar_id=body.calendar_id,
        event_id=body.event_id,
        status=body.status,
        marked_by=body.marked_by,
    )
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _to_habit_session_out(session)


def _to_zone_out(zone) -> ZoneOut:
    return ZoneOut(
        zone_id=zone.zone_id,
        label=zone.label,
        start_time=zone.start_time,
        end_time=zone.end_time,
        days_of_week=zone.days_of_week,
        created_at=zone.created_at,
        updated_at=zone.updated_at,
    )


@router.post("/zones", response_model=ZoneOut)
async def create_zone(body: AgentCreateZoneRequest, store: Store = Depends(get_store)):
    """Track a new named scheduling restriction for a user (work hours,
    commute, ...). A habit may only be placed inside it if the habit's
    own allowed_zones names this zone's label — see schemas/habits.py."""
    zone = await store.create_zone(
        user_id=body.user_id,
        label=body.label,
        start_time=body.start_time,
        end_time=body.end_time,
        days_of_week=body.days_of_week,
    )
    return _to_zone_out(zone)


@router.get("/zones", response_model=ZonesResponse)
async def list_zones(user_id: str, store: Store = Depends(get_store)):
    """Every zone for a user. No rows at all means no restriction of
    this kind exists for them — there's nothing else to check for."""
    zones = await store.list_zones(user_id)
    return ZonesResponse(zones=[_to_zone_out(z) for z in zones])


@router.post("/zones/update", response_model=ZoneOut)
async def update_zone(body: AgentUpdateZoneRequest, store: Store = Depends(get_store)):
    """Partial update of an existing zone."""
    zone = await store.update_zone(
        user_id=body.user_id,
        zone_id=body.zone_id,
        label=body.label,
        start_time=body.start_time,
        end_time=body.end_time,
        days_of_week=body.days_of_week,
    )
    if zone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _to_zone_out(zone)


def _to_sleep_schedule_out(schedule) -> SleepScheduleOut:
    return SleepScheduleOut(
        sleep_time=schedule.sleep_time,
        wake_time=schedule.wake_time,
        day_overrides=schedule.day_overrides,
        cool_down_minutes=schedule.cool_down_minutes,
        wake_up_buffer_minutes=schedule.wake_up_buffer_minutes,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


@router.get("/sleep-schedule", response_model=SleepScheduleResponse)
async def get_sleep_schedule(user_id: str, store: Store = Depends(get_store)):
    """The user's sleep schedule, if they've ever set one. exists=False
    (not a 404) when they haven't — this is a singleton with no id to
    get wrong, so "not configured yet" is a normal response, not an
    error."""
    schedule = await store.get_sleep_schedule(user_id)
    if schedule is None:
        return SleepScheduleResponse(exists=False)
    return SleepScheduleResponse(exists=True, schedule=_to_sleep_schedule_out(schedule))


@router.post("/sleep-schedule", response_model=SleepScheduleOut)
async def set_sleep_schedule(
    body: AgentSetSleepScheduleRequest, store: Store = Depends(get_store)
):
    """Create-or-update the user's sleep schedule. Partial like
    /habits/update, except there's no habit_id-style "must already
    exist" check — the first call for a user creates it."""
    day_overrides = None
    if body.day_overrides is not None:
        day_overrides = {
            day: override.model_dump(exclude_none=True)
            for day, override in body.day_overrides.items()
        }
    schedule = await store.set_sleep_schedule(
        user_id=body.user_id,
        sleep_time=body.sleep_time,
        wake_time=body.wake_time,
        cool_down_minutes=body.cool_down_minutes,
        wake_up_buffer_minutes=body.wake_up_buffer_minutes,
        day_overrides=day_overrides,
    )
    return _to_sleep_schedule_out(schedule)
