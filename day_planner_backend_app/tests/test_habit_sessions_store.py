"""Coverage of habit-session domain logic (moved from
day_planner_backend_internal by A6.1) beyond what test_habit_sessions.py's
/me/habit-sessions routes and test_agent_routes.py's /agent/habit-sessions
routes already cover. upsert_habit_session still has no /me or /agent-side
caller test of its own beyond request/response mapping (only the agent
tags calendar events with a habit session; see schemas/habit_sessions.py's
module docstring) — these exercise the `store` fixture's own methods
directly for that reason.

Unlike a route-level test, there's no Pydantic schema between these calls
and the Store — planned_start/planned_end must be real datetime objects,
not ISO strings, since nothing here parses them the way
UpsertHabitSessionRequest would.
"""

from datetime import datetime


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _upsert(store, **overrides):
    body = {
        "user_id": "u1",
        "habit_id": "h1",
        "event_id": "e1",
        "calendar_id": "me@gmail.com",
        "planned_start": _dt("2026-08-04T07:00:00-07:00"),
        "planned_end": _dt("2026-08-04T07:30:00-07:00"),
    }
    body.update(overrides)
    return store.habit_sessions.upsert(**body)


def _set_status(store, **overrides):
    body = {
        "user_id": "u1",
        "calendar_id": "me@gmail.com",
        "event_id": "e1",
        "status": "completed",
        "marked_by": "user",
    }
    body.update(overrides)
    return store.habit_sessions.set_status(**body)


async def test_upsert_habit_session_creates(store):
    session = await _upsert(store)
    assert session.habit_id == "h1"
    assert session.event_id == "e1"
    assert session.session_id


async def test_upsert_habit_session_same_event_updates_in_place(store):
    """A reschedule (update_calendar_event moving a tagged event) must
    update the existing plan, not create a second record for the same
    event — otherwise review_habit_week would see two conflicting plans
    for one actual event."""
    first = await _upsert(store)

    second = await _upsert(
        store,
        planned_start=_dt("2026-08-04T18:00:00-07:00"),
        planned_end=_dt("2026-08-04T18:30:00-07:00"),
    )

    assert second.session_id == first.session_id
    assert second.planned_start == _dt("2026-08-04T18:00:00-07:00")
    assert second.created_at == first.created_at  # preserved, not reset


async def test_list_habit_sessions_filters_by_planned_start_range(store):
    await _upsert(store, event_id="e-in-range", planned_start=_dt("2026-08-04T07:00:00-07:00"))
    await _upsert(store, event_id="e-before", planned_start=_dt("2026-07-20T07:00:00-07:00"))
    await _upsert(store, event_id="e-after", planned_start=_dt("2026-09-01T07:00:00-07:00"))
    # A different user's session must never show up in this list.
    await _upsert(store, user_id="u2", event_id="e-other-user")

    sessions = await store.habit_sessions.list(
        "u1",
        planned_from=_dt("2026-08-01T00:00:00+00:00"),
        planned_to=_dt("2026-08-08T00:00:00+00:00"),
    )
    assert [s.event_id for s in sessions] == ["e-in-range"]


async def test_list_habit_sessions_empty_by_default(store):
    sessions = await store.habit_sessions.list(
        "u1",
        planned_from=_dt("2026-08-01T00:00:00+00:00"),
        planned_to=_dt("2026-08-08T00:00:00+00:00"),
    )
    assert sessions == []


async def test_upsert_habit_session_defaults_to_pending_status(store):
    """Three states, never two: a freshly-planned session is pending
    (unknown), not implicitly anything else — see A1.5."""
    session = await _upsert(store)
    assert session.status == "pending"
    assert session.completed_at is None
    assert session.marked_by is None


async def test_set_habit_session_status_marks_completed(store):
    await _upsert(store)
    session = await _set_status(store, status="completed", marked_by="user")
    assert session.status == "completed"
    assert session.marked_by == "user"
    assert session.completed_at is not None


async def test_set_habit_session_status_marks_skipped_with_no_completed_at(store):
    await _upsert(store)
    session = await _set_status(store, status="skipped", marked_by="agent")
    assert session.status == "skipped"
    assert session.marked_by == "agent"
    assert session.completed_at is None


async def test_set_habit_session_status_not_found_returns_none(store):
    assert await _set_status(store, event_id="never-planned") is None


async def test_set_habit_session_status_not_found_never_creates_a_session(store):
    """A status call must never be a side door that creates a session
    without a plan — upsert_habit_session (add_calendar_event/
    update_calendar_event tagging a habit session) is the only writer of
    the plan fields."""
    await _set_status(store, event_id="never-planned")
    sessions = await store.habit_sessions.list(
        "u1",
        planned_from=_dt("2026-01-01T00:00:00+00:00"),
        planned_to=_dt("2027-01-01T00:00:00+00:00"),
    )
    assert sessions == []


async def test_set_habit_session_status_is_idempotent(store):
    """Marking complete twice must not keep bumping completed_at forward —
    a genuine no-op on the second call, not just the same status value."""
    await _upsert(store)
    first = await _set_status(store, status="completed")

    second = await _set_status(store, status="completed")

    assert second.completed_at == first.completed_at
    assert second.updated_at == first.updated_at


async def test_set_habit_session_status_transition_updates_completed_at(store):
    await _upsert(store)
    await _set_status(store, status="skipped")

    completed = await _set_status(store, status="completed")
    assert completed.status == "completed"
    assert completed.completed_at is not None


async def test_set_habit_session_status_can_reset_to_pending(store):
    """Resetting to pending (undoing a mis-mark, e.g. "actually I didn't
    go") is an explicit mark like any other, not something the API
    forbids — see A1.5 follow-up. Must also clear completed_at, the same
    as a transition to skipped."""
    await _upsert(store)
    await _set_status(store, status="completed", marked_by="user")

    reset = await _set_status(store, status="pending", marked_by="user")
    assert reset.status == "pending"
    assert reset.completed_at is None


async def test_completion_survives_a_reschedule(store):
    """The reschedule-survival invariant (A1.5): upsert_habit_session runs
    again every time update_calendar_event patches a habit-tagged event in
    place — same (calendar_id, event_id), so the same document — and must
    never reset a completion that was already recorded on it."""
    await _upsert(store)
    completed = await _set_status(store, status="completed", marked_by="user")

    # Simulates update_calendar_event rescheduling the same tagged event —
    # patched in place, so calendar_id/event_id (and the derived
    # session_id) are unchanged.
    rescheduled = await _upsert(
        store,
        planned_start=_dt("2026-08-05T18:00:00-07:00"),
        planned_end=_dt("2026-08-05T18:30:00-07:00"),
    )

    assert rescheduled.session_id == completed.session_id
    assert rescheduled.planned_start == _dt("2026-08-05T18:00:00-07:00")
    assert rescheduled.status == "completed"
    assert rescheduled.marked_by == "user"
    assert rescheduled.completed_at == completed.completed_at
