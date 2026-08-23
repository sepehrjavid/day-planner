"""Coverage of habit domain logic (moved from day_planner_backend_internal
by A6.1).

No route on this service calls Store.create_habit/list_habits/update_habit
yet — A6.1 moves the data and the code that owns it; A6.2 (agent access)
and A6.3 (user-facing routes) are what actually wire a caller to it. These
tests exercise the `store` fixture's own habit methods directly rather
than through HTTP, which is what "no net loss in assertions" from the
move means until a route exists to test through — the auth-gate coverage
day_planner_backend_internal had for /internal/habits* doesn't have an
app-service equivalent yet, since there is no route here to gate.
"""


async def test_create_habit_returns_a_stable_id(store):
    habit = await store.create_habit(user_id="u1", label="Gym", goal="180 min/week")
    assert habit.label == "Gym"
    assert habit.status == "active"
    assert habit.habit_id


async def test_create_habit_defaults_allowed_zones_to_empty(store):
    habit = await store.create_habit(user_id="u1", label="Gym", goal="3x/week")
    assert habit.allowed_zones == []


async def test_list_habits_empty_by_default(store):
    assert await store.list_habits("u1") == []


async def test_list_habits_returns_created_and_scopes_by_user(store):
    await store.create_habit(user_id="u1", label="Gym", goal="3x/week")
    await store.create_habit(user_id="u1", label="Reading", goal="nightly")
    # A different user's habits must never show up in this list.
    await store.create_habit(user_id="u2", label="Meditation", goal="daily")

    habits = await store.list_habits("u1")
    assert {h.label for h in habits} == {"Gym", "Reading"}


async def test_list_habits_filters_by_status(store):
    created = await store.create_habit(user_id="u1", label="Gym", goal="3x/week")
    await store.create_habit(user_id="u1", label="Reading", goal="nightly")
    await store.update_habit(user_id="u1", habit_id=created.habit_id, status="paused")

    active = await store.list_habits("u1", status="active")
    paused = await store.list_habits("u1", status="paused")
    assert [h.label for h in active] == ["Reading"]
    assert [h.label for h in paused] == ["Gym"]


async def test_update_habit_changes_fields(store):
    created = await store.create_habit(user_id="u1", label="Gym", goal="3x/week")

    updated = await store.update_habit(
        user_id="u1", habit_id=created.habit_id, goal="5x/week now", status="active"
    )
    assert updated.goal == "5x/week now"
    assert updated.label == "Gym"  # untouched field survives a partial update


async def test_update_habit_unknown_returns_none(store):
    assert await store.update_habit(user_id="u1", habit_id="ghost", goal="x") is None


async def test_update_habit_sets_allowed_zones(store):
    created = await store.create_habit(user_id="u1", label="Gym", goal="3x/week")

    updated = await store.update_habit(
        user_id="u1", habit_id=created.habit_id, allowed_zones=["Work"]
    )
    assert updated.allowed_zones == ["Work"]


async def test_update_habit_wrong_user_returns_none(store):
    """A habit_id from one user must not be updatable by naming a different
    user_id — habits live under users/{user_id}/habits, so this is really
    just confirming that scoping."""
    created = await store.create_habit(user_id="u1", label="Gym", goal="3x/week")

    assert (
        await store.update_habit(user_id="u2", habit_id=created.habit_id, goal="hijacked")
        is None
    )
