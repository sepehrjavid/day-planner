"""Coverage of zone domain logic (moved from day_planner_backend_internal
by A6.1) — see test_habits.py's module docstring for why these exercise
the `store` fixture directly rather than through a route. Route-level
coverage (including delete_zone, new in A6.3) lives in
test_zones_routes.py and test_agent_routes.py.
"""

import pytest
from pydantic import ValidationError

from app.schemas.zones import CreateZoneRequest


async def test_create_zone_returns_a_stable_id(store):
    zone = await store.zones.create(
        user_id="u1",
        label="Work",
        start_time="09:00",
        end_time="17:00",
        days_of_week=["mon", "tue", "wed", "thu", "fri"],
    )
    assert zone.label == "Work"
    assert zone.days_of_week == ["mon", "tue", "wed", "thu", "fri"]
    assert zone.zone_id


def test_create_zone_request_rejects_malformed_time():
    """Schema-level validation — the shape a future route will get for
    free from CreateZoneRequest once one exists (A6.2/A6.3)."""
    with pytest.raises(ValidationError):
        CreateZoneRequest(label="Work", start_time="9am", end_time="17:00", days_of_week=["mon"])


async def test_list_zones_empty_by_default(store):
    assert await store.zones.list("u1") == []


async def test_list_zones_returns_created_and_scopes_by_user(store):
    await store.zones.create(
        user_id="u1", label="Work", start_time="09:00", end_time="17:00", days_of_week=["mon"]
    )
    # A different user's zones must never show up in this list.
    await store.zones.create(
        user_id="u2", label="Commute", start_time="08:00", end_time="09:00", days_of_week=["mon"]
    )

    zones = await store.zones.list("u1")
    assert {z.label for z in zones} == {"Work"}


async def test_update_zone_changes_fields(store):
    created = await store.zones.create(
        user_id="u1",
        label="Work",
        start_time="09:00",
        end_time="17:00",
        days_of_week=["mon", "tue", "wed", "thu", "fri"],
    )

    updated = await store.zones.update(user_id="u1", zone_id=created.zone_id, end_time="18:00")
    assert updated.end_time == "18:00"
    assert updated.start_time == "09:00"  # untouched field survives a partial update


async def test_update_zone_unknown_returns_none(store):
    assert await store.zones.update(user_id="u1", zone_id="ghost", end_time="18:00") is None


async def test_update_zone_wrong_user_returns_none(store):
    created = await store.zones.create(
        user_id="u1", label="Work", start_time="09:00", end_time="17:00", days_of_week=["mon"]
    )

    assert (
        await store.zones.update(user_id="u2", zone_id=created.zone_id, end_time="18:00") is None
    )
