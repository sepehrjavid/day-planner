"""Coverage of /me/zones (A6.3) — the user-facing counterpart to
/agent/zones (A6.2), including the DELETE that has no /agent equivalent
(see routes/zones.py's own docstring for why deletion is /me-only).

test_zones.py already covers store.zones.create/list/update
directly; these exercise the same store methods through the route, so
what's new here is auth scoping and HTTP status mapping, plus delete_zone
itself (new in A6.3, no A6.1 predecessor to already be covered).
"""


def _create_zone(anon_client, headers, **overrides):
    body = {
        "label": "Work",
        "start_time": "09:00",
        "end_time": "17:00",
        "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
    }
    body.update(overrides)
    return anon_client.post("/me/zones", json=body, headers=headers)


def test_create_zone_returns_a_stable_id(anon_client, user):
    _, headers = user
    response = _create_zone(anon_client, headers)
    assert response.status_code == 200
    body = response.json()
    assert body["label"] == "Work"
    assert body["zone_id"]


def test_create_zone_rejects_malformed_time(anon_client, user):
    _, headers = user
    response = _create_zone(anon_client, headers, start_time="9am")
    assert response.status_code == 422


def test_list_zones_empty_by_default(anon_client, user):
    _, headers = user
    assert anon_client.get("/me/zones", headers=headers).json() == {"zones": []}


def test_list_zones_returns_only_the_caller_s_own(anon_client, user):
    _, headers = user
    _create_zone(anon_client, headers)

    other = anon_client.post(
        "/auth/signup", json={"email": "zone-other@example.com", "password": "correct-horse-battery-staple"}
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    _create_zone(anon_client, other_headers, label="Commute")

    zones = anon_client.get("/me/zones", headers=headers).json()["zones"]
    assert [z["label"] for z in zones] == ["Work"]


def test_update_zone_changes_fields(anon_client, user):
    _, headers = user
    created = _create_zone(anon_client, headers).json()

    response = anon_client.post(
        "/me/zones/update",
        json={"zone_id": created["zone_id"], "end_time": "18:00"},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["end_time"] == "18:00"
    assert body["start_time"] == "09:00"


def test_update_zone_unknown_is_404(anon_client, user):
    _, headers = user
    response = anon_client.post(
        "/me/zones/update", json={"zone_id": "ghost", "end_time": "18:00"}, headers=headers
    )
    assert response.status_code == 404


def test_update_zone_another_user_s_zone_is_404(anon_client, user):
    _, headers = user
    created = _create_zone(anon_client, headers).json()

    other = anon_client.post(
        "/auth/signup", json={"email": "zone-other2@example.com", "password": "correct-horse-battery-staple"}
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    response = anon_client.post(
        "/me/zones/update",
        json={"zone_id": created["zone_id"], "end_time": "18:00"},
        headers=other_headers,
    )
    assert response.status_code == 404


def test_delete_zone_removes_it(anon_client, user):
    _, headers = user
    created = _create_zone(anon_client, headers).json()

    response = anon_client.delete(f"/me/zones/{created['zone_id']}", headers=headers)
    assert response.status_code == 204

    zones = anon_client.get("/me/zones", headers=headers).json()["zones"]
    assert zones == []


def test_delete_zone_unknown_is_404(anon_client, user):
    _, headers = user
    response = anon_client.delete("/me/zones/ghost", headers=headers)
    assert response.status_code == 404


def test_delete_zone_another_user_s_zone_is_404(anon_client, user):
    _, headers = user
    created = _create_zone(anon_client, headers).json()

    other = anon_client.post(
        "/auth/signup", json={"email": "zone-other3@example.com", "password": "correct-horse-battery-staple"}
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    response = anon_client.delete(f"/me/zones/{created['zone_id']}", headers=other_headers)
    assert response.status_code == 404

    # And it must still be there for the actual owner.
    zones = anon_client.get("/me/zones", headers=headers).json()["zones"]
    assert [z["zone_id"] for z in zones] == [created["zone_id"]]


def test_zones_require_a_session(anon_client):
    assert (
        anon_client.post(
            "/me/zones",
            json={
                "label": "Work",
                "start_time": "09:00",
                "end_time": "17:00",
                "days_of_week": ["mon"],
            },
        ).status_code
        == 401
    )
    assert anon_client.get("/me/zones").status_code == 401
    assert anon_client.delete("/me/zones/some-id").status_code == 401
