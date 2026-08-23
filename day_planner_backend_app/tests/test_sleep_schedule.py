"""Coverage of sleep-schedule domain logic (moved from
day_planner_backend_internal by A6.1) — see test_habits.py's module
docstring for why these exercise the `store` fixture directly rather
than through a route. Route-level coverage lives in
test_sleep_schedule_routes.py and test_agent_routes.py.
"""


async def test_get_sleep_schedule_reports_not_configured(store):
    assert await store.get_sleep_schedule("u1") is None


async def test_set_sleep_schedule_creates(store):
    schedule = await store.set_sleep_schedule(
        user_id="u1",
        sleep_time="23:00",
        wake_time="07:00",
        cool_down_minutes=30,
        wake_up_buffer_minutes=15,
    )
    assert schedule.sleep_time == "23:00"
    assert schedule.wake_time == "07:00"
    assert schedule.day_overrides == {}


async def test_set_sleep_schedule_partial_update_preserves_other_fields(store):
    await store.set_sleep_schedule(
        user_id="u1",
        sleep_time="23:00",
        wake_time="07:00",
        cool_down_minutes=30,
        wake_up_buffer_minutes=15,
    )

    updated = await store.set_sleep_schedule(user_id="u1", cool_down_minutes=45)
    assert updated.cool_down_minutes == 45
    assert updated.sleep_time == "23:00"  # untouched field survives a partial update


async def test_set_sleep_schedule_day_overrides_round_trip(store):
    schedule = await store.set_sleep_schedule(
        user_id="u1",
        sleep_time="23:00",
        wake_time="07:00",
        cool_down_minutes=30,
        wake_up_buffer_minutes=15,
        day_overrides={"sun": {"wake_time": "09:00"}},
    )
    assert schedule.day_overrides == {"sun": {"wake_time": "09:00"}}

    fetched = await store.get_sleep_schedule("u1")
    assert fetched is not None
    assert fetched.day_overrides == {"sun": {"wake_time": "09:00"}}


async def test_set_sleep_schedule_day_overrides_replaces_wholesale(store):
    """day_overrides isn't a per-day merge — passing a new map drops
    whatever wasn't included, matching the documented contract on
    SetSleepScheduleRequest.day_overrides."""
    await store.set_sleep_schedule(
        user_id="u1",
        sleep_time="23:00",
        wake_time="07:00",
        cool_down_minutes=30,
        wake_up_buffer_minutes=15,
        day_overrides={"sun": {"wake_time": "09:00"}, "sat": {"wake_time": "10:00"}},
    )

    updated = await store.set_sleep_schedule(
        user_id="u1", day_overrides={"sun": {"wake_time": "09:30"}}
    )
    assert updated.day_overrides == {"sun": {"wake_time": "09:30"}}
