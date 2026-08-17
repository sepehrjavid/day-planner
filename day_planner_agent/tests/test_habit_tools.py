"""Coverage for habit_tools.py's orchestration: that user_id always comes
from tool_context (never a model-supplied argument), and that each tool
maps backend_client's response shapes — including "not found" on update —
into the right status.

backend_client's own HTTP mechanics aren't re-tested here —
day_planner_backend_internal's own test suite already covers
/internal/habits* directly.
"""

from day_planner_agent import backend_client, calendar_tool, habit_tools


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
        {"label": None, "goal": None, "status": "archived", "allowed_zones": None},
    )


async def test_update_habit_sets_allowed_zones(tool_context, monkeypatch):
    seen = {}

    async def update_habit(user_id, habit_id, **kwargs):
        seen["args"] = (user_id, habit_id, kwargs)
        return {"habit_id": habit_id, "allowed_zones": kwargs.get("allowed_zones")}

    monkeypatch.setattr(backend_client, "update_habit", update_habit)

    result = await habit_tools.update_habit(tool_context, "h1", allowed_zones=["Work"])
    assert result["habit"]["allowed_zones"] == ["Work"]
    assert seen["args"][2]["allowed_zones"] == ["Work"]


async def test_update_habit_clears_allowed_zones_with_empty_list(tool_context, monkeypatch):
    """allowed_zones=[] is a meaningful "clear it" request, not the same
    as "not provided" — a naive truthiness check on this list would
    wrongly reject the call as having no fields to update."""
    seen = {}

    async def update_habit(user_id, habit_id, **kwargs):
        seen["args"] = (user_id, habit_id, kwargs)
        return {"habit_id": habit_id, "allowed_zones": []}

    monkeypatch.setattr(backend_client, "update_habit", update_habit)

    result = await habit_tools.update_habit(tool_context, "h1", allowed_zones=[])
    assert result["status"] == "success"
    assert seen["args"][2]["allowed_zones"] == []


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


# ---------------------------------------------------------------------------
# review_habit_week
# ---------------------------------------------------------------------------


async def test_review_habit_week_no_sessions_short_circuits(tool_context, monkeypatch):
    """No point calling get_calendar_events at all if nothing was ever
    planned in this window — and the empty-list result must be
    distinguishable from "everything was kept", so callers don't conflate
    the two when summarizing."""

    async def list_habit_sessions(user_id, *, planned_from, planned_to):
        return []

    async def get_calendar_events(*args, **kwargs):
        raise AssertionError("should not be called when there are no sessions")

    monkeypatch.setattr(backend_client, "list_habit_sessions", list_habit_sessions)
    monkeypatch.setattr(calendar_tool, "get_calendar_events", get_calendar_events)

    result = await habit_tools.review_habit_week(tool_context, "2026-08-01", "2026-08-08")
    assert result == {"status": "success", "sessions": []}


async def test_review_habit_week_bubbles_needs_auth(tool_context, monkeypatch):
    async def list_habit_sessions(user_id, *, planned_from, planned_to):
        return [
            {
                "habit_id": "h1",
                "event_id": "e1",
                "calendar_id": "me@gmail.com",
                "planned_start": "2026-08-04T07:00:00-07:00",
                "planned_end": "2026-08-04T07:30:00-07:00",
            }
        ]

    async def get_calendar_events(tool_context, date_from, date_to):
        return {"status": "needs_auth", "connect_url": "https://connect.example/start"}

    monkeypatch.setattr(backend_client, "list_habit_sessions", list_habit_sessions)
    monkeypatch.setattr(calendar_tool, "get_calendar_events", get_calendar_events)

    result = await habit_tools.review_habit_week(tool_context, "2026-08-01", "2026-08-08")
    assert result == {"status": "needs_auth", "connect_url": "https://connect.example/start"}


async def test_review_habit_week_buckets_outcomes_and_names_the_cause(tool_context, monkeypatch):
    sessions = [
        {  # still there, same time -> kept
            "habit_id": "h1",
            "event_id": "e1",
            "calendar_id": "me@gmail.com",
            "planned_start": "2026-08-04T07:00:00-07:00",
            "planned_end": "2026-08-04T07:30:00-07:00",
        },
        {  # still there, different time -> moved, nothing else in its old slot
            "habit_id": "h1",
            "event_id": "e2",
            "calendar_id": "me@gmail.com",
            "planned_start": "2026-08-05T07:00:00-07:00",
            "planned_end": "2026-08-05T07:30:00-07:00",
        },
        {  # deleted -> gone, something else now sits in its old slot
            "habit_id": "h2",
            "event_id": "e3",
            "calendar_id": "me@gmail.com",
            "planned_start": "2026-08-06T07:00:00-07:00",
            "planned_end": "2026-08-06T07:30:00-07:00",
        },
        {  # habit was archived/renamed away since — label falls back to id
            "habit_id": "h-unknown",
            "event_id": "e4",
            "calendar_id": "me@gmail.com",
            "planned_start": "2026-08-07T07:00:00-07:00",
            "planned_end": "2026-08-07T07:30:00-07:00",
        },
    ]

    async def list_habit_sessions(user_id, *, planned_from, planned_to):
        return sessions

    calendar_state = {
        "status": "success",
        "events": [
            {
                "event_id": "e1",
                "calendar_id": "me@gmail.com",
                "title": "Gym",
                "start_time": "2026-08-04T07:00:00-07:00",
                "end_time": "2026-08-04T07:30:00-07:00",
            },
            {
                "event_id": "e2",
                "calendar_id": "me@gmail.com",
                "title": "Gym",
                "start_time": "2026-08-05T18:00:00-07:00",
                "end_time": "2026-08-05T18:30:00-07:00",
            },
            {
                "event_id": "standup",
                "calendar_id": "me@gmail.com",
                "title": "Standup ran long",
                "start_time": "2026-08-06T07:00:00-07:00",
                "end_time": "2026-08-06T08:00:00-07:00",
            },
            # e4 also gone, and nothing occupies its old slot -> bumped_by None
        ],
    }

    async def get_calendar_events(tool_context, date_from, date_to):
        return calendar_state

    async def list_habits(user_id, *, status=None):
        assert status is None  # review must see every habit, not just active ones
        return [{"habit_id": "h1", "label": "Gym"}, {"habit_id": "h2", "label": "Tennis"}]

    monkeypatch.setattr(backend_client, "list_habit_sessions", list_habit_sessions)
    monkeypatch.setattr(calendar_tool, "get_calendar_events", get_calendar_events)
    monkeypatch.setattr(backend_client, "list_habits", list_habits)

    result = await habit_tools.review_habit_week(tool_context, "2026-08-01", "2026-08-08")
    assert result["status"] == "success"
    by_event = {s["event_id"]: s for s in result["sessions"]}

    assert by_event["e1"]["outcome"] == "kept"
    assert "bumped_by" not in by_event["e1"]
    assert by_event["e1"]["habit_label"] == "Gym"

    assert by_event["e2"]["outcome"] == "moved"
    assert by_event["e2"]["bumped_by"] is None

    assert by_event["e3"]["outcome"] == "gone"
    assert by_event["e3"]["bumped_by"] == "Standup ran long"
    assert by_event["e3"]["habit_label"] == "Tennis"

    assert by_event["e4"]["outcome"] == "gone"
    assert by_event["e4"]["bumped_by"] is None
    assert by_event["e4"]["habit_label"] == "h-unknown"


async def test_review_habit_week_surfaces_session_status(tool_context, monkeypatch):
    """The whole point of A1.5: "moved" + "completed" must be reportable
    as a success, not a partial failure the calendar diff alone would
    suggest. A session with no status field at all (pre-A1.5 data, or a
    backend that hasn't rolled the field out) must read as "pending", not
    crash or silently misreport."""
    sessions = [
        {
            "habit_id": "h1",
            "event_id": "e-moved-completed",
            "calendar_id": "me@gmail.com",
            "planned_start": "2026-08-04T07:00:00-07:00",
            "planned_end": "2026-08-04T07:30:00-07:00",
            "status": "completed",
            "completed_at": "2026-08-04T09:00:00Z",
            "marked_by": "user",
        },
        {
            "habit_id": "h1",
            "event_id": "e-no-status-field",
            "calendar_id": "me@gmail.com",
            "planned_start": "2026-08-05T07:00:00-07:00",
            "planned_end": "2026-08-05T07:30:00-07:00",
        },
    ]

    async def list_habit_sessions(user_id, *, planned_from, planned_to):
        return sessions

    async def get_calendar_events(tool_context, date_from, date_to):
        return {
            "status": "success",
            "events": [
                {
                    "event_id": "e-moved-completed",
                    "calendar_id": "me@gmail.com",
                    "title": "Gym",
                    "start_time": "2026-08-04T18:00:00-07:00",
                    "end_time": "2026-08-04T18:30:00-07:00",
                },
                {
                    "event_id": "e-no-status-field",
                    "calendar_id": "me@gmail.com",
                    "title": "Gym",
                    "start_time": "2026-08-05T07:00:00-07:00",
                    "end_time": "2026-08-05T07:30:00-07:00",
                },
            ],
        }

    async def list_habits(user_id, *, status=None):
        return [{"habit_id": "h1", "label": "Gym"}]

    monkeypatch.setattr(backend_client, "list_habit_sessions", list_habit_sessions)
    monkeypatch.setattr(calendar_tool, "get_calendar_events", get_calendar_events)
    monkeypatch.setattr(backend_client, "list_habits", list_habits)

    result = await habit_tools.review_habit_week(tool_context, "2026-08-01", "2026-08-08")
    by_event = {s["event_id"]: s for s in result["sessions"]}

    moved_completed = by_event["e-moved-completed"]
    assert moved_completed["outcome"] == "moved"
    assert moved_completed["session_status"] == "completed"
    assert moved_completed["completed_at"] == "2026-08-04T09:00:00Z"
    assert moved_completed["marked_by"] == "user"

    no_status = by_event["e-no-status-field"]
    assert no_status["session_status"] == "pending"
    assert no_status["completed_at"] is None
    assert no_status["marked_by"] is None


# ---------------------------------------------------------------------------
# mark_habit_session
# ---------------------------------------------------------------------------


async def test_mark_habit_session_passes_through_and_hardcodes_marked_by(
    tool_context, monkeypatch
):
    seen = {}

    async def set_habit_session_status(user_id, *, calendar_id, event_id, status, marked_by):
        seen["args"] = (user_id, calendar_id, event_id, status, marked_by)
        return {
            "habit_id": "h1",
            "event_id": event_id,
            "calendar_id": calendar_id,
            "status": status,
            "completed_at": "2026-08-04T09:00:00Z",
            "marked_by": marked_by,
        }

    monkeypatch.setattr(backend_client, "set_habit_session_status", set_habit_session_status)

    result = await habit_tools.mark_habit_session(
        tool_context, "me@gmail.com", "e1", "completed"
    )

    assert result["status"] == "success"
    assert result["session"]["session_status"] == "completed"
    # marked_by="agent" is hardcoded by the tool itself — not something a
    # model argument could override, since mark_habit_session's own
    # signature has no marked_by parameter at all.
    assert seen["args"] == ("user-1", "me@gmail.com", "e1", "completed", "agent")


async def test_mark_habit_session_not_found(tool_context, monkeypatch):
    async def set_habit_session_status(user_id, *, calendar_id, event_id, status, marked_by):
        return None

    monkeypatch.setattr(backend_client, "set_habit_session_status", set_habit_session_status)

    result = await habit_tools.mark_habit_session(
        tool_context, "me@gmail.com", "never-planned", "completed"
    )
    assert result["status"] == "not_found"


async def test_mark_habit_session_user_id_comes_only_from_tool_context(tool_context, monkeypatch):
    assert "user_id" not in habit_tools.mark_habit_session.__code__.co_varnames[
        : habit_tools.mark_habit_session.__code__.co_argcount
    ]
