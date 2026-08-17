"""Coverage of /me/habit-sessions/status (A1.5).

internal_client.set_habit_session_status is monkeypatched here rather than
exercised for real — there's no live internal service in this test
process, and day_planner_backend_internal's own test suite already covers
the route this calls into. What's under test is that this route resolves
user_id only from the session token, never trusts a client-supplied
marked_by, and maps internal_client's None (not found) to a 404.
"""

from app.services import internal_client


def _session_payload(**overrides) -> dict:
    payload = {
        "session_id": "me@gmail.com__e1",
        "habit_id": "h1",
        "event_id": "e1",
        "calendar_id": "me@gmail.com",
        "planned_start": "2026-08-04T07:00:00-07:00",
        "planned_end": "2026-08-04T07:30:00-07:00",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-04T09:00:00Z",
        "status": "completed",
        "completed_at": "2026-08-04T09:00:00Z",
        "marked_by": "user",
    }
    payload.update(overrides)
    return payload


def test_requires_auth(anon_client, monkeypatch):
    calls = []

    async def fake_set_status(settings, **kwargs):
        calls.append(kwargs)
        return _session_payload()

    monkeypatch.setattr(internal_client, "set_habit_session_status", fake_set_status)

    response = anon_client.post(
        "/me/habit-sessions/status",
        json={"calendar_id": "me@gmail.com", "event_id": "e1", "status": "completed"},
    )
    assert response.status_code == 401
    assert calls == []


def test_identity_comes_from_session_token_not_body(anon_client, user, monkeypatch):
    """The whole trust boundary this route exists to enforce — see A1.5's
    "rejects any attempt to set status on another user's session"
    acceptance criterion. The schema doesn't even define a user_id field,
    but a smuggled one in the raw JSON body must still have no effect."""
    user_id, headers = user
    calls = []

    async def fake_set_status(settings, **kwargs):
        calls.append(kwargs)
        return _session_payload()

    monkeypatch.setattr(internal_client, "set_habit_session_status", fake_set_status)

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
    assert len(calls) == 1
    assert calls[0]["user_id"] == user_id


def test_marked_by_is_always_user_never_client_supplied(anon_client, user, monkeypatch):
    user_id, headers = user
    calls = []

    async def fake_set_status(settings, **kwargs):
        calls.append(kwargs)
        return _session_payload()

    monkeypatch.setattr(internal_client, "set_habit_session_status", fake_set_status)

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
    assert calls[0]["marked_by"] == "user"


def test_returns_404_when_session_not_found(anon_client, user, monkeypatch):
    async def fake_set_status(settings, **kwargs):
        return None

    monkeypatch.setattr(internal_client, "set_habit_session_status", fake_set_status)
    _, headers = user

    response = anon_client.post(
        "/me/habit-sessions/status",
        json={"calendar_id": "me@gmail.com", "event_id": "never-planned", "status": "completed"},
        headers=headers,
    )
    assert response.status_code == 404


def test_returns_session_on_success(anon_client, user, monkeypatch):
    async def fake_set_status(settings, **kwargs):
        return _session_payload(status="skipped", completed_at=None, marked_by="user")

    monkeypatch.setattr(internal_client, "set_habit_session_status", fake_set_status)
    _, headers = user

    response = anon_client.post(
        "/me/habit-sessions/status",
        json={"calendar_id": "me@gmail.com", "event_id": "e1", "status": "skipped"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "skipped"
    assert body["completed_at"] is None


def test_can_reset_status_to_pending(anon_client, user, monkeypatch):
    """Undoing a mis-mark is a valid transition, not just forward marks."""
    calls = []

    async def fake_set_status(settings, **kwargs):
        calls.append(kwargs)
        return _session_payload(status="pending", completed_at=None, marked_by="user")

    monkeypatch.setattr(internal_client, "set_habit_session_status", fake_set_status)
    _, headers = user

    response = anon_client.post(
        "/me/habit-sessions/status",
        json={"calendar_id": "me@gmail.com", "event_id": "e1", "status": "pending"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert calls[0]["status"] == "pending"
    assert response.json()["status"] == "pending"
