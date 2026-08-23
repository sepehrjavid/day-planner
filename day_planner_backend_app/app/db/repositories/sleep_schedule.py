"""Sleep schedule (users/{user_id}/sleep_schedule/{fixed id}) — singleton
per user."""

from __future__ import annotations

from google.cloud import firestore

from ..models import SleepSchedule, utcnow
from .users import USERS

SLEEP_SCHEDULE = "sleep_schedule"
SLEEP_SCHEDULE_DOC_ID = "current"


class SleepScheduleRepository:
    def __init__(self, db: firestore.AsyncClient) -> None:
        self._db = db

    def _ref(self, user_id: str):
        return (
            self._db.collection(USERS)
            .document(user_id)
            .collection(SLEEP_SCHEDULE)
            .document(SLEEP_SCHEDULE_DOC_ID)
        )

    async def get(self, user_id: str) -> SleepSchedule | None:
        snapshot = await self._ref(user_id).get()
        if not snapshot.exists:
            return None
        return SleepSchedule.from_dict(snapshot.to_dict() or {})

    async def set(
        self,
        *,
        user_id: str,
        sleep_time: str | None = None,
        wake_time: str | None = None,
        cool_down_minutes: int | None = None,
        wake_up_buffer_minutes: int | None = None,
        day_overrides: dict[str, dict[str, str]] | None = None,
    ) -> SleepSchedule:
        """Create-or-update, unlike ZoneRepository.update/HabitRepository
        .update — there's always exactly one sleep schedule per user, so
        the first call naturally creates it rather than needing a separate
        create step. Partial update like the others; day_overrides
        replaces the whole map when provided rather than merging per-day,
        so clearing an override means passing the full remaining set
        back, not just the one key you want gone."""
        ref = self._ref(user_id)
        existing = await ref.get()
        now = utcnow()

        payload: dict = {"updated_at": now}
        if not existing.exists:
            payload["created_at"] = now
        if sleep_time is not None:
            payload["sleep_time"] = sleep_time
        if wake_time is not None:
            payload["wake_time"] = wake_time
        if cool_down_minutes is not None:
            payload["cool_down_minutes"] = cool_down_minutes
        if wake_up_buffer_minutes is not None:
            payload["wake_up_buffer_minutes"] = wake_up_buffer_minutes
        if day_overrides is not None:
            payload["day_overrides"] = day_overrides
        await ref.set(payload, merge=True)

        updated = await ref.get()
        return SleepSchedule.from_dict(updated.to_dict() or {})
