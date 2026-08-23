"""Coverage for zone_tools.py's orchestration: that user_id always comes
from tool_context (never a model-supplied argument), and that each tool
maps backend_client's response shapes — including "not found" on update
and "not configured yet" on the sleep schedule — into the right status.

backend_client's own HTTP mechanics aren't re-tested here —
day_planner_backend_internal's own test suite already covers
/internal/zones* and /internal/sleep-schedule* directly.
"""

import httpx
import pytest

from day_planner_agent import backend_client, zone_tools


async def test_create_zone_passes_through(tool_context, monkeypatch):
    seen = {}

    async def create_zone(user_id, *, label, start_time, end_time, days_of_week):
        seen["args"] = (user_id, label, start_time, end_time, days_of_week)
        return {
            "zone_id": "z1",
            "label": label,
            "start_time": start_time,
            "end_time": end_time,
            "days_of_week": days_of_week,
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z",
        }

    monkeypatch.setattr(backend_client, "create_zone", create_zone)

    result = await zone_tools.create_zone(
        tool_context, "Work", "09:00", "17:00", ["mon", "tue", "wed", "thu", "fri"]
    )
    assert result["status"] == "success"
    assert result["zone"]["zone_id"] == "z1"
    assert seen["args"] == (
        "user-1",
        "Work",
        "09:00",
        "17:00",
        ["mon", "tue", "wed", "thu", "fri"],
    )


async def test_list_zones_passes_through(tool_context, monkeypatch):
    async def list_zones(user_id):
        return [{"zone_id": "z1", "label": "Work"}]

    monkeypatch.setattr(backend_client, "list_zones", list_zones)

    result = await zone_tools.list_zones(tool_context)
    assert result == {"status": "success", "zones": [{"zone_id": "z1", "label": "Work"}]}


async def test_update_zone_no_fields_is_an_error(tool_context):
    result = await zone_tools.update_zone(tool_context, "z1")
    assert result["status"] == "error"


async def test_update_zone_not_found(tool_context, monkeypatch):
    async def update_zone(user_id, zone_id, **kwargs):
        return None

    monkeypatch.setattr(backend_client, "update_zone", update_zone)

    result = await zone_tools.update_zone(tool_context, "ghost", end_time="18:00")
    assert result["status"] == "not_found"


async def test_update_zone_success(tool_context, monkeypatch):
    seen = {}

    async def update_zone(user_id, zone_id, **kwargs):
        seen["args"] = (user_id, zone_id, kwargs)
        return {"zone_id": zone_id, "end_time": kwargs.get("end_time")}

    monkeypatch.setattr(backend_client, "update_zone", update_zone)

    result = await zone_tools.update_zone(tool_context, "z1", end_time="18:00")
    assert result == {"status": "success", "zone": {"zone_id": "z1", "end_time": "18:00"}}
    assert seen["args"] == (
        "user-1",
        "z1",
        {"label": None, "start_time": None, "end_time": "18:00", "days_of_week": None},
    )


# ---------------------------------------------------------------------------
# sleep schedule
# ---------------------------------------------------------------------------


async def test_get_sleep_schedule_not_configured(tool_context, monkeypatch):
    async def get_sleep_schedule(user_id):
        return None

    monkeypatch.setattr(backend_client, "get_sleep_schedule", get_sleep_schedule)

    result = await zone_tools.get_sleep_schedule(tool_context)
    assert result == {"status": "success", "exists": False}


async def test_get_sleep_schedule_exists(tool_context, monkeypatch):
    schedule = {
        "sleep_time": "23:00",
        "wake_time": "07:00",
        "day_overrides": {},
        "cool_down_minutes": 30,
        "wake_up_buffer_minutes": 15,
    }

    async def get_sleep_schedule(user_id):
        return schedule

    monkeypatch.setattr(backend_client, "get_sleep_schedule", get_sleep_schedule)

    result = await zone_tools.get_sleep_schedule(tool_context)
    assert result == {"status": "success", "exists": True, "schedule": schedule}


async def test_set_sleep_schedule_no_fields_is_an_error(tool_context):
    result = await zone_tools.set_sleep_schedule(tool_context)
    assert result["status"] == "error"


async def test_set_sleep_schedule_zero_minutes_is_not_treated_as_missing(
    tool_context, monkeypatch
):
    """cool_down_minutes=0 and wake_up_buffer_minutes=0 are meaningful
    values ("no buffer at all"), not the same as "not provided" — a
    naive truthiness check on these would wrongly reject this call."""
    seen = {}

    async def set_sleep_schedule(user_id, **kwargs):
        seen["kwargs"] = kwargs
        return {"cool_down_minutes": 0, "wake_up_buffer_minutes": 0}

    monkeypatch.setattr(backend_client, "set_sleep_schedule", set_sleep_schedule)

    result = await zone_tools.set_sleep_schedule(
        tool_context, cool_down_minutes=0, wake_up_buffer_minutes=0
    )
    assert result["status"] == "success"
    assert seen["kwargs"]["cool_down_minutes"] == 0
    assert seen["kwargs"]["wake_up_buffer_minutes"] == 0


async def test_set_sleep_schedule_success(tool_context, monkeypatch):
    seen = {}

    async def set_sleep_schedule(user_id, **kwargs):
        seen["args"] = (user_id, kwargs)
        return {"sleep_time": kwargs.get("sleep_time"), "wake_time": kwargs.get("wake_time")}

    monkeypatch.setattr(backend_client, "set_sleep_schedule", set_sleep_schedule)

    result = await zone_tools.set_sleep_schedule(
        tool_context, sleep_time="23:00", wake_time="07:00"
    )
    assert result == {
        "status": "success",
        "schedule": {"sleep_time": "23:00", "wake_time": "07:00"},
    }
    assert seen["args"] == (
        "user-1",
        {
            "sleep_time": "23:00",
            "wake_time": "07:00",
            "cool_down_minutes": None,
            "wake_up_buffer_minutes": None,
            "day_overrides": None,
        },
    )


async def test_user_id_comes_only_from_tool_context(tool_context, monkeypatch):
    """The whole tenant boundary: none of the zone tools take user_id as a
    parameter a model could fill in — confirm each call into
    backend_client is keyed on tool_context.session.user_id instead."""
    seen_user_ids = []

    async def list_zones(user_id):
        seen_user_ids.append(user_id)
        return []

    monkeypatch.setattr(backend_client, "list_zones", list_zones)

    await zone_tools.list_zones(tool_context)
    assert seen_user_ids == ["user-1"]

    for fn in (
        zone_tools.create_zone,
        zone_tools.list_zones,
        zone_tools.update_zone,
        zone_tools.get_sleep_schedule,
        zone_tools.set_sleep_schedule,
    ):
        assert "user_id" not in fn.__code__.co_varnames[: fn.__code__.co_argcount]


# ---------------------------------------------------------------------------
# A2.6: backend failures return {"status": "error", ...} instead of
# crashing the turn — never {"status": "success", "exists": False} or an
# empty list that would read as "the user has none".
# ---------------------------------------------------------------------------


async def test_create_zone_backend_failure_returns_error(tool_context, monkeypatch):
    async def create_zone(user_id, **kwargs):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(backend_client, "create_zone", create_zone)

    result = await zone_tools.create_zone(tool_context, "Work", "09:00", "17:00", ["mon"])
    assert result["status"] == "error"


async def test_list_zones_backend_failure_does_not_read_as_empty(tool_context, monkeypatch):
    async def list_zones(user_id):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(backend_client, "list_zones", list_zones)

    result = await zone_tools.list_zones(tool_context)
    assert result["status"] == "error"
    assert "zones" not in result
    assert "does not mean" in result["message"]


async def test_update_zone_backend_failure_returns_error(tool_context, monkeypatch):
    async def update_zone(user_id, zone_id, **kwargs):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(backend_client, "update_zone", update_zone)

    result = await zone_tools.update_zone(tool_context, "z1", end_time="18:00")
    assert result["status"] == "error"


async def test_get_sleep_schedule_backend_failure_omits_exists_key(tool_context, monkeypatch):
    async def get_sleep_schedule(user_id):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(backend_client, "get_sleep_schedule", get_sleep_schedule)

    result = await zone_tools.get_sleep_schedule(tool_context)
    assert result["status"] == "error"
    # Must never look like {"status": "success", "exists": False} — the
    # whole point of scope item 4 is that a failed fetch can't be
    # misread as "none is set".
    assert "exists" not in result


async def test_set_sleep_schedule_backend_failure_returns_error(tool_context, monkeypatch):
    async def set_sleep_schedule(user_id, **kwargs):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(backend_client, "set_sleep_schedule", set_sleep_schedule)

    result = await zone_tools.set_sleep_schedule(tool_context, sleep_time="23:00")
    assert result["status"] == "error"


async def test_list_zones_programming_error_still_propagates(tool_context, monkeypatch):
    """A2.6's scope item 3: only HTTP/network/auth classes are caught —
    a real bug (TypeError, KeyError) must keep surfacing loudly rather
    than being reported to the model as a backend hiccup."""

    async def list_zones(user_id):
        raise TypeError("not a backend failure")

    monkeypatch.setattr(backend_client, "list_zones", list_zones)

    with pytest.raises(TypeError):
        await zone_tools.list_zones(tool_context)
