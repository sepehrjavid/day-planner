"""User-facing habit session completion route (A1.5).

Like every other /me route, user_id comes only from current_user_id (the
session token) and never from the request body — that's what makes
another user's session structurally unreachable here: the internal call
this makes is scoped to whatever user_id the caller resolved server-side,
and day_planner_backend_internal's own Firestore layout keys every
document under users/{user_id}/..., so there is no path in this codebase
from a verified caller identity to someone else's data.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from ...core.config import Settings, get_settings
from ...schemas.habit_sessions import HabitSessionOut, MarkHabitSessionRequest
from ...services import internal_client
from ..deps import current_user_id

router = APIRouter(prefix="/me", tags=["habit-sessions"])


@router.post("/habit-sessions/status", response_model=HabitSessionOut)
async def mark_habit_session(
    body: MarkHabitSessionRequest,
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
):
    """Mark a planned habit session completed or skipped. calendar_id and
    event_id identify which session, the same pair the agent already
    surfaces on habit sessions from get_calendar_events/review_habit_week.

    marked_by is hardcoded "user" here — never something the client
    supplies — since this route is specifically the human directly
    marking their own session; day_planner_agent's mark_habit_session tool
    calls the same underlying internal endpoint with "agent" instead.
    """
    session = await internal_client.set_habit_session_status(
        settings,
        user_id=user_id,
        calendar_id=body.calendar_id,
        event_id=body.event_id,
        status=body.status,
        marked_by="user",
    )
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return HabitSessionOut(**session)
