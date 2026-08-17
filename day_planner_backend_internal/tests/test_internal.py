"""Coverage of the agent-facing API.

Accounts are seeded directly into FakeStore (via store.seed_account /
store.seed_user) rather than driven through a live OAuth connect flow — that
code lives in the sibling day_planner_backend_app service and doesn't exist
here. What's tested is this service's own logic: the auth gate, connect-link
minting, multi-account fan-out on /calendars, and the needs_reauth
transition on /access-token — the same failure modes covered for the
combined service before the split, scoped to just this codebase now.
"""

from app.db.models import STATUS_NEEDS_REAUTH, Calendar
from app.providers import NeedsReauth
from app.providers import google as google_provider

# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_healthz_is_open(anon_client):
    assert anon_client.get("/healthz").status_code == 200


def test_rejects_anonymous_callers(anon_client):
    assert (
        anon_client.post("/internal/connect-link", json={"user_id": "u1"}).status_code
        == 401
    )
    assert anon_client.get("/internal/calendars?user_id=u1").status_code == 401


def test_rejects_unverifiable_tokens(anon_client):
    response = anon_client.post(
        "/internal/connect-link",
        json={"user_id": "u1"},
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )
    assert response.status_code == 401


def test_unknown_provider_is_404(client):
    response = client.post(
        "/internal/connect-link", json={"user_id": "u1", "provider": "icloud"}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Connect link
# ---------------------------------------------------------------------------


def test_connect_link_points_at_the_app_service(client):
    """The URL must point at PUBLIC_BASE_URL (the app service's origin, set
    in conftest to http://localhost:8080), never at this service's own
    address — /auth/{provider}/start doesn't exist here."""
    response = client.post("/internal/connect-link", json={"user_id": "u1"})
    assert response.status_code == 200
    url = response.json()["connect_url"]
    assert url.startswith("http://localhost:8080/auth/google/start?s=")


def test_connect_link_does_not_leak_user_id(client):
    url = client.post("/internal/connect-link", json={"user_id": "u1"}).json()[
        "connect_url"
    ]
    assert "u1" not in url


# ---------------------------------------------------------------------------
# /internal/calendars
# ---------------------------------------------------------------------------


def test_calendars_reports_not_connected(client):
    body = client.get("/internal/calendars?user_id=ghost").json()
    assert body == {"connected": False, "needs_reauth": [], "calendars": []}


def test_calendars_spans_every_account(client, store):
    store.seed_account(
        user_id="u1",
        provider_account_id="sub-personal",
        email="me@gmail.com",
        calendars=[Calendar("me@gmail.com", "Me", is_primary=True, selected=True)],
    )
    store.seed_account(
        user_id="u1",
        provider_account_id="sub-work",
        email="me@work.com",
        make_default=False,
        calendars=[Calendar("me@work.com", "Work", is_primary=True, selected=True)],
    )

    body = client.get("/internal/calendars?user_id=u1").json()
    assert body["connected"] is True
    assert {c["calendar_id"] for c in body["calendars"]} == {
        "me@gmail.com",
        "me@work.com",
    }
    assert len({c["account_id"] for c in body["calendars"]}) == 2


def test_calendars_excludes_unselected(client, store):
    store.seed_account(
        user_id="u1",
        calendars=[
            Calendar("me@gmail.com", "Me", is_primary=True, selected=True),
            Calendar(
                "holidays@group.calendar.google.com",
                "Holidays",
                is_primary=False,
                selected=False,
            ),
        ],
    )
    calendars = client.get("/internal/calendars?user_id=u1").json()["calendars"]
    assert [c["calendar_id"] for c in calendars] == ["me@gmail.com"]


def test_calendars_surfaces_broken_accounts(client, store):
    store.seed_account(user_id="u1", status=STATUS_NEEDS_REAUTH)
    body = client.get("/internal/calendars?user_id=u1").json()
    assert body["calendars"] == []
    assert len(body["needs_reauth"]) == 1


# ---------------------------------------------------------------------------
# /internal/access-token
# ---------------------------------------------------------------------------


def test_access_token_defaults_to_the_users_first_account(
    client, store, fake_crypto, monkeypatch
):
    account_id = store.seed_account(user_id="u1")

    async def refresh(self, *, refresh_token):
        from app.providers import TokenSet

        assert refresh_token == "RT-1"
        return TokenSet(refresh_token=None, access_token="AT-2", expires_in=3599, scopes=[])

    monkeypatch.setattr(google_provider.GoogleCalendarProvider, "refresh", refresh)

    response = client.post("/internal/access-token", json={"user_id": "u1"})
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == "AT-2"
    assert body["account_id"] == account_id


def test_access_token_can_target_a_specific_account(client, store, fake_crypto, monkeypatch):
    store.seed_account(user_id="u1", provider_account_id="sub-personal")
    work_id = store.seed_account(
        user_id="u1", provider_account_id="sub-work", make_default=False
    )

    async def refresh(self, *, refresh_token):
        from app.providers import TokenSet

        return TokenSet(refresh_token=None, access_token="AT-work", expires_in=3599, scopes=[])

    monkeypatch.setattr(google_provider.GoogleCalendarProvider, "refresh", refresh)

    response = client.post(
        "/internal/access-token", json={"user_id": "u1", "account_id": work_id}
    )
    assert response.status_code == 200
    assert response.json()["account_id"] == work_id


def test_unconnected_user_gets_409_not_500(client):
    response = client.post("/internal/access-token", json={"user_id": "ghost"})
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "not_connected"


def test_dead_grant_becomes_needs_reauth(client, store, fake_crypto, monkeypatch):
    """invalid_grant is a normal state — user revoked access, six months
    idle, a password change. It must degrade into a reconnect prompt, not a
    500, and the dead credential must actually be dropped."""
    account_id = store.seed_account(user_id="u1")

    async def revoked(self, *, refresh_token):
        raise NeedsReauth("revoked")

    monkeypatch.setattr(google_provider.GoogleCalendarProvider, "refresh", revoked)

    response = client.post("/internal/access-token", json={"user_id": "u1"})
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "needs_reauth"

    stored = store.accounts["u1"][account_id]
    assert stored["status"] == STATUS_NEEDS_REAUTH
    assert stored["encrypted_refresh_token"] is None


def test_rotated_refresh_token_is_persisted(client, store, fake_crypto, monkeypatch):
    """Google usually omits refresh_token on refresh, but rotates it in some
    flows. Dropping the new one leaves us using a dead credential."""
    account_id = store.seed_account(user_id="u1")

    async def rotating(self, *, refresh_token):
        from app.providers import TokenSet

        return TokenSet(
            refresh_token="RT-rotated", access_token="AT-3", expires_in=3600, scopes=[]
        )

    monkeypatch.setattr(google_provider.GoogleCalendarProvider, "refresh", rotating)
    assert (
        client.post("/internal/access-token", json={"user_id": "u1"}).status_code == 200
    )
    assert (
        store.accounts["u1"][account_id]["encrypted_refresh_token"]
        == "enc(RT-rotated|u1)"
    )


# ---------------------------------------------------------------------------
# /internal/disconnect
# ---------------------------------------------------------------------------


def test_disconnect_revokes_at_the_provider(client, store, fake_crypto, monkeypatch):
    account_id = store.seed_account(user_id="u1")
    revoked: list[str] = []

    async def revoke(self, *, refresh_token):
        revoked.append(refresh_token)

    monkeypatch.setattr(google_provider.GoogleCalendarProvider, "revoke", revoke)

    response = client.post(
        "/internal/disconnect", json={"user_id": "u1", "account_id": account_id}
    )
    assert response.status_code == 200
    assert revoked == ["RT-1"]
    assert account_id not in store.accounts["u1"]


def test_disconnect_unknown_account_is_404(client):
    response = client.post(
        "/internal/disconnect", json={"user_id": "u1", "account_id": "ghost"}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Habits
# ---------------------------------------------------------------------------


def test_habits_require_internal_caller(anon_client):
    assert (
        anon_client.post(
            "/internal/habits", json={"user_id": "u1", "label": "Gym", "goal": "3x/week"}
        ).status_code
        == 401
    )
    assert anon_client.get("/internal/habits?user_id=u1").status_code == 401


def test_create_habit_returns_a_stable_id(client):
    response = client.post(
        "/internal/habits",
        json={"user_id": "u1", "label": "Gym", "goal": "180 min/week, sessions 30-60 min"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["label"] == "Gym"
    assert body["status"] == "active"
    assert body["habit_id"]


def test_list_habits_empty_by_default(client):
    body = client.get("/internal/habits?user_id=u1").json()
    assert body == {"habits": []}


def test_list_habits_returns_created(client):
    client.post(
        "/internal/habits", json={"user_id": "u1", "label": "Gym", "goal": "3x/week"}
    )
    client.post(
        "/internal/habits", json={"user_id": "u1", "label": "Reading", "goal": "nightly"}
    )
    # A different user's habits must never show up in this list.
    client.post(
        "/internal/habits", json={"user_id": "u2", "label": "Meditation", "goal": "daily"}
    )

    habits = client.get("/internal/habits?user_id=u1").json()["habits"]
    assert {h["label"] for h in habits} == {"Gym", "Reading"}


def test_list_habits_filters_by_status(client):
    created = client.post(
        "/internal/habits", json={"user_id": "u1", "label": "Gym", "goal": "3x/week"}
    ).json()
    client.post(
        "/internal/habits", json={"user_id": "u1", "label": "Reading", "goal": "nightly"}
    )
    client.post(
        "/internal/habits/update",
        json={"user_id": "u1", "habit_id": created["habit_id"], "status": "paused"},
    )

    active = client.get("/internal/habits?user_id=u1&status=active").json()["habits"]
    paused = client.get("/internal/habits?user_id=u1&status=paused").json()["habits"]
    assert [h["label"] for h in active] == ["Reading"]
    assert [h["label"] for h in paused] == ["Gym"]


def test_update_habit_changes_fields(client):
    created = client.post(
        "/internal/habits", json={"user_id": "u1", "label": "Gym", "goal": "3x/week"}
    ).json()

    response = client.post(
        "/internal/habits/update",
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


def test_update_habit_unknown_is_404(client):
    response = client.post(
        "/internal/habits/update", json={"user_id": "u1", "habit_id": "ghost", "goal": "x"}
    )
    assert response.status_code == 404


def test_create_habit_defaults_allowed_zones_to_empty(client):
    response = client.post(
        "/internal/habits", json={"user_id": "u1", "label": "Gym", "goal": "3x/week"}
    )
    assert response.json()["allowed_zones"] == []


def test_update_habit_sets_allowed_zones(client):
    created = client.post(
        "/internal/habits", json={"user_id": "u1", "label": "Gym", "goal": "3x/week"}
    ).json()

    response = client.post(
        "/internal/habits/update",
        json={"user_id": "u1", "habit_id": created["habit_id"], "allowed_zones": ["Work"]},
    )
    assert response.status_code == 200
    assert response.json()["allowed_zones"] == ["Work"]


def test_update_habit_wrong_user_is_404(client):
    """A habit_id from one user must not be updatable by naming a different
    user_id — habits live under users/{user_id}/habits, so this is really
    just confirming that scoping."""
    created = client.post(
        "/internal/habits", json={"user_id": "u1", "label": "Gym", "goal": "3x/week"}
    ).json()

    response = client.post(
        "/internal/habits/update",
        json={"user_id": "u2", "habit_id": created["habit_id"], "goal": "hijacked"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Habit sessions
# ---------------------------------------------------------------------------


def _upsert_session(client, **overrides):
    body = {
        "user_id": "u1",
        "habit_id": "h1",
        "event_id": "e1",
        "calendar_id": "me@gmail.com",
        "planned_start": "2026-08-04T07:00:00-07:00",
        "planned_end": "2026-08-04T07:30:00-07:00",
    }
    body.update(overrides)
    return client.post("/internal/habit-sessions", json=body)


def test_habit_sessions_require_internal_caller(anon_client):
    assert (
        anon_client.post(
            "/internal/habit-sessions",
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
            "/internal/habit-sessions"
            "?user_id=u1&planned_from=2026-08-01T00:00:00Z&planned_to=2026-08-08T00:00:00Z"
        ).status_code
        == 401
    )


def test_upsert_habit_session_creates(client):
    response = _upsert_session(client)
    assert response.status_code == 200
    body = response.json()
    assert body["habit_id"] == "h1"
    assert body["event_id"] == "e1"
    assert body["session_id"]


def test_upsert_habit_session_same_event_updates_in_place(client):
    """A reschedule (update_calendar_event moving a tagged event) must
    update the existing plan, not create a second record for the same
    event — otherwise review_habit_week would see two conflicting plans
    for one actual event."""
    first = _upsert_session(client).json()

    second = _upsert_session(
        client,
        planned_start="2026-08-04T18:00:00-07:00",
        planned_end="2026-08-04T18:30:00-07:00",
    ).json()

    assert second["session_id"] == first["session_id"]
    assert second["planned_start"] == "2026-08-04T18:00:00-07:00"
    assert second["created_at"] == first["created_at"]  # preserved, not reset


def test_list_habit_sessions_filters_by_planned_start_range(client):
    _upsert_session(client, event_id="e-in-range", planned_start="2026-08-04T07:00:00-07:00")
    _upsert_session(client, event_id="e-before", planned_start="2026-07-20T07:00:00-07:00")
    _upsert_session(client, event_id="e-after", planned_start="2026-09-01T07:00:00-07:00")
    # A different user's session must never show up in this list.
    _upsert_session(client, user_id="u2", event_id="e-other-user")

    body = client.get(
        "/internal/habit-sessions"
        "?user_id=u1&planned_from=2026-08-01T00:00:00Z&planned_to=2026-08-08T00:00:00Z"
    ).json()
    assert [s["event_id"] for s in body["sessions"]] == ["e-in-range"]


def test_list_habit_sessions_empty_by_default(client):
    body = client.get(
        "/internal/habit-sessions"
        "?user_id=u1&planned_from=2026-08-01T00:00:00Z&planned_to=2026-08-08T00:00:00Z"
    ).json()
    assert body == {"sessions": []}


def test_upsert_habit_session_defaults_to_pending_status(client):
    """Three states, never two: a freshly-planned session is pending
    (unknown), not implicitly anything else — see A1.5."""
    body = _upsert_session(client).json()
    assert body["status"] == "pending"
    assert body["completed_at"] is None
    assert body["marked_by"] is None


def _set_status(client, **overrides):
    body = {
        "user_id": "u1",
        "calendar_id": "me@gmail.com",
        "event_id": "e1",
        "status": "completed",
        "marked_by": "user",
    }
    body.update(overrides)
    return client.post("/internal/habit-sessions/status", json=body)


def test_set_habit_session_status_requires_internal_caller(anon_client):
    response = anon_client.post(
        "/internal/habit-sessions/status",
        json={
            "user_id": "u1",
            "calendar_id": "me@gmail.com",
            "event_id": "e1",
            "status": "completed",
            "marked_by": "user",
        },
    )
    assert response.status_code == 401


def test_set_habit_session_status_marks_completed(client):
    _upsert_session(client)
    response = _set_status(client, status="completed", marked_by="user")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["marked_by"] == "user"
    assert body["completed_at"] is not None


def test_set_habit_session_status_marks_skipped_with_no_completed_at(client):
    _upsert_session(client)
    body = _set_status(client, status="skipped", marked_by="agent").json()
    assert body["status"] == "skipped"
    assert body["marked_by"] == "agent"
    assert body["completed_at"] is None


def test_set_habit_session_status_not_found_is_404(client):
    response = _set_status(client, event_id="never-planned")
    assert response.status_code == 404


def test_set_habit_session_status_not_found_never_creates_a_session(client):
    """A status call must never be a side door that creates a session
    without a plan — upsert_habit_session (add_calendar_event/
    update_calendar_event tagging a habit session) is the only writer of
    the plan fields."""
    _set_status(client, event_id="never-planned")
    body = client.get(
        "/internal/habit-sessions"
        "?user_id=u1&planned_from=2026-01-01T00:00:00Z&planned_to=2027-01-01T00:00:00Z"
    ).json()
    assert body == {"sessions": []}


def test_set_habit_session_status_is_idempotent(client):
    """Marking complete twice must not keep bumping completed_at forward —
    a genuine no-op on the second call, not just the same status value."""
    _upsert_session(client)
    first = _set_status(client, status="completed").json()

    second = _set_status(client, status="completed").json()

    assert second["completed_at"] == first["completed_at"]
    assert second["updated_at"] == first["updated_at"]


def test_set_habit_session_status_transition_updates_completed_at(client):
    _upsert_session(client)
    _set_status(client, status="skipped")

    completed = _set_status(client, status="completed").json()
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None


def test_moved_and_completed_session_is_not_a_failure(client):
    """The whole point of A1.5: a session the user moved and then actually
    did must be reportable as a success, not a partial failure the
    calendar diff alone would suggest."""
    _upsert_session(client)
    body = _set_status(client, status="completed", marked_by="user").json()
    assert body["status"] == "completed"


def test_completion_survives_a_reschedule(client):
    """The reschedule-survival invariant (A1.5): upsert_habit_session runs
    again every time update_calendar_event patches a habit-tagged event in
    place — same (calendar_id, event_id), so the same document — and must
    never reset a completion that was already recorded on it."""
    _upsert_session(client)
    completed = _set_status(client, status="completed", marked_by="user").json()

    # Simulates update_calendar_event rescheduling the same tagged event —
    # patched in place, so calendar_id/event_id (and the derived
    # session_id) are unchanged.
    rescheduled = _upsert_session(
        client,
        planned_start="2026-08-05T18:00:00-07:00",
        planned_end="2026-08-05T18:30:00-07:00",
    ).json()

    assert rescheduled["session_id"] == completed["session_id"]
    assert rescheduled["planned_start"] == "2026-08-05T18:00:00-07:00"
    assert rescheduled["status"] == "completed"
    assert rescheduled["marked_by"] == "user"
    assert rescheduled["completed_at"] == completed["completed_at"]


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------


def test_zones_require_internal_caller(anon_client):
    assert (
        anon_client.post(
            "/internal/zones",
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
    assert anon_client.get("/internal/zones?user_id=u1").status_code == 401


def test_create_zone_returns_a_stable_id(client):
    response = client.post(
        "/internal/zones",
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


def test_create_zone_rejects_malformed_time(client):
    response = client.post(
        "/internal/zones",
        json={
            "user_id": "u1",
            "label": "Work",
            "start_time": "9am",
            "end_time": "17:00",
            "days_of_week": ["mon"],
        },
    )
    assert response.status_code == 422


def test_list_zones_empty_by_default(client):
    assert client.get("/internal/zones?user_id=u1").json() == {"zones": []}


def test_list_zones_returns_created(client):
    client.post(
        "/internal/zones",
        json={
            "user_id": "u1",
            "label": "Work",
            "start_time": "09:00",
            "end_time": "17:00",
            "days_of_week": ["mon"],
        },
    )
    # A different user's zones must never show up in this list.
    client.post(
        "/internal/zones",
        json={
            "user_id": "u2",
            "label": "Commute",
            "start_time": "08:00",
            "end_time": "09:00",
            "days_of_week": ["mon"],
        },
    )

    zones = client.get("/internal/zones?user_id=u1").json()["zones"]
    assert {z["label"] for z in zones} == {"Work"}


def test_update_zone_changes_fields(client):
    created = client.post(
        "/internal/zones",
        json={
            "user_id": "u1",
            "label": "Work",
            "start_time": "09:00",
            "end_time": "17:00",
            "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
        },
    ).json()

    response = client.post(
        "/internal/zones/update",
        json={"user_id": "u1", "zone_id": created["zone_id"], "end_time": "18:00"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["end_time"] == "18:00"
    assert body["start_time"] == "09:00"  # untouched field survives a partial update


def test_update_zone_unknown_is_404(client):
    response = client.post(
        "/internal/zones/update", json={"user_id": "u1", "zone_id": "ghost", "end_time": "18:00"}
    )
    assert response.status_code == 404


def test_update_zone_wrong_user_is_404(client):
    created = client.post(
        "/internal/zones",
        json={
            "user_id": "u1",
            "label": "Work",
            "start_time": "09:00",
            "end_time": "17:00",
            "days_of_week": ["mon"],
        },
    ).json()

    response = client.post(
        "/internal/zones/update",
        json={"user_id": "u2", "zone_id": created["zone_id"], "end_time": "18:00"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Sleep schedule
# ---------------------------------------------------------------------------


def test_sleep_schedule_requires_internal_caller(anon_client):
    assert (
        anon_client.post("/internal/sleep-schedule", json={"user_id": "u1"}).status_code == 401
    )
    assert anon_client.get("/internal/sleep-schedule?user_id=u1").status_code == 401


def test_get_sleep_schedule_reports_not_configured(client):
    body = client.get("/internal/sleep-schedule?user_id=u1").json()
    assert body == {"exists": False, "schedule": None}


def test_set_sleep_schedule_creates(client):
    response = client.post(
        "/internal/sleep-schedule",
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


def test_set_sleep_schedule_partial_update_preserves_other_fields(client):
    client.post(
        "/internal/sleep-schedule",
        json={
            "user_id": "u1",
            "sleep_time": "23:00",
            "wake_time": "07:00",
            "cool_down_minutes": 30,
            "wake_up_buffer_minutes": 15,
        },
    )

    response = client.post(
        "/internal/sleep-schedule", json={"user_id": "u1", "cool_down_minutes": 45}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cool_down_minutes"] == 45
    assert body["sleep_time"] == "23:00"  # untouched field survives a partial update


def test_set_sleep_schedule_day_overrides_round_trip(client):
    response = client.post(
        "/internal/sleep-schedule",
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

    fetched = client.get("/internal/sleep-schedule?user_id=u1").json()
    assert fetched["exists"] is True
    assert fetched["schedule"]["day_overrides"] == {
        "sun": {"sleep_time": None, "wake_time": "09:00"}
    }


def test_set_sleep_schedule_day_overrides_replaces_wholesale(client):
    """day_overrides isn't a per-day merge — passing a new map drops
    whatever wasn't included, matching the documented contract on
    SetSleepScheduleRequest.day_overrides."""
    client.post(
        "/internal/sleep-schedule",
        json={
            "user_id": "u1",
            "sleep_time": "23:00",
            "wake_time": "07:00",
            "cool_down_minutes": 30,
            "wake_up_buffer_minutes": 15,
            "day_overrides": {"sun": {"wake_time": "09:00"}, "sat": {"wake_time": "10:00"}},
        },
    )

    response = client.post(
        "/internal/sleep-schedule",
        json={"user_id": "u1", "day_overrides": {"sun": {"wake_time": "09:30"}}},
    )
    assert response.json()["day_overrides"] == {"sun": {"sleep_time": None, "wake_time": "09:30"}}
