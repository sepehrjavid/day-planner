"""Coverage of /me/habit-sessions/status (A1.5, rewired by A6.1).

Habit session data lives in this service's own Store as of A6.1, so
these tests seed the `store` fixture directly instead of monkeypatching
a since-deleted internal_client — day_planner_backend_internal's own
test suite used to cover the equivalent logic before the move; it now
lives here alongside the code. What's under test is that this route
resolves user_id only from the session token, never trusts a
client-supplied marked_by, and maps a missing session to a 404.

Calling store.upsert_habit_session directly (not through a route) means
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
    return store.upsert_habit_session(user_id=user_id, **body)


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
    assert store.habit_sessions[user_id]["me@gmail.com__e1"]["status"] == "completed"
    assert store.habit_sessions["someone-else"]["me@gmail.com__e1"]["status"] == "pending"


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
    assert store.habit_sessions[user_id]["me@gmail.com__e1"]["marked_by"] == "user"


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
