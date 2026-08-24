"""Coverage of scheduling_tool.py — get_available_slots and the shadow-mode
comparison hook (A4.2), and find_zone_collisions (A4.3).

calendar_tool's own plumbing (Google Calendar API, timezone resolution)
isn't re-tested here — test_calendar_tool.py already covers
resolve_reference_timezone and get_calendar_events directly. What's
tested here is scheduling_tool.py's own integration: adapting domain
dicts into scheduling's dataclasses, zone-anchored detection, candidate
slicing, the shadow comparison's agreement check, and find_zone_collisions'
own habit-tagged/plain-appointment filtering.
"""

from datetime import datetime

from day_planner_agent import calendar_tool, domain_client, habit_tools, scheduling_tool

GYM_HABIT = {
    "habit_id": "h1",
    "label": "Gym",
    "goal": "180 min/week, sessions 30-60 minutes",
    "status": "active",
    "allowed_zones": [],
}

WORK_ZONE = {
    "zone_id": "z1",
    "label": "Work",
    "start_time": "09:00",
    "end_time": "17:00",
    "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
}

SLEEP_SCHEDULE = {
    "sleep_time": "23:00",
    "wake_time": "07:00",
    "cool_down_minutes": 0,
    "wake_up_buffer_minutes": 0,
    "day_overrides": {},
}


def _install(
    monkeypatch,
    *,
    habits=(GYM_HABIT,),
    zones=(),
    sleep_schedule=None,
    events=None,
    habit_sessions=(),
    review_sessions=(),
    tz="America/New_York",
):
    async def list_habits(user_id, status=None):
        return list(habits)

    async def list_zones(user_id):
        return list(zones)

    async def get_sleep_schedule(user_id):
        return dict(sleep_schedule) if sleep_schedule else None

    async def list_habit_sessions(user_id, *, planned_from, planned_to):
        return list(habit_sessions)

    async def resolve_reference_timezone(tool_context, user_id):
        return tz

    async def get_calendar_events(tool_context, date_from, date_to):
        return {"status": "success", "events": list(events or [])}

    async def compute_habit_review(tool_context, date_from, date_to):
        return {"status": "success", "sessions": list(review_sessions)}

    monkeypatch.setattr(domain_client, "list_habits", list_habits)
    monkeypatch.setattr(domain_client, "list_zones", list_zones)
    monkeypatch.setattr(domain_client, "get_sleep_schedule", get_sleep_schedule)
    monkeypatch.setattr(domain_client, "list_habit_sessions", list_habit_sessions)
    monkeypatch.setattr(calendar_tool, "resolve_reference_timezone", resolve_reference_timezone)
    monkeypatch.setattr(calendar_tool, "get_calendar_events", get_calendar_events)
    monkeypatch.setattr(habit_tools, "compute_habit_review", compute_habit_review)


async def test_habit_not_found(tool_context, monkeypatch):
    _install(monkeypatch, habits=())
    result = await scheduling_tool.get_available_slots(
        tool_context, "2026-08-03", "2026-08-04", "ghost"
    )
    assert result["status"] == "not_found"


async def test_needs_auth_when_no_calendar_connected(tool_context, monkeypatch):
    _install(monkeypatch, tz=None)
    result = await scheduling_tool.get_available_slots(
        tool_context, "2026-08-03", "2026-08-04", "h1"
    )
    assert result["status"] == "needs_auth"


async def test_propagates_calendar_events_failure_status(tool_context, monkeypatch):
    _install(monkeypatch)

    async def failing_get_calendar_events(tool_context, date_from, date_to):
        return {"status": "error", "error_message": "boom"}

    monkeypatch.setattr(calendar_tool, "get_calendar_events", failing_get_calendar_events)

    result = await scheduling_tool.get_available_slots(
        tool_context, "2026-08-03", "2026-08-04", "h1"
    )
    assert result == {"status": "error", "error_message": "boom"}


async def test_success_returns_ranked_candidates(tool_context, monkeypatch):
    _install(monkeypatch, zones=[WORK_ZONE], sleep_schedule=SLEEP_SCHEDULE)

    # 2026-08-03 is a Monday.
    result = await scheduling_tool.get_available_slots(
        tool_context, "2026-08-03", "2026-08-04", "h1"
    )

    assert result["status"] == "success"
    assert result["zone_anchored"] is False
    assert result["remaining_target_minutes"] == 180
    assert len(result["candidates"]) > 0
    first = result["candidates"][0]
    assert set(first) == {"start", "end", "score", "reasons", "constraints_applied"}
    assert "Work" in first["constraints_applied"]
    assert "sleep_schedule" in first["constraints_applied"]
    # No candidate may fall inside work hours or the sleep window.
    for c in result["candidates"]:
        start = datetime.fromisoformat(c["start"])
        assert not (9 <= start.hour < 17)
        assert not (23 <= start.hour or start.hour < 7)


async def test_candidates_sorted_best_first(tool_context, monkeypatch):
    _install(monkeypatch)
    result = await scheduling_tool.get_available_slots(
        tool_context, "2026-08-08", "2026-08-10", "h1"  # Sat-Sun
    )
    scores = [c["score"] for c in result["candidates"]]
    assert scores == sorted(scores, reverse=True)


async def test_respects_explicit_min_max_minutes_override(tool_context, monkeypatch):
    _install(monkeypatch)
    result = await scheduling_tool.get_available_slots(
        tool_context, "2026-08-03", "2026-08-04", "h1", min_minutes=90, max_minutes=90
    )
    for c in result["candidates"]:
        start = datetime.fromisoformat(c["start"])
        end = datetime.fromisoformat(c["end"])
        assert (end - start).total_seconds() / 60 == 90


async def test_accounts_for_already_placed_sessions(tool_context, monkeypatch):
    placed = [
        {
            "habit_id": "h1",
            "planned_start": "2026-08-03T06:00:00-04:00",
            "planned_end": "2026-08-03T06:45:00-04:00",
        }
    ]
    _install(monkeypatch, habit_sessions=placed)
    result = await scheduling_tool.get_available_slots(
        tool_context, "2026-08-03", "2026-08-04", "h1"
    )
    assert result["remaining_target_minutes"] == 135  # 180 - 45


async def test_zone_anchored_habit_returns_zone_occurrences(tool_context, monkeypatch):
    commute = {
        "habit_id": "h2",
        "label": "Commute audiobook",
        "goal": "listen to an audiobook, whenever I have commute",
        "status": "active",
        "allowed_zones": ["Commute"],
    }
    commute_zone = {
        "zone_id": "z2",
        "label": "Commute",
        "start_time": "08:30",
        "end_time": "09:00",
        "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
    }
    _install(monkeypatch, habits=[commute], zones=[commute_zone])

    result = await scheduling_tool.get_available_slots(
        tool_context, "2026-08-03", "2026-08-08", "h2"  # Mon-Fri
    )

    assert result["status"] == "success"
    assert result["zone_anchored"] is True
    assert result["remaining_target_minutes"] is None
    assert len(result["candidates"]) == 5  # one per weekday occurrence
    for c in result["candidates"]:
        assert c["score"] is None
        assert c["reasons"] == ["zone-anchored: Commute"]
        assert c["start"].endswith("08:30:00-04:00") or "08:30:00" in c["start"]


async def test_repeat_bump_reason_surfaces_in_response(tool_context, monkeypatch):
    # A sleep schedule waking at 06:00 so the day's first free gap starts
    # exactly there — otherwise, with no other constraint, the only free
    # gap starts at midnight and 06:00 is never a candidate at all (see
    # _slice_candidates: one candidate per gap, anchored at the gap's
    # own start).
    sleep_schedule = {
        "sleep_time": "23:00",
        "wake_time": "06:00",
        "cool_down_minutes": 0,
        "wake_up_buffer_minutes": 0,
        "day_overrides": {},
    }
    # Two prior Mondays at 06:00, both bumped by the same unrelated meeting.
    review_sessions = [
        {
            "habit_id": "h1",
            "planned_start": "2026-07-27T06:00:00-04:00",
            "outcome": "moved",
            "bumped_by": "Standup",
        },
        {
            "habit_id": "h1",
            "planned_start": "2026-08-03T06:00:00-04:00",
            "outcome": "moved",
            "bumped_by": "Standup",
        },
    ]
    _install(monkeypatch, sleep_schedule=sleep_schedule, review_sessions=review_sessions)

    result = await scheduling_tool.get_available_slots(
        tool_context, "2026-08-10", "2026-08-11", "h1", min_minutes=30, max_minutes=30
    )

    # 2026-08-10 is the following Monday — the same slot pattern.
    six_am = next(
        c for c in result["candidates"] if datetime.fromisoformat(c["start"]).hour == 6
    )
    assert "repeatedly bumped by Standup" in six_am["reasons"]


# ---------------------------------------------------------------------------
# log_shadow_comparison
# ---------------------------------------------------------------------------


async def test_shadow_comparison_agrees_with_engine_top_candidate(tool_context, monkeypatch):
    _install(monkeypatch)
    # Find out what the engine's top candidate actually is first, then
    # pretend the model placed exactly that.
    result = await scheduling_tool.get_available_slots(
        tool_context, "2026-08-08", "2026-08-09", "h1"  # Saturday
    )
    top = result["candidates"][0]
    event = {
        "event_id": "e1",
        "calendar_id": "me@gmail.com",
        "start_time": top["start"],
        "end_time": top["end"],
    }

    await scheduling_tool.log_shadow_comparison(tool_context, "user-1", "h1", event)

    comparisons = tool_context.state[scheduling_tool.SHADOW_COMPARISONS_STATE_KEY]
    assert len(comparisons) == 1
    assert comparisons[0]["agrees_with_top_candidate"] is True
    assert comparisons[0]["habit_id"] == "h1"


async def test_shadow_comparison_disagrees_with_a_different_placement(tool_context, monkeypatch):
    _install(monkeypatch, zones=[WORK_ZONE], sleep_schedule=SLEEP_SCHEDULE)
    event = {
        "event_id": "e1",
        "calendar_id": "me@gmail.com",
        # Deliberately inside the work zone — a placement the engine
        # would never itself suggest, so it can never be the top
        # candidate (or any candidate at all).
        "start_time": "2026-08-03T12:00:00-04:00",
        "end_time": "2026-08-03T12:30:00-04:00",
    }

    await scheduling_tool.log_shadow_comparison(tool_context, "user-1", "h1", event)

    comparisons = tool_context.state[scheduling_tool.SHADOW_COMPARISONS_STATE_KEY]
    assert comparisons[0]["agrees_with_top_candidate"] is False


async def test_shadow_comparison_skips_all_day_events(tool_context, monkeypatch):
    _install(monkeypatch)
    event = {"event_id": "e1", "calendar_id": "me@gmail.com", "start_time": "2026-08-03", "end_time": "2026-08-04"}

    await scheduling_tool.log_shadow_comparison(tool_context, "user-1", "h1", event)

    assert scheduling_tool.SHADOW_COMPARISONS_STATE_KEY not in tool_context.state


async def test_shadow_comparison_accumulates_across_calls(tool_context, monkeypatch):
    _install(monkeypatch)
    event = {
        "event_id": "e1",
        "calendar_id": "me@gmail.com",
        "start_time": "2026-08-08T10:00:00-04:00",
        "end_time": "2026-08-08T10:30:00-04:00",
    }
    await scheduling_tool.log_shadow_comparison(tool_context, "user-1", "h1", event)
    await scheduling_tool.log_shadow_comparison(tool_context, "user-1", "h1", event)

    assert len(tool_context.state[scheduling_tool.SHADOW_COMPARISONS_STATE_KEY]) == 2



# ---------------------------------------------------------------------------
# find_zone_collisions
# ---------------------------------------------------------------------------


async def test_find_zone_collisions_zone_not_found(tool_context, monkeypatch):
    _install(monkeypatch, zones=[WORK_ZONE])
    result = await scheduling_tool.find_zone_collisions(
        tool_context, "Ghost Zone", "2026-08-03", "2026-08-10"
    )
    assert result["status"] == "not_found"


async def test_find_zone_collisions_needs_auth_when_no_calendar_connected(
    tool_context, monkeypatch
):
    _install(monkeypatch, zones=[WORK_ZONE], tz=None)
    result = await scheduling_tool.find_zone_collisions(
        tool_context, "Work", "2026-08-03", "2026-08-10"
    )
    assert result["status"] == "needs_auth"


async def test_find_zone_collisions_propagates_calendar_events_failure_status(
    tool_context, monkeypatch
):
    _install(monkeypatch, zones=[WORK_ZONE])

    async def failing_get_calendar_events(tool_context, date_from, date_to):
        return {"status": "error", "error_message": "boom"}

    monkeypatch.setattr(calendar_tool, "get_calendar_events", failing_get_calendar_events)

    result = await scheduling_tool.find_zone_collisions(
        tool_context, "Work", "2026-08-03", "2026-08-10"
    )
    assert result == {"status": "error", "error_message": "boom"}


async def test_find_zone_collisions_finds_a_habit_tagged_session_inside_the_zone(
    tool_context, monkeypatch
):
    # 2026-08-03 is a Monday, inside WORK_ZONE's 09:00-17:00.
    colliding_event = {
        "event_id": "e1",
        "calendar_id": "me@gmail.com",
        "title": "Gym",
        "habit_id": "h1",
        "start_time": "2026-08-03T10:00:00-04:00",
        "end_time": "2026-08-03T10:45:00-04:00",
    }
    _install(monkeypatch, zones=[WORK_ZONE], events=[colliding_event])

    result = await scheduling_tool.find_zone_collisions(
        tool_context, "Work", "2026-08-03", "2026-08-04"
    )

    assert result["status"] == "success"
    assert result["colliding_sessions"] == [colliding_event]


async def test_find_zone_collisions_ignores_plain_appointments(tool_context, monkeypatch):
    # Same window as the zone, but no habit_id — not what this check is for.
    plain_event = {
        "event_id": "e1",
        "calendar_id": "me@gmail.com",
        "title": "Dentist",
        "start_time": "2026-08-03T10:00:00-04:00",
        "end_time": "2026-08-03T10:45:00-04:00",
    }
    _install(monkeypatch, zones=[WORK_ZONE], events=[plain_event])

    result = await scheduling_tool.find_zone_collisions(
        tool_context, "Work", "2026-08-03", "2026-08-04"
    )

    assert result["status"] == "success"
    assert result["colliding_sessions"] == []


async def test_find_zone_collisions_ignores_sessions_outside_the_zone_window(
    tool_context, monkeypatch
):
    # Before WORK_ZONE opens (09:00) — habit-tagged, but no collision.
    early_event = {
        "event_id": "e1",
        "calendar_id": "me@gmail.com",
        "title": "Gym",
        "habit_id": "h1",
        "start_time": "2026-08-03T07:00:00-04:00",
        "end_time": "2026-08-03T07:45:00-04:00",
    }
    _install(monkeypatch, zones=[WORK_ZONE], events=[early_event])

    result = await scheduling_tool.find_zone_collisions(
        tool_context, "Work", "2026-08-03", "2026-08-04"
    )

    assert result["status"] == "success"
    assert result["colliding_sessions"] == []


async def test_shadow_comparison_never_raises_on_backend_failure(tool_context, monkeypatch):
    async def failing_list_habits(user_id, status=None):
        raise domain_client.BACKEND_ERROR[0]("boom")

    monkeypatch.setattr(domain_client, "list_habits", failing_list_habits)
    event = {
        "event_id": "e1",
        "calendar_id": "me@gmail.com",
        "start_time": "2026-08-08T10:00:00-04:00",
        "end_time": "2026-08-08T10:30:00-04:00",
    }

    # Must not raise.
    await scheduling_tool.log_shadow_comparison(tool_context, "user-1", "h1", event)

    assert scheduling_tool.SHADOW_COMPARISONS_STATE_KEY not in tool_context.state
