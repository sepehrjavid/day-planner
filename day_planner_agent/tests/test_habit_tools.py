"""Coverage for habit_tools.py's orchestration: that user_id always comes
from tool_context (never a model-supplied argument), and that each tool
maps backend_client's response shapes — including "not found" on update —
into the right status.

backend_client's own HTTP mechanics aren't re-tested here —
day_planner_backend_internal's own test suite already covers
/internal/habits* directly.
"""

from day_planner_agent import backend_client, habit_tools


async def test_create_habit_passes_through(tool_context, monkeypatch):
    seen = {}

    async def create_habit(user_id, *, label, goal):
        seen["args"] = (user_id, label, goal)
        return {
            "habit_id": "h1",
            "label": label,
            "goal": goal,
            "status": "active",
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z",
        }

    monkeypatch.setattr(backend_client, "create_habit", create_habit)

    result = await habit_tools.create_habit(tool_context, "Gym", "180 min/week")
    assert result["status"] == "success"
    assert result["habit"]["habit_id"] == "h1"
    assert seen["args"] == ("user-1", "Gym", "180 min/week")


async def test_list_habits_defaults_to_active_only(tool_context, monkeypatch):
    seen = {}

    async def list_habits(user_id, *, status=None):
        seen["status"] = status
        return []

    monkeypatch.setattr(backend_client, "list_habits", list_habits)

    result = await habit_tools.list_habits(tool_context)
    assert result == {"status": "success", "habits": []}
    assert seen["status"] == "active"


async def test_list_habits_include_inactive_lifts_the_filter(tool_context, monkeypatch):
    seen = {}

    async def list_habits(user_id, *, status=None):
        seen["status"] = status
        return [{"habit_id": "h1", "status": "paused"}]

    monkeypatch.setattr(backend_client, "list_habits", list_habits)

    result = await habit_tools.list_habits(tool_context, include_inactive=True)
    assert result["habits"] == [{"habit_id": "h1", "status": "paused"}]
    assert seen["status"] is None


async def test_update_habit_no_fields_is_an_error(tool_context):
    result = await habit_tools.update_habit(tool_context, "h1")
    assert result["status"] == "error"


async def test_update_habit_not_found(tool_context, monkeypatch):
    async def update_habit(user_id, habit_id, **kwargs):
        return None

    monkeypatch.setattr(backend_client, "update_habit", update_habit)

    result = await habit_tools.update_habit(tool_context, "ghost", status="paused")
    assert result["status"] == "not_found"


async def test_update_habit_success(tool_context, monkeypatch):
    seen = {}

    async def update_habit(user_id, habit_id, **kwargs):
        seen["args"] = (user_id, habit_id, kwargs)
        return {"habit_id": habit_id, "status": kwargs.get("status")}

    monkeypatch.setattr(backend_client, "update_habit", update_habit)

    result = await habit_tools.update_habit(tool_context, "h1", status="archived")
    assert result == {
        "status": "success",
        "habit": {"habit_id": "h1", "status": "archived"},
    }
    assert seen["args"] == (
        "user-1",
        "h1",
        {"label": None, "goal": None, "status": "archived"},
    )


async def test_user_id_comes_only_from_tool_context(tool_context, monkeypatch):
    """The whole tenant boundary: none of the habit tools take user_id as a
    parameter a model could fill in — confirm each call into backend_client
    is keyed on tool_context.session.user_id instead."""
    seen_user_ids = []

    async def list_habits(user_id, *, status=None):
        seen_user_ids.append(user_id)
        return []

    monkeypatch.setattr(backend_client, "list_habits", list_habits)

    await habit_tools.list_habits(tool_context)
    assert seen_user_ids == ["user-1"]

    for fn in (habit_tools.create_habit, habit_tools.list_habits, habit_tools.update_habit):
        assert "user_id" not in fn.__code__.co_varnames[: fn.__code__.co_argcount]
