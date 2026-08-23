"""Coverage of /me/habits (A6.3) — the user-facing counterpart to
/agent/habits (A6.2), reachable via a session token instead of an OIDC
service identity.

test_habits.py already covers store.habits.create/list/update
directly; these exercise the same store methods through the route, so
what's new here is auth scoping (current_user_id, never a body field)
and the HTTP status mapping (404 on an unknown or cross-user habit_id).
"""


def test_create_habit_returns_a_stable_id(anon_client, user):
    _, headers = user
    response = anon_client.post(
        "/me/habits",
        json={"label": "Gym", "goal": "180 min/week, sessions 30-60 min"},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["label"] == "Gym"
    assert body["status"] == "active"
    assert body["habit_id"]
    assert body["allowed_zones"] == []


def test_list_habits_empty_by_default(anon_client, user):
    _, headers = user
    assert anon_client.get("/me/habits", headers=headers).json() == {"habits": []}


def test_list_habits_returns_only_the_caller_s_own(anon_client, user):
    _, headers = user
    anon_client.post("/me/habits", json={"label": "Gym", "goal": "3x/week"}, headers=headers)

    other = anon_client.post(
        "/auth/signup", json={"email": "other@example.com", "password": "correct-horse-battery-staple"}
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    anon_client.post(
        "/me/habits", json={"label": "Meditation", "goal": "daily"}, headers=other_headers
    )

    habits = anon_client.get("/me/habits", headers=headers).json()["habits"]
    assert [h["label"] for h in habits] == ["Gym"]


def test_list_habits_filters_by_status(anon_client, user):
    _, headers = user
    created = anon_client.post(
        "/me/habits", json={"label": "Gym", "goal": "3x/week"}, headers=headers
    ).json()
    anon_client.post(
        "/me/habits", json={"label": "Reading", "goal": "nightly"}, headers=headers
    )
    anon_client.post(
        "/me/habits/update",
        json={"habit_id": created["habit_id"], "status": "paused"},
        headers=headers,
    )

    active = anon_client.get("/me/habits?status=active", headers=headers).json()["habits"]
    paused = anon_client.get("/me/habits?status=paused", headers=headers).json()["habits"]
    assert [h["label"] for h in active] == ["Reading"]
    assert [h["label"] for h in paused] == ["Gym"]


def test_update_habit_changes_fields(anon_client, user):
    _, headers = user
    created = anon_client.post(
        "/me/habits", json={"label": "Gym", "goal": "3x/week"}, headers=headers
    ).json()

    response = anon_client.post(
        "/me/habits/update",
        json={"habit_id": created["habit_id"], "goal": "5x/week now"},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["goal"] == "5x/week now"
    assert body["label"] == "Gym"


def test_update_habit_unknown_is_404(anon_client, user):
    _, headers = user
    response = anon_client.post(
        "/me/habits/update", json={"habit_id": "ghost", "goal": "x"}, headers=headers
    )
    assert response.status_code == 404


def test_update_habit_another_user_s_habit_is_404(anon_client, user):
    _, headers = user
    created = anon_client.post(
        "/me/habits", json={"label": "Gym", "goal": "3x/week"}, headers=headers
    ).json()

    other = anon_client.post(
        "/auth/signup", json={"email": "other2@example.com", "password": "correct-horse-battery-staple"}
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    response = anon_client.post(
        "/me/habits/update",
        json={"habit_id": created["habit_id"], "goal": "hijacked"},
        headers=other_headers,
    )
    assert response.status_code == 404


def test_update_habit_sets_allowed_zones(anon_client, user):
    _, headers = user
    created = anon_client.post(
        "/me/habits", json={"label": "Gym", "goal": "3x/week"}, headers=headers
    ).json()

    response = anon_client.post(
        "/me/habits/update",
        json={"habit_id": created["habit_id"], "allowed_zones": ["Work"]},
        headers=headers,
    )
    assert response.json()["allowed_zones"] == ["Work"]


def test_habits_require_a_session(anon_client):
    assert anon_client.post("/me/habits", json={"label": "Gym", "goal": "x"}).status_code == 401
    assert anon_client.get("/me/habits").status_code == 401
