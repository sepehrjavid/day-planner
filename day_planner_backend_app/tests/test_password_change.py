"""Coverage of POST /me/password (A6.4).

What's under test: the current password is genuinely required (session
possession alone must not be enough), the new password goes through the
same length policy signup uses, and a successful change evicts every
other session for this user while keeping the caller's own.
"""

GOOD_PASSWORD = "correct-horse-battery-staple"
NEW_PASSWORD = "another-correct-horse-battery"


def test_requires_auth(anon_client):
    response = anon_client.post(
        "/me/password", json={"current_password": GOOD_PASSWORD, "new_password": NEW_PASSWORD}
    )
    assert response.status_code == 401


def test_wrong_current_password_is_rejected(anon_client, user):
    _, headers = user
    response = anon_client.post(
        "/me/password",
        json={"current_password": "totally-wrong-password", "new_password": NEW_PASSWORD},
        headers=headers,
    )
    assert response.status_code == 401


def test_new_password_must_meet_the_length_policy(anon_client, user):
    _, headers = user
    response = anon_client.post(
        "/me/password",
        json={"current_password": GOOD_PASSWORD, "new_password": "short"},
        headers=headers,
    )
    assert response.status_code == 422


def test_successful_change_allows_login_with_new_password(anon_client, user):
    _, headers = user
    response = anon_client.post(
        "/me/password",
        json={"current_password": GOOD_PASSWORD, "new_password": NEW_PASSWORD},
        headers=headers,
    )
    assert response.status_code == 204

    old_login = anon_client.post(
        "/auth/login", json={"email": "me@example.com", "password": GOOD_PASSWORD}
    )
    assert old_login.status_code == 401

    new_login = anon_client.post(
        "/auth/login", json={"email": "me@example.com", "password": NEW_PASSWORD}
    )
    assert new_login.status_code == 200


def test_successful_change_evicts_other_sessions_but_keeps_the_caller_s_own(
    anon_client, user
):
    _, headers = user
    other_login = anon_client.post(
        "/auth/login", json={"email": "me@example.com", "password": GOOD_PASSWORD}
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['access_token']}"}

    response = anon_client.post(
        "/me/password",
        json={"current_password": GOOD_PASSWORD, "new_password": NEW_PASSWORD},
        headers=headers,
    )
    assert response.status_code == 204

    # The session that made the change is still valid.
    assert anon_client.get("/me", headers=headers).status_code == 200
    # The other, previously-valid session is now dead.
    assert anon_client.get("/me", headers=other_headers).status_code == 401
