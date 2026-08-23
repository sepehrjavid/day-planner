"""Zones (users/{user_id}/zones/{zone_id})."""

from __future__ import annotations

import uuid

from google.cloud import firestore

from ..models import Zone, utcnow
from .users import USERS

ZONES = "zones"


class ZoneRepository:
    def __init__(self, db: firestore.AsyncClient) -> None:
        self._db = db

    def _zones(self, user_id: str):
        return self._db.collection(USERS).document(user_id).collection(ZONES)

    async def create(
        self,
        *,
        user_id: str,
        label: str,
        start_time: str,
        end_time: str,
        days_of_week: list[str],
    ) -> Zone:
        zone_id = uuid.uuid4().hex
        now = utcnow()
        payload = {
            "label": label,
            "start_time": start_time,
            "end_time": end_time,
            "days_of_week": days_of_week,
            "created_at": now,
            "updated_at": now,
        }
        await self._zones(user_id).document(zone_id).set(payload)
        return Zone.from_dict(zone_id, payload)

    async def list(self, user_id: str) -> list[Zone]:
        return [
            Zone.from_dict(doc.id, doc.to_dict() or {})
            async for doc in self._zones(user_id).stream()
        ]

    async def update(
        self,
        *,
        user_id: str,
        zone_id: str,
        label: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        days_of_week: list[str] | None = None,
    ) -> Zone | None:
        """Partial update. Returns None if zone_id doesn't exist for this
        user, same 404-vs-silent-create reasoning as HabitRepository.update."""
        ref = self._zones(user_id).document(zone_id)
        snapshot = await ref.get()
        if not snapshot.exists:
            return None

        payload: dict = {"updated_at": utcnow()}
        if label is not None:
            payload["label"] = label
        if start_time is not None:
            payload["start_time"] = start_time
        if end_time is not None:
            payload["end_time"] = end_time
        if days_of_week is not None:
            payload["days_of_week"] = days_of_week
        await ref.set(payload, merge=True)

        updated = await ref.get()
        return Zone.from_dict(zone_id, updated.to_dict() or {})

    async def delete(self, *, user_id: str, zone_id: str) -> bool:
        """True if a zone existed and was deleted, False if there was
        nothing to delete for this user — lets the route 404 instead of
        reporting success for an id that was never there. Unlike habits,
        zones have no soft-retire status and no referential-integrity
        concern of their own (a habit's allowed_zones names a zone by
        label, not by zone_id — see schemas/habits.py), so a hard delete
        is safe here (A6.3)."""
        ref = self._zones(user_id).document(zone_id)
        snapshot = await ref.get()
        if not snapshot.exists:
            return False
        await ref.delete()
        return True
