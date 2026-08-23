"""Coverage of /agent/* (A6.2) — the agent runtime's own path to habits,
habit sessions, zones, and the sleep schedule.

Route logic is tested via the `agent_client` fixture (dependency
override, same pattern day_planner_backend_internal's own test suite
used for require_internal_caller) — accounts are seeded directly into
`store`, the same as that suite's own approach, since there's no live
connect flow to drive here either.

The auth gate itself (require_agent_caller, unmodified) is tested
separately, via `anon_client`, in the last section — including both
directions A6.2's own acceptance criterion calls for: a session token
rejected on /agent/*, and (the mirror image) a non-session token
rejected on /me/*. The four "does this route group require the caller
gate at all" checks below mirror the ones
day_planner_backend_internal's test suite had for /internal/habits*,
/internal/habit-sessions*, /internal/zones*, and
/internal/sleep-schedule* before A6.1 moved that data here — dropped
coverage a review flagged as needing a real replacement, not just the
new /me <-> /agent separation tests.
"""

# ---------------------------------------------------------------------------
# Auth gate — does this route group require the caller gate at all
# ---------------------------------------------------------------------------


def test_habits_require_agent_caller(anon_client):
    assert (
        anon_client.post(
            "/agent/habits", json={"user_id": "u1", "label": "Gym", "goal": "3x/week"}
        ).status_code
        == 401
    )
    assert anon_client.get("/agent/habits?user_id=u1").status_code == 401


def test_habit_sessions_require_agent_caller(anon_client):
    assert (
        anon_client.post(
            "/agent/habit-sessions",
            json={
                "user_id": "u1",
                "habit_id": "h1",
                "event_id": "e1",
                "calendar_id": "me@gmail.com",
                "planned_start": "2026-08-04T07:00:00Z",
                "planned_end": "2026-08-04T07:30:00Z",
            },
        ).status_code
        == 401
    )
    assert (
        anon_client.get(
            "/agent/habit-sessions"
            "?user_id=u1&planned_from=2026-08-01T00:00:00Z&planned_to=2026-08-08T00:00:00Z"
        ).status_code
        == 401
    )


def test_zones_require_agent_caller(anon_client):
    assert (
        anon_client.post(
            "/agent/zones",
            json={
                "user_id": "u1",
                "label": "Work",
                "start_time": "09:00",
                "end_time": "17:00",
                "days_of_week": ["mon"],
            },
        ).status_code
        == 401
    )
    assert anon_client.get("/agent/zones?user_id=u1").status_code == 401


def test_sleep_schedule_requires_agent_caller(anon_client):
    assert anon_client.post("/agent/sleep-schedule", json={"user_id": "u1"}).status_code == 401
    assert anon_client.get("/agent/sleep-schedule?user_id=u1").status_code == 401


# ---------------------------------------------------------------------------
# Habits
# ---------------------------------------------------------------------------


def test_create_habit_returns_a_stable_id(agent_client):
    response = agent_client.post(
        "/agent/habits",
        json={"user_id": "u1", "label": "Gym", "goal": "180 min/week, sessions 30-60 min"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["label"] == "Gym"
    assert body["status"] == "active"
    assert body["habit_id"]


def test_list_habits_empty_by_default(agent_client):
    body = agent_client.get("/agent/habits?user_id=u1").json()
    assert body == {"habits": []}


def test_list_habits_returns_created(agent_client):
    agent_client.post("/agent/habits", json={"user_id": "u1", "label": "Gym", "goal": "3x/week"})
    agent_client.post(
        "/agent/habits", json={"user_id": "u1", "label": "Reading", "goal": "nightly"}
    )
    # A different user's habits must never show up in this list.
    agent_client.post(
        "/agent/habits", json={"user_id": "u2", "label": "Meditation", "goal": "daily"}
    )

    habits = agent_client.get("/agent/habits?user_id=u1").json()["habits"]
    assert {h["label"] for h in habits} == {"Gym", "Reading"}


def test_list_habits_filters_by_status(agent_client):
    created = agent_client.post(
        "/agent/habits", json={"user_id": "u1", "label": "Gym", "goal": "3x/week"}
    ).json()
    agent_client.post(
        "/agent/habits", json={"user_id": "u1", "label": "Reading", "goal": "nightly"}
    )
    agent_client.post(
        "/agent/habits/update",
        json={"user_id": "u1", "habit_id": created["habit_id"], "status": "paused"},
    )

    active = agent_client.get("/agent/habits?user_id=u1&status=active").json()["habits"]
    paused = agent_client.get("/agent/habits?user_id=u1&status=paused").json()["habits"]
    assert [h["label"] for h in active] == ["Reading"]
    assert [h["label"] for h in paused] == ["Gym"]


def test_update_habit_changes_fields(agent_client):
    created = agent_client.post(
        "/agent/habits", json={"user_id": "u1", "label": "Gym", "goal": "3x/week"}
    ).json()

    response = agent_client.post(
        "/agent/habits/update",
        json={
            "user_id": "u1",
            "habit_id": created["habit_id"],
            "goal": "5x/week now",
            "status": "active",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["goal"] == "5x/week now"
    assert body["label"] == "Gym"  # untouched field survives a partial update


def test_update_habit_unknown_is_404(agent_client):
    response = agent_client.post(
        "/agent/habits/update", json={"user_id": "u1", "habit_id": "ghost", "goal": "x"}
    )
    assert response.status_code == 404


def test_create_habit_defaults_allowed_zones_to_empty(agent_client):
    response = agent_client.post(
        "/agent/habits", json={"user_id": "u1", "label": "Gym", "goal": "3x/week"}
    )
    assert response.json()["allowed_zones"] == []


def test_update_habit_sets_allowed_zones(agent_client):
    created = agent_client.post(
        "/agent/habits", json={"user_id": "u1", "label": "Gym", "goal": "3x/week"}
    ).json()

    response = agent_client.post(
        "/agent/habits/update",
        json={"user_id": "u1", "habit_id": created["habit_id"], "allowed_zones": ["Work"]},
    )
    assert response.status_code == 200
    assert response.json()["allowed_zones"] == ["Work"]


def test_update_habit_wrong_user_is_404(agent_client):
    """A habit_id from one user must not be updatable by naming a different
    user_id — habits live under users/{user_id}/habits, so this is really
    just confirming that scoping."""
    created = agent_client.post(
        "/agent/habits", json={"user_id": "u1", "label": "Gym", "goal": "3x/week"}
    ).json()

    response = agent_client.post(
        "/agent/habits/update",
        json={"user_id": "u2", "habit_id": created["habit_id"], "goal": "hijacked"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Habit sessions
# ---------------------------------------------------------------------------


def _upsert_session(agent_client, **overrides):
    body = {
        "user_id": "u1",
        "habit_id": "h1",
        "event_id": "e1",
        "calendar_id": "me@gmail.com",
        "planned_start": "2026-08-04T07:00:00-07:00",
        "planned_end": "2026-08-04T07:30:00-07:00",
    }
    body.update(overrides)
    return agent_client.post("/agent/habit-sessions", json=body)


def test_upsert_habit_session_creates(agent_client):
    response = _upsert_session(agent_client)
    assert response.status_code == 200
    body = response.json()
    assert body["habit_id"] == "h1"
    assert body["event_id"] == "e1"
    assert body["session_id"]


def test_upsert_habit_session_same_event_updates_in_place(agent_client):
    """A reschedule (update_calendar_event moving a tagged event) must
    update the existing plan, not create a second record for the same
    event — otherwise review_habit_week would see two conflicting plans
    for one actual event."""
    first = _upsert_session(agent_client).json()

    second = _upsert_session(
        agent_client,
        planned_start="2026-08-04T18:00:00-07:00",
        planned_end="2026-08-04T18:30:00-07:00",
    ).json()

    assert second["session_id"] == first["session_id"]
    assert second["planned_start"] == "2026-08-04T18:00:00-07:00"
    assert second["created_at"] == first["created_at"]  # preserved, not reset


def test_list_habit_sessions_filters_by_planned_start_range(agent_client):
    _upsert_session(agent_client, event_id="e-in-range", planned_start="2026-08-04T07:00:00-07:00")
    _upsert_session(agent_client, event_id="e-before", planned_start="2026-07-20T07:00:00-07:00")
    _upsert_session(agent_client, event_id="e-after", planned_start="2026-09-01T07:00:00-07:00")
    # A different user's session must never show up in this list.
    _upsert_session(agent_client, user_id="u2", event_id="e-other-user")

    body = agent_client.get(
        "/agent/habit-sessions"
        "?user_id=u1&planned_from=2026-08-01T00:00:00Z&planned_to=2026-08-08T00:00:00Z"
    ).json()
    assert [s["event_id"] for s in body["sessions"]] == ["e-in-range"]


def test_list_habit_sessions_empty_by_default(agent_client):
    body = agent_client.get(
        "/agent/habit-sessions"
        "?user_id=u1&planned_from=2026-08-01T00:00:00Z&planned_to=2026-08-08T00:00:00Z"
    ).json()
    assert body == {"sessions": []}


def test_upsert_habit_session_defaults_to_pending_status(agent_client):
    """Three states, never two: a freshly-planned session is pending
    (unknown), not implicitly anything else — see A1.5."""
    body = _upsert_session(agent_client).json()
    assert body["status"] == "pending"
    assert body["completed_at"] is None
    assert body["marked_by"] is None


def _set_status(agent_client, **overrides):
    body = {
        "user_id": "u1",
        "calendar_id": "me@gmail.com",
        "event_id": "e1",
        "status": "completed",
        "marked_by": "agent",
    }
    body.update(overrides)
    return agent_client.post("/agent/habit-sessions/status", json=body)


def test_set_habit_session_status_marks_completed(agent_client):
    _upsert_session(agent_client)
    response = _set_status(agent_client, status="completed", marked_by="agent")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["marked_by"] == "agent"
    assert body["completed_at"] is not None


def test_set_habit_session_status_marks_skipped_with_no_completed_at(agent_client):
    _upsert_session(agent_client)
    body = _set_status(agent_client, status="skipped", marked_by="agent").json()
    assert body["status"] == "skipped"
    assert body["marked_by"] == "agent"
    assert body["completed_at"] is None


def test_set_habit_session_status_not_found_is_404(agent_client):
    response = _set_status(agent_client, event_id="never-planned")
    assert response.status_code == 404


def test_set_habit_session_status_not_found_never_creates_a_session(agent_client):
    """A status call must never be a side door that creates a session
    without a plan — upsert_habit_session (add_calendar_event/
    update_calendar_event tagging a habit session) is the only writer of
    the plan fields."""
    _set_status(agent_client, event_id="never-planned")
    body = agent_client.get(
        "/agent/habit-sessions"
        "?user_id=u1&planned_from=2026-01-01T00:00:00Z&planned_to=2027-01-01T00:00:00Z"
    ).json()
    assert body == {"sessions": []}


def test_set_habit_session_status_is_idempotent(agent_client):
    """Marking complete twice must not keep bumping completed_at forward —
    a genuine no-op on the second call, not just the same status value."""
    _upsert_session(agent_client)
    first = _set_status(agent_client, status="completed").json()

    second = _set_status(agent_client, status="completed").json()

    assert second["completed_at"] == first["completed_at"]
    assert second["updated_at"] == first["updated_at"]


def test_set_habit_session_status_transition_updates_completed_at(agent_client):
    _upsert_session(agent_client)
    _set_status(agent_client, status="skipped")

    completed = _set_status(agent_client, status="completed").json()
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None


def test_set_habit_session_status_can_reset_to_pending(agent_client):
    """Resetting to pending (undoing a mis-mark, e.g. "actually I didn't
    go") is an explicit mark like any other, not something the API
    forbids — see A1.5 follow-up. Must also clear completed_at, the same
    as a transition to skipped."""
    _upsert_session(agent_client)
    _set_status(agent_client, status="completed", marked_by="agent")

    reset = _set_status(agent_client, status="pending", marked_by="agent").json()
    assert reset["status"] == "pending"
    assert reset["completed_at"] is None


def test_marked_by_user_is_also_accepted(agent_client):
    """The agent route accepts marked_by="user" too — day_planner_backend_app's
    own /me/habit-sessions/status route writes to the same underlying
    Store method with "user" hardcoded; /agent/* isn't the only caller of
    this mechanism, just the only one that can name either value."""
    _upsert_session(agent_client)
    body = _set_status(agent_client, status="completed", marked_by="user").json()
    assert body["marked_by"] == "user"


def test_completion_survives_a_reschedule(agent_client):
    """The reschedule-survival invariant (A1.5): upsert_habit_session runs
    again every time update_calendar_event patches a habit-tagged event in
    place — same (calendar_id, event_id), so the same document — and must
    never reset a completion that was already recorded on it."""
    _upsert_session(agent_client)
    completed = _set_status(agent_client, status="completed", marked_by="agent").json()

    rescheduled = _upsert_session(
        agent_client,
        planned_start="2026-08-05T18:00:00-07:00",
        planned_end="2026-08-05T18:30:00-07:00",
    ).json()

    assert rescheduled["session_id"] == completed["session_id"]
    assert rescheduled["planned_start"] == "2026-08-05T18:00:00-07:00"
    assert rescheduled["status"] == "completed"
    assert rescheduled["marked_by"] == "agent"
    assert rescheduled["completed_at"] == completed["completed_at"]


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------


def test_create_zone_returns_a_stable_id(agent_client):
    response = agent_client.post(
        "/agent/zones",
        json={
            "user_id": "u1",
            "label": "Work",
            "start_time": "09:00",
            "end_time": "17:00",
            "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["label"] == "Work"
    assert body["days_of_week"] == ["mon", "tue", "wed", "thu", "fri"]
    assert body["zone_id"]


def test_create_zone_rejects_malformed_time(agent_client):
    response = agent_client.post(
        "/agent/zones",
        json={
            "user_id": "u1",
            "label": "Work",
            "start_time": "9am",
            "end_time": "17:00",
            "days_of_week": ["mon"],
        },
    )
    assert response.status_code == 422


def test_list_zones_empty_by_default(agent_client):
    assert agent_client.get("/agent/zones?user_id=u1").json() == {"zones": []}


def test_list_zones_returns_created(agent_client):
    agent_client.post(
        "/agent/zones",
        json={
            "user_id": "u1",
            "label": "Work",
            "start_time": "09:00",
            "end_time": "17:00",
            "days_of_week": ["mon"],
        },
    )
    # A different user's zones must never show up in this list.
    agent_client.post(
        "/agent/zones",
        json={
            "user_id": "u2",
            "label": "Commute",
            "start_time": "08:00",
            "end_time": "09:00",
            "days_of_week": ["mon"],
        },
    )

    zones = agent_client.get("/agent/zones?user_id=u1").json()["zones"]
    assert {z["label"] for z in zones} == {"Work"}


def test_update_zone_changes_fields(agent_client):
    created = agent_client.post(
        "/agent/zones",
        json={
            "user_id": "u1",
            "label": "Work",
            "start_time": "09:00",
            "end_time": "17:00",
            "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
        },
    ).json()

    response = agent_client.post(
        "/agent/zones/update",
        json={"user_id": "u1", "zone_id": created["zone_id"], "end_time": "18:00"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["end_time"] == "18:00"
    assert body["start_time"] == "09:00"  # untouched field survives a partial update


def test_update_zone_unknown_is_404(agent_client):
    response = agent_client.post(
        "/agent/zones/update", json={"user_id": "u1", "zone_id": "ghost", "end_time": "18:00"}
    )
    assert response.status_code == 404


def test_update_zone_wrong_user_is_404(agent_client):
    created = agent_client.post(
        "/agent/zones",
        json={
            "user_id": "u1",
            "label": "Work",
            "start_time": "09:00",
            "end_time": "17:00",
            "days_of_week": ["mon"],
        },
    ).json()

    response = agent_client.post(
        "/agent/zones/update",
        json={"user_id": "u2", "zone_id": created["zone_id"], "end_time": "18:00"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Sleep schedule
# ---------------------------------------------------------------------------


def test_get_sleep_schedule_reports_not_configured(agent_client):
    body = agent_client.get("/agent/sleep-schedule?user_id=u1").json()
    assert body == {"exists": False, "schedule": None}


def test_set_sleep_schedule_creates(agent_client):
    response = agent_client.post(
        "/agent/sleep-schedule",
        json={
            "user_id": "u1",
            "sleep_time": "23:00",
            "wake_time": "07:00",
            "cool_down_minutes": 30,
            "wake_up_buffer_minutes": 15,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sleep_time"] == "23:00"
    assert body["wake_time"] == "07:00"
    assert body["day_overrides"] == {}


def test_set_sleep_schedule_partial_update_preserves_other_fields(agent_client):
    agent_client.post(
        "/agent/sleep-schedule",
        json={
            "user_id": "u1",
            "sleep_time": "23:00",
            "wake_time": "07:00",
            "cool_down_minutes": 30,
            "wake_up_buffer_minutes": 15,
        },
    )

    response = agent_client.post(
        "/agent/sleep-schedule", json={"user_id": "u1", "cool_down_minutes": 45}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cool_down_minutes"] == 45
    assert body["sleep_time"] == "23:00"  # untouched field survives a partial update


def test_set_sleep_schedule_day_overrides_round_trip(agent_client):
    response = agent_client.post(
        "/agent/sleep-schedule",
        json={
            "user_id": "u1",
            "sleep_time": "23:00",
            "wake_time": "07:00",
            "cool_down_minutes": 30,
            "wake_up_buffer_minutes": 15,
            "day_overrides": {"sun": {"wake_time": "09:00"}},
        },
    )
    assert response.status_code == 200
    assert response.json()["day_overrides"] == {"sun": {"sleep_time": None, "wake_time": "09:00"}}

    fetched = agent_client.get("/agent/sleep-schedule?user_id=u1").json()
    assert fetched["exists"] is True
    assert fetched["schedule"]["day_overrides"] == {
        "sun": {"sleep_time": None, "wake_time": "09:00"}
    }


def test_set_sleep_schedule_day_overrides_replaces_wholesale(agent_client):
    """day_overrides isn't a per-day merge — passing a new map drops
    whatever wasn't included, matching the documented contract on
    AgentSetSleepScheduleRequest.day_overrides."""
    agent_client.post(
        "/agent/sleep-schedule",
        json={
            "user_id": "u1",
            "sleep_time": "23:00",
            "wake_time": "07:00",
            "cool_down_minutes": 30,
            "wake_up_buffer_minutes": 15,
            "day_overrides": {"sun": {"wake_time": "09:00"}, "sat": {"wake_time": "10:00"}},
        },
    )

    response = agent_client.post(
        "/agent/sleep-schedule",
        json={"user_id": "u1", "day_overrides": {"sun": {"wake_time": "09:30"}}},
    )
    assert response.json()["day_overrides"] == {"sun": {"sleep_time": None, "wake_time": "09:30"}}


# ---------------------------------------------------------------------------
# A6.2's own acceptance criterion: /me and /agent must never share a
# dependency, a router, or a user_id derivation. Tested in both
# directions, against the real (unmodified) auth gate on each side —
# neither test uses agent_client's dependency override.
# ---------------------------------------------------------------------------


def test_session_token_is_rejected_on_agent_routes(anon_client, user):
    """A signed-in browser session must never reach /agent/* — that would
    let any logged-in user act as the trusted service identity and name
    any user_id in the body."""
    _, headers = user

    response = anon_client.get("/agent/habits?user_id=u1", headers=headers)

    assert response.status_code == 401


def test_non_session_token_is_rejected_on_me_routes(anon_client):
    """The mirror image: current_user_id resolves a session token via
    store.resolve_session, never anything that merely looks like a
    bearer token — a string shaped like (but not actually) an agent's
    OIDC token has no session behind it and must 401 the same as any
    other garbage credential."""
    response = anon_client.get(
        "/me", headers={"Authorization": "Bearer not-a-real-session-token"}
    )

    assert response.status_code == 401


def test_agent_route_rejects_an_unverifiable_token(anon_client):
    """Same check day_planner_backend_internal's own suite ran against
    require_internal_caller — a token that isn't a real, Google-signed
    JWT must 401, not fall through to some other outcome."""
    response = anon_client.post(
        "/agent/habits",
        json={"user_id": "u1", "label": "Gym", "goal": "3x/week"},
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )

    assert response.status_code == 401
