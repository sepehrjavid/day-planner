"""Habits (users/{user_id}/habits/{habit_id}).

Moved here from day_planner_backend_internal by A6.1 — see that task's
own reasoning in docs/roadmaps/1-agent.md: this is ordinary user data
with no credential exposure, unlike everything else that service
protects. Code-only move; no Firestore path changed.
"""

from __future__ import annotations

import uuid

from google.cloud import firestore

from ..models import HABIT_STATUS_ACTIVE, Habit, utcnow
from .users import USERS

HABITS = "habits"


class HabitRepository:
    def __init__(self, db: firestore.AsyncClient) -> None:
        self._db = db

    def _habits(self, user_id: str):
        return self._db.collection(USERS).document(user_id).collection(HABITS)

    async def create(self, *, user_id: str, label: str, goal: str) -> Habit:
        habit_id = uuid.uuid4().hex
        now = utcnow()
        payload = {
            "label": label,
            "goal": goal,
            "status": HABIT_STATUS_ACTIVE,
            "created_at": now,
            "updated_at": now,
        }
        await self._habits(user_id).document(habit_id).set(payload)
        return Habit.from_dict(habit_id, payload)

    async def list(self, user_id: str, *, status: str | None = None) -> list[Habit]:
        query = self._habits(user_id)
        if status is not None:
            query = query.where("status", "==", status)
        return [
            Habit.from_dict(doc.id, doc.to_dict() or {}) async for doc in query.stream()
        ]

    async def update(
        self,
        *,
        user_id: str,
        habit_id: str,
        label: str | None = None,
        goal: str | None = None,
        status: str | None = None,
        allowed_zones: list[str] | None = None,
    ) -> Habit | None:
        """Partial update. Returns None if habit_id doesn't exist for this
        user, so the route can turn that into a 404 rather than silently
        creating a new document under a caller-chosen id."""
        ref = self._habits(user_id).document(habit_id)
        snapshot = await ref.get()
        if not snapshot.exists:
            return None

        payload: dict = {"updated_at": utcnow()}
        if label is not None:
            payload["label"] = label
        if goal is not None:
            payload["goal"] = goal
        if status is not None:
            payload["status"] = status
        if allowed_zones is not None:
            payload["allowed_zones"] = allowed_zones
        await ref.set(payload, merge=True)

        updated = await ref.get()
        return Habit.from_dict(habit_id, updated.to_dict() or {})
