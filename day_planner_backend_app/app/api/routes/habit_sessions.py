"""User-facing habit session routes: completion (A1.5) and listing
(A6.3).

Like every other /me route, user_id comes only from current_user_id (the
session token) and never from the request body — that's what makes
another user's session structurally unreachable here.

A6.1: mark_habit_session used to proxy to day_planner_backend_internal's
/internal/habit-sessions/status over HTTP (see git history's
services/internal_client.py, now deleted). Habit session data moved to
this service's own Store, so this calls it directly — same Firestore
document, same users/{user_id}/... scoping, one fewer network hop and
one fewer OIDC token to mint. list_habit_sessions (A6.3) is a new
listing route with no predecessor on day_planner_backend_internal — the
UI's own equivalent of what review_habit_week reads via /agent's own
list route.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status

from ...db.store import Store
from ...schemas.habit_sessions import (
    HabitSessionOut,
    HabitSessionsResponse,
    MarkHabitSessionRequest,
)
from ..deps import current_user_id, get_store

router = APIRouter(prefix="/me", tags=["habit-sessions"])


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


@router.get("/habit-sessions", response_model=HabitSessionsResponse)
async def list_habit_sessions(
    planned_from: datetime,
    planned_to: datetime,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
):
    """Every session for the signed-in user planned to start in
    [planned_from, planned_to) — the same range query /agent/habit-sessions
    (A6.2) uses, scoped here to current_user_id instead of a body field."""
    sessions = await store.habit_sessions.list(
        user_id, planned_from=planned_from, planned_to=planned_to
    )
    return HabitSessionsResponse(sessions=[_to_habit_session_out(s) for s in sessions])


@router.post("/habit-sessions/status", response_model=HabitSessionOut)
async def mark_habit_session(
    body: MarkHabitSessionRequest,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
):
    """Mark a planned habit session completed, skipped, or back to
    pending (correcting a mis-mark). calendar_id and event_id identify
    which session, the same pair the agent already surfaces on habit
    sessions from get_calendar_events/review_habit_week.

    marked_by is hardcoded "user" here — never something the client
    supplies — since this route is specifically the human directly
    marking their own session; day_planner_agent's mark_habit_session tool
    calls store.habit_sessions.set_status with "agent" instead, via its
    own path to this data (A6.2).
    """
    session = await store.habit_sessions.set_status(
        user_id=user_id,
        calendar_id=body.calendar_id,
        event_id=body.event_id,
        status=body.status,
        marked_by="user",
    )
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _to_habit_session_out(session)
