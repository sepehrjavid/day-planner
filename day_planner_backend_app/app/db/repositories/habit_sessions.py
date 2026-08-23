"""Habit sessions (users/{user_id}/habit_sessions/{session_id}) — the plan
log review_habit_week diffs against actual calendar state.
"""

from __future__ import annotations

from datetime import datetime

from google.cloud import firestore

from ..models import (
    HABIT_SESSION_STATUS_COMPLETED,
    HABIT_SESSION_STATUS_PENDING,
    HabitSession,
    habit_session_id_for,
    utcnow,
)
from .users import USERS

HABIT_SESSIONS = "habit_sessions"


class HabitSessionRepository:
    def __init__(self, db: firestore.AsyncClient) -> None:
        self._db = db

    def _habit_sessions(self, user_id: str):
        return self._db.collection(USERS).document(user_id).collection(HABIT_SESSIONS)

    async def upsert(
        self,
        *,
        user_id: str,
        habit_id: str,
        event_id: str,
        calendar_id: str,
        planned_start: datetime,
        planned_end: datetime,
    ) -> HabitSession:
        """Create a session record, or — for the same (calendar_id,
        event_id), e.g. after the agent reschedules its own event — update
        its plan in place. created_at is set once and preserved across
        later upserts; everything else always reflects the latest plan.

        status/completed_at/marked_by (A1.5) are deliberately absent from
        `payload` below — merge=True then leaves them completely untouched
        on an existing document, which is what makes completion survive a
        reschedule (see HabitSession's docstring for the invariant this
        protects). Only a brand-new document gets an explicit starting
        status, since there's no prior value to preserve.

        A6.5 note: this omission is the single most dangerous line in the
        whole repository split to get wrong. Do not "complete" this
        payload with status/completed_at/marked_by defaults — that would
        silently destroy completion state on every reschedule. See
        test_habit_sessions_store.py's reschedule-survival test, the
        canary for this invariant."""
        session_id = habit_session_id_for(calendar_id, event_id)
        ref = self._habit_sessions(user_id).document(session_id)

        existing = await ref.get()
        now = utcnow()
        payload = {
            "habit_id": habit_id,
            "event_id": event_id,
            "calendar_id": calendar_id,
            "planned_start": planned_start,
            "planned_end": planned_end,
            "updated_at": now,
        }
        if not existing.exists:
            payload["created_at"] = now
            payload["status"] = HABIT_SESSION_STATUS_PENDING
        await ref.set(payload, merge=True)

        updated = await ref.get()
        return HabitSession.from_dict(session_id, updated.to_dict() or {})

    async def set_status(
        self,
        *,
        user_id: str,
        calendar_id: str,
        event_id: str,
        status: str,
        marked_by: str,
    ) -> HabitSession | None:
        """Explicitly mark a session's completion state. Returns None if no
        session exists for this (calendar_id, event_id) under this user, so
        the route can turn that into a 404 rather than creating a record
        via a side door that skips upsert's own plan fields.

        Idempotent: calling this again with the *same* status is a true
        no-op — the existing record is returned unchanged, without even a
        write, so completed_at doesn't keep drifting forward on repeated
        calls. completed_at is only ever set when transitioning *to*
        completed; transitioning to skipped clears it, since it would
        otherwise misreport when a since-abandoned completion happened.
        """
        session_id = habit_session_id_for(calendar_id, event_id)
        ref = self._habit_sessions(user_id).document(session_id)

        snapshot = await ref.get()
        if not snapshot.exists:
            return None

        current = snapshot.to_dict() or {}
        if current.get("status") == status:
            return HabitSession.from_dict(session_id, current)

        now = utcnow()
        payload = {
            "status": status,
            "marked_by": marked_by,
            "updated_at": now,
            "completed_at": now if status == HABIT_SESSION_STATUS_COMPLETED else None,
        }
        await ref.set(payload, merge=True)

        updated = await ref.get()
        return HabitSession.from_dict(session_id, updated.to_dict() or {})

    async def list(
        self, user_id: str, *, planned_from: datetime, planned_to: datetime
    ) -> list[HabitSession]:
        """Every session planned to start in [planned_from, planned_to) —
        a native Firestore Timestamp range query, not a string comparison,
        so this stays correct regardless of which UTC offset a given
        session's planned_start happens to carry."""
        query = (
            self._habit_sessions(user_id)
            .where("planned_start", ">=", planned_from)
            .where("planned_start", "<", planned_to)
        )
        return [
            HabitSession.from_dict(doc.id, doc.to_dict() or {})
            async for doc in query.stream()
        ]
