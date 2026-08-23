"""User-facing zone management (A6.3) — see ./habits.py's own docstring
for the current_user_id rule and the /me-vs-/agent schema split.

**Known gap, recorded rather than silently unhandled (A6.3 scope item
4):** instruction.md documents the agent checking a new or changed zone
against already-placed habit sessions before treating the change as
final, and reporting the resulting conflicts conversationally. This
route performs no equivalent check — a UI-driven create/update can
introduce a zone that now covers a session's planned_start/planned_end
with nothing surfacing that to the caller. Building it correctly needs a
per-user timezone, which nothing in this codebase's data model carries
yet (zones store wall-clock "HH:MM" strings; habit sessions store
UTC-aware datetimes — reconciling the two without a timezone is not
reliable enough to ship as a safety check). Revisit once a timezone
field exists on the user record.

Zones, unlike habits, have no soft-retire status and no referential
integrity of their own to protect (a habit's allowed_zones names a zone
by label, not by zone_id — see schemas/habits.py), so this is the one
domain in A6.3 that gets a real DELETE. See db/repositories/zones.py's
`ZoneRepository.delete` (A6.5).
"""

from fastapi import APIRouter, Depends, HTTPException, status

from ...db.store import Store
from ...schemas.zones import (
    CreateZoneRequest,
    UpdateZoneRequest,
    ZoneOut,
    ZonesResponse,
)
from ..deps import current_user_id, get_store

router = APIRouter(prefix="/me", tags=["zones"])


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
async def create_zone(
    body: CreateZoneRequest,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
):
    """Track a new named scheduling restriction for the signed-in user.
    See this module's docstring for the zone-conflict check this does
    NOT perform."""
    zone = await store.zones.create(
        user_id=user_id,
        label=body.label,
        start_time=body.start_time,
        end_time=body.end_time,
        days_of_week=body.days_of_week,
    )
    return _to_zone_out(zone)


@router.get("/zones", response_model=ZonesResponse)
async def list_zones(
    user_id: str = Depends(current_user_id), store: Store = Depends(get_store)
):
    """Every zone for the signed-in user."""
    zones = await store.zones.list(user_id)
    return ZonesResponse(zones=[_to_zone_out(z) for z in zones])


@router.post("/zones/update", response_model=ZoneOut)
async def update_zone(
    body: UpdateZoneRequest,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
):
    """Partial update of an existing zone. See this module's docstring
    for the zone-conflict check this does NOT perform."""
    zone = await store.zones.update(
        user_id=user_id,
        zone_id=body.zone_id,
        label=body.label,
        start_time=body.start_time,
        end_time=body.end_time,
        days_of_week=body.days_of_week,
    )
    if zone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _to_zone_out(zone)


@router.delete("/zones/{zone_id}", status_code=204)
async def delete_zone(
    zone_id: str,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
):
    """Remove a zone. No corresponding /agent route — deletion is not
    something the agent does on a user's behalf."""
    deleted = await store.zones.delete(user_id=user_id, zone_id=zone_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
