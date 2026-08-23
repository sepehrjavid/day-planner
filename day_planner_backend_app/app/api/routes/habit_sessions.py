"""User-facing habit session completion route (A1.5).

Like every other /me route, user_id comes only from current_user_id (the
session token) and never from the request body — that's what makes
another user's session structurally unreachable here.

A6.1: this used to proxy to day_planner_backend_internal's
/internal/habit-sessions/status over HTTP (see git history's
services/internal_client.py, now deleted). Habit session data moved to
this service's own Store, so this calls it directly — same Firestore
document, same users/{user_id}/... scoping, one fewer network hop and
one fewer OIDC token to mint.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from ...db.store import Store
from ...schemas.habit_sessions import HabitSessionOut, MarkHabitSessionRequest
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
    calls Store.set_habit_session_status with "agent" instead, via its
    own path to this data (A6.2).
    """
    session = await store.set_habit_session_status(
        user_id=user_id,
        calendar_id=body.calendar_id,
        event_id=body.event_id,
        status=body.status,
        marked_by="user",
    )
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _to_habit_session_out(session)
