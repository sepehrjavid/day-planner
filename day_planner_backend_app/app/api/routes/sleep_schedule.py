"""User-facing sleep-schedule management (A6.3) — see ./habits.py's own
docstring for the current_user_id rule and the /me-vs-/agent schema
split.

Singleton per user: GET reports exists=False rather than 404 when unset
— there's no id to get wrong — and POST is create-or-update. This is the
one domain where /me and /agent (A6.2) are almost byte-identical route
bodies, since there's no user_id-in-path-or-body distinction left once
the request schema itself already differs.
"""

from fastapi import APIRouter, Depends

from ...db.store import Store
from ...schemas.sleep_schedule import (
    SetSleepScheduleRequest,
    SleepScheduleOut,
    SleepScheduleResponse,
)
from ..deps import current_user_id, get_store

router = APIRouter(prefix="/me", tags=["sleep-schedule"])


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
async def get_sleep_schedule(
    user_id: str = Depends(current_user_id), store: Store = Depends(get_store)
):
    """The signed-in user's sleep schedule, if they've ever set one."""
    schedule = await store.get_sleep_schedule(user_id)
    if schedule is None:
        return SleepScheduleResponse(exists=False)
    return SleepScheduleResponse(exists=True, schedule=_to_sleep_schedule_out(schedule))


@router.post("/sleep-schedule", response_model=SleepScheduleOut)
async def set_sleep_schedule(
    body: SetSleepScheduleRequest,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
):
    """Create-or-update the signed-in user's sleep schedule."""
    day_overrides = None
    if body.day_overrides is not None:
        day_overrides = {
            day: override.model_dump(exclude_none=True)
            for day, override in body.day_overrides.items()
        }
    schedule = await store.set_sleep_schedule(
        user_id=user_id,
        sleep_time=body.sleep_time,
        wake_time=body.wake_time,
        cool_down_minutes=body.cool_down_minutes,
        wake_up_buffer_minutes=body.wake_up_buffer_minutes,
        day_overrides=day_overrides,
    )
    return _to_sleep_schedule_out(schedule)
