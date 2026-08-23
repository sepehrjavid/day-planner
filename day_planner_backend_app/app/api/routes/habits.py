"""User-facing habit management (A6.3).

Every handler here takes user_id from current_user_id (the session
token) only, never the request body — the same rule every other /me
route follows (see ../deps.py's own module docstring). Request/response
schemas are the ones schemas/habits.py has carried since A6.1 moved this
data here; /agent/habits (A6.2) uses its own Agent*-prefixed request
classes instead, since those additionally carry user_id in the body, but
both sides validate against the same HabitStatus definition — see
schemas/agent.py's own docstring.

No hard delete: update_habit's status field ("active"/"paused"/
"archived") is the retirement mechanism, so a habit_id referenced
elsewhere (a tagged calendar event, a habit session) always still
resolves. See schemas/habits.py's UpdateHabitRequest docstring.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from ...db.store import Store
from ...schemas.habits import (
    CreateHabitRequest,
    HabitOut,
    HabitsResponse,
    UpdateHabitRequest,
)
from ..deps import current_user_id, get_store

router = APIRouter(prefix="/me", tags=["habits"])


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
async def create_habit(
    body: CreateHabitRequest,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
):
    """Track a new recurring goal for the signed-in user."""
    habit = await store.create_habit(user_id=user_id, label=body.label, goal=body.goal)
    return _to_habit_out(habit)


@router.get("/habits", response_model=HabitsResponse)
async def list_habits(
    status: str | None = None,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
):
    """Every tracked habit for the signed-in user, optionally filtered to
    one status."""
    habits = await store.list_habits(user_id, status=status)
    return HabitsResponse(habits=[_to_habit_out(h) for h in habits])


@router.post("/habits/update", response_model=HabitOut)
async def update_habit(
    body: UpdateHabitRequest,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
):
    """Partial update of a tracked habit, e.g. to change its goal or
    retire it (status="paused"/"archived")."""
    habit = await store.update_habit(
        user_id=user_id,
        habit_id=body.habit_id,
        label=body.label,
        goal=body.goal,
        status=body.status,
        allowed_zones=body.allowed_zones,
    )
    if habit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _to_habit_out(habit)
