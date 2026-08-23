"""Coverage of /me/sleep-schedule (A6.3) — the user-facing counterpart to
/agent/sleep-schedule (A6.2). See routes/sleep_schedule.py's own
docstring for why the two route bodies are nearly byte-identical.

test_sleep_schedule.py already covers store.sleep_schedule.get/
set_sleep_schedule directly; these exercise the same store methods
through the route, so what's new here is auth scoping.
"""


def test_get_sleep_schedule_reports_not_configured(anon_client, user):
    _, headers = user
    body = anon_client.get("/me/sleep-schedule", headers=headers).json()
    assert body == {"exists": False, "schedule": None}


def test_set_sleep_schedule_creates(anon_client, user):
    _, headers = user
    response = anon_client.post(
        "/me/sleep-schedule",
        json={
            "sleep_time": "23:00",
            "wake_time": "07:00",
            "cool_down_minutes": 30,
            "wake_up_buffer_minutes": 15,
        },
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sleep_time"] == "23:00"
    assert body["wake_time"] == "07:00"
    assert body["day_overrides"] == {}


def test_set_sleep_schedule_partial_update_preserves_other_fields(anon_client, user):
    _, headers = user
    anon_client.post(
        "/me/sleep-schedule",
        json={
            "sleep_time": "23:00",
            "wake_time": "07:00",
            "cool_down_minutes": 30,
            "wake_up_buffer_minutes": 15,
        },
        headers=headers,
    )

    response = anon_client.post(
        "/me/sleep-schedule", json={"cool_down_minutes": 45}, headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cool_down_minutes"] == 45
    assert body["sleep_time"] == "23:00"


def test_set_sleep_schedule_day_overrides_round_trip(anon_client, user):
    _, headers = user
    response = anon_client.post(
        "/me/sleep-schedule",
        json={
            "sleep_time": "23:00",
            "wake_time": "07:00",
            "cool_down_minutes": 30,
            "wake_up_buffer_minutes": 15,
            "day_overrides": {"sun": {"wake_time": "09:00"}},
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["day_overrides"] == {"sun": {"sleep_time": None, "wake_time": "09:00"}}

    fetched = anon_client.get("/me/sleep-schedule", headers=headers).json()
    assert fetched["exists"] is True
    assert fetched["schedule"]["day_overrides"] == {
        "sun": {"sleep_time": None, "wake_time": "09:00"}
    }


def test_sleep_schedule_is_scoped_to_the_caller(anon_client, user):
    _, headers = user
    anon_client.post(
        "/me/sleep-schedule",
        json={"sleep_time": "23:00", "wake_time": "07:00"},
        headers=headers,
    )

    other = anon_client.post(
        "/auth/signup", json={"email": "sleep-other@example.com", "password": "correct-horse-battery-staple"}
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    body = anon_client.get("/me/sleep-schedule", headers=other_headers).json()
    assert body == {"exists": False, "schedule": None}


def test_sleep_schedule_requires_a_session(anon_client):
    assert anon_client.get("/me/sleep-schedule").status_code == 401
    assert anon_client.post("/me/sleep-schedule", json={}).status_code == 401
