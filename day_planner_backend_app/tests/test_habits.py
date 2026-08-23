"""Coverage of habit domain logic (moved from day_planner_backend_internal
by A6.1).

Both /agent/habits (A6.2) and /me/habits (A6.3) now call
store.habits.create/list/update — see test_agent_routes.py
and test_habits_routes.py for the auth-gate and HTTP-mapping coverage on
each of those paths. These tests exercise the `store` fixture's own
habit methods directly instead, which is what's left once the route-level
suites cover request validation and status mapping: the store's own
behavior (stable ids, partial-update semantics, unknown-id handling)
independent of which caller reached it.
"""


async def test_create_habit_returns_a_stable_id(store):
    habit = await store.habits.create(user_id="u1", label="Gym", goal="180 min/week")
    assert habit.label == "Gym"
    assert habit.status == "active"
    assert habit.habit_id


async def test_create_habit_defaults_allowed_zones_to_empty(store):
    habit = await store.habits.create(user_id="u1", label="Gym", goal="3x/week")
    assert habit.allowed_zones == []


async def test_list_habits_empty_by_default(store):
    assert await store.habits.list("u1") == []


async def test_list_habits_returns_created_and_scopes_by_user(store):
    await store.habits.create(user_id="u1", label="Gym", goal="3x/week")
    await store.habits.create(user_id="u1", label="Reading", goal="nightly")
    # A different user's habits must never show up in this list.
    await store.habits.create(user_id="u2", label="Meditation", goal="daily")

    habits = await store.habits.list("u1")
    assert {h.label for h in habits} == {"Gym", "Reading"}


async def test_list_habits_filters_by_status(store):
    created = await store.habits.create(user_id="u1", label="Gym", goal="3x/week")
    await store.habits.create(user_id="u1", label="Reading", goal="nightly")
    await store.habits.update(user_id="u1", habit_id=created.habit_id, status="paused")

    active = await store.habits.list("u1", status="active")
    paused = await store.habits.list("u1", status="paused")
    assert [h.label for h in active] == ["Reading"]
    assert [h.label for h in paused] == ["Gym"]


async def test_update_habit_changes_fields(store):
    created = await store.habits.create(user_id="u1", label="Gym", goal="3x/week")

    updated = await store.habits.update(
        user_id="u1", habit_id=created.habit_id, goal="5x/week now", status="active"
    )
    assert updated.goal == "5x/week now"
    assert updated.label == "Gym"  # untouched field survives a partial update


async def test_update_habit_unknown_returns_none(store):
    assert await store.habits.update(user_id="u1", habit_id="ghost", goal="x") is None


async def test_update_habit_sets_allowed_zones(store):
    created = await store.habits.create(user_id="u1", label="Gym", goal="3x/week")

    updated = await store.habits.update(
        user_id="u1", habit_id=created.habit_id, allowed_zones=["Work"]
    )
    assert updated.allowed_zones == ["Work"]


async def test_update_habit_wrong_user_returns_none(store):
    """A habit_id from one user must not be updatable by naming a different
    user_id — habits live under users/{user_id}/habits, so this is really
    just confirming that scoping."""
    created = await store.habits.create(user_id="u1", label="Gym", goal="3x/week")

    assert (
        await store.habits.update(user_id="u2", habit_id=created.habit_id, goal="hijacked")
        is None
    )
