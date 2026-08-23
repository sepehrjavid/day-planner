"""Coverage of /me/habit-sessions: the status route (A1.5, rewired by
A6.1) and the list route (new in A6.3).

Habit session data lives in this service's own Store as of A6.1, so
these tests seed the `store` fixture directly instead of monkeypatching
a since-deleted internal_client — day_planner_backend_internal's own
test suite used to cover the equivalent logic before the move; it now
lives here alongside the code. What's under test is that both routes
resolve user_id only from the session token, the status route never
trusts a client-supplied marked_by, and a missing session 404s rather
than being silently created.

Calling store.habit_sessions.upsert directly (not through a route) means
planned_start/planned_end must be real datetime objects, not ISO
strings — nothing here parses them the way a Pydantic schema would.
"""

from datetime import datetime


def _upsert_session(store, *, user_id, **overrides):
    body = {
        "habit_id": "h1",
        "event_id": "e1",
        "calendar_id": "me@gmail.com",
        "planned_start": datetime.fromisoformat("2026-08-04T07:00:00-07:00"),
        "planned_end": datetime.fromisoformat("2026-08-04T07:30:00-07:00"),
    }
    body.update(overrides)
    return store.habit_sessions.upsert(user_id=user_id, **body)


def test_requires_auth(anon_client):
    response = anon_client.post(
        "/me/habit-sessions/status",
        json={"calendar_id": "me@gmail.com", "event_id": "e1", "status": "completed"},
    )
    assert response.status_code == 401


async def test_identity_comes_from_session_token_not_body(anon_client, user, store):
    """The whole trust boundary this route exists to enforce — see A1.5's
    "rejects any attempt to set status on another user's session"
    acceptance criterion. The schema doesn't even define a user_id field,
    but a smuggled one in the raw JSON body must still have no effect."""
    user_id, headers = user
    await _upsert_session(store, user_id=user_id)
    # A different user's session with the same (calendar_id, event_id) —
    # if the smuggled body user_id leaked through, this is the one that
    # would get marked instead.
    await _upsert_session(store, user_id="someone-else")

    response = anon_client.post(
        "/me/habit-sessions/status",
        json={
            "calendar_id": "me@gmail.com",
            "event_id": "e1",
            "status": "completed",
            "user_id": "someone-else",
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert store._habit_sessions[user_id]["me@gmail.com__e1"]["status"] == "completed"
    assert store._habit_sessions["someone-else"]["me@gmail.com__e1"]["status"] == "pending"


async def test_marked_by_is_always_user_never_client_supplied(anon_client, user, store):
    user_id, headers = user
    await _upsert_session(store, user_id=user_id)

    response = anon_client.post(
        "/me/habit-sessions/status",
        json={
            "calendar_id": "me@gmail.com",
            "event_id": "e1",
            "status": "completed",
            "marked_by": "agent",
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert store._habit_sessions[user_id]["me@gmail.com__e1"]["marked_by"] == "user"


def test_returns_404_when_session_not_found(anon_client, user):
    _, headers = user

    response = anon_client.post(
        "/me/habit-sessions/status",
        json={"calendar_id": "me@gmail.com", "event_id": "never-planned", "status": "completed"},
        headers=headers,
    )
    assert response.status_code == 404


async def test_returns_session_on_success(anon_client, user, store):
    user_id, headers = user
    await _upsert_session(store, user_id=user_id)

    response = anon_client.post(
        "/me/habit-sessions/status",
        json={"calendar_id": "me@gmail.com", "event_id": "e1", "status": "skipped"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "skipped"
    assert body["completed_at"] is None


async def test_can_reset_status_to_pending(anon_client, user, store):
    """Undoing a mis-mark is a valid transition, not just forward marks."""
    user_id, headers = user
    await _upsert_session(store, user_id=user_id)
    anon_client.post(
        "/me/habit-sessions/status",
        json={"calendar_id": "me@gmail.com", "event_id": "e1", "status": "completed"},
        headers=headers,
    )

    response = anon_client.post(
        "/me/habit-sessions/status",
        json={"calendar_id": "me@gmail.com", "event_id": "e1", "status": "pending"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["completed_at"] is None


# ---------------------------------------------------------------------------
# GET /me/habit-sessions (A6.3) — the user-facing counterpart to
# /agent/habit-sessions (A6.2), scoped to current_user_id instead of a
# body field.
# ---------------------------------------------------------------------------


def test_list_requires_auth(anon_client):
    response = anon_client.get(
        "/me/habit-sessions"
        "?planned_from=2026-08-01T00:00:00Z&planned_to=2026-08-08T00:00:00Z"
    )
    assert response.status_code == 401


async def test_list_returns_only_the_caller_s_own_sessions(anon_client, user, store):
    user_id, headers = user
    await _upsert_session(store, user_id=user_id, event_id="mine")
    await _upsert_session(store, user_id="someone-else", event_id="not-mine")

    response = anon_client.get(
        "/me/habit-sessions"
        "?planned_from=2026-08-01T00:00:00Z&planned_to=2026-08-08T00:00:00Z",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [s["event_id"] for s in body["sessions"]] == ["mine"]


async def test_list_filters_by_planned_start_range(anon_client, user, store):
    user_id, headers = user
    await _upsert_session(
        store,
        user_id=user_id,
        event_id="in-range",
        planned_start=datetime.fromisoformat("2026-08-04T07:00:00-07:00"),
    )
    await _upsert_session(
        store,
        user_id=user_id,
        event_id="before",
        planned_start=datetime.fromisoformat("2026-07-20T07:00:00-07:00"),
    )

    response = anon_client.get(
        "/me/habit-sessions"
        "?planned_from=2026-08-01T00:00:00Z&planned_to=2026-08-08T00:00:00Z",
        headers=headers,
    )

    assert [s["event_id"] for s in response.json()["sessions"]] == ["in-range"]
