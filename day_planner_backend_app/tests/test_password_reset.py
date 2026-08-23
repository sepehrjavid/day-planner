"""Coverage of /auth/password-reset/request and /confirm (A6.4).

email.send_password_reset_email is monkeypatched to a no-op recorder in
every test here — no test should ever depend on a real SendGrid call
succeeding, and the enumeration-resistance tests specifically need to
observe whether it was called at all without it touching the network.

The send is scheduled fire-and-forget (email_service.
schedule_password_reset_email, added on review — see that function's own
docstring) via asyncio.create_task on TestClient's own background event
loop, a different thread than this synchronous test. anon_client.post()
returning is therefore not proof the task has run yet, so tests that need
to observe a send poll for it with _wait_until rather than asserting
immediately; tests that need to prove a send did NOT happen use _settle
to give a wrongly-scheduled task a fair chance to show up before checking.

What's under test: the request endpoint responds identically regardless
of whether the address exists or the request was rate-limited; confirm
consumes the token exactly once; a successful reset evicts every session,
including the caller's own; and the new password goes through the same
length policy as signup.
"""

import time

import pytest

from app.services import email as email_service

GOOD_PASSWORD = "correct-horse-battery-staple"
NEW_PASSWORD = "another-correct-horse-battery"


def _wait_until(predicate, *, timeout=2.0, interval=0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _settle(seconds: float = 0.2) -> None:
    """Give a scheduled-but-not-yet-run background task a chance to
    execute before asserting it didn't happen."""
    time.sleep(seconds)


@pytest.fixture
def sent_emails(monkeypatch):
    """Records (to_email, reset_url) for every call instead of hitting
    SendGrid."""
    calls: list[tuple[str, str]] = []

    async def fake_send(*, settings, to_email, reset_url):
        calls.append((to_email, reset_url))

    monkeypatch.setattr(email_service, "send_password_reset_email", fake_send)
    return calls


def _reset_url_token(url: str) -> str:
    return url.rsplit("token=", 1)[1]


def _request_and_get_token(anon_client, sent_emails, email: str) -> str:
    anon_client.post("/auth/password-reset/request", json={"email": email})
    assert _wait_until(lambda: len(sent_emails) == 1)
    return _reset_url_token(sent_emails[0][1])


def test_request_responds_202_for_a_registered_address(anon_client, user, sent_emails):
    response = anon_client.post(
        "/auth/password-reset/request", json={"email": "me@example.com"}
    )
    assert response.status_code == 202
    assert response.json() is None
    assert _wait_until(lambda: len(sent_emails) == 1)
    assert sent_emails[0][0] == "me@example.com"


def test_request_responds_identically_for_an_unregistered_address(
    anon_client, sent_emails
):
    response = anon_client.post(
        "/auth/password-reset/request", json={"email": "nobody@example.com"}
    )
    assert response.status_code == 202
    assert response.json() is None
    _settle()
    assert sent_emails == []


def test_request_locked_out_by_email_still_responds_202_and_sends_nothing(
    anon_client, user, sent_emails, store
):
    for _ in range(8):  # settings.password_reset_max_attempts default
        anon_client.post("/auth/password-reset/request", json={"email": "me@example.com"})
    _settle()
    sent_emails.clear()

    response = anon_client.post(
        "/auth/password-reset/request", json={"email": "me@example.com"}
    )
    assert response.status_code == 202
    assert response.json() is None
    _settle()
    assert sent_emails == []


def test_request_locked_out_by_ip_still_responds_202_and_sends_nothing(
    anon_client, sent_emails
):
    for i in range(8):
        anon_client.post(
            "/auth/password-reset/request", json={"email": f"target{i}@example.com"}
        )
    _settle()
    sent_emails.clear()

    response = anon_client.post(
        "/auth/password-reset/request", json={"email": "yet-another@example.com"}
    )
    assert response.status_code == 202
    assert response.json() is None
    _settle()
    assert sent_emails == []


def test_a_send_failure_does_not_change_the_response(anon_client, user, monkeypatch):
    """The send is fire-and-forget, so the response can't depend on its
    outcome at all — this asserts the request still succeeds, and that a
    background failure never propagates anywhere observable."""

    async def failing_send(*, settings, to_email, reset_url):
        raise email_service.SendEmailError("simulated SendGrid outage")

    monkeypatch.setattr(email_service, "send_password_reset_email", failing_send)

    response = anon_client.post(
        "/auth/password-reset/request", json={"email": "me@example.com"}
    )
    assert response.status_code == 202
    assert response.json() is None
    _settle()  # let the background task run and fail; must not raise anywhere


def test_confirm_with_a_valid_token_changes_the_password(anon_client, user, sent_emails):
    token = _request_and_get_token(anon_client, sent_emails, "me@example.com")

    response = anon_client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": NEW_PASSWORD},
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


def test_confirm_token_is_single_use(anon_client, user, sent_emails):
    token = _request_and_get_token(anon_client, sent_emails, "me@example.com")

    first = anon_client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert first.status_code == 204

    second = anon_client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": "yet-another-new-password"},
    )
    assert second.status_code == 400


def test_confirm_with_an_unknown_token_is_rejected(anon_client):
    response = anon_client.post(
        "/auth/password-reset/confirm",
        json={"token": "not-a-real-token", "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 400


def test_confirm_rejects_a_weak_new_password_without_burning_the_token(
    anon_client, user, sent_emails
):
    token = _request_and_get_token(anon_client, sent_emails, "me@example.com")

    weak = anon_client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": "short"}
    )
    assert weak.status_code == 422

    # The token must still be usable — a policy failure shouldn't burn
    # the caller's only link.
    strong = anon_client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": NEW_PASSWORD},
    )
    assert strong.status_code == 204


def test_successful_reset_evicts_every_session_including_the_requester_s_own(
    anon_client, user, sent_emails
):
    _, original_headers = user
    other_login = anon_client.post(
        "/auth/login", json={"email": "me@example.com", "password": GOOD_PASSWORD}
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['access_token']}"}

    token = _request_and_get_token(anon_client, sent_emails, "me@example.com")
    anon_client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": NEW_PASSWORD},
    )

    assert anon_client.get("/me", headers=original_headers).status_code == 401
    assert anon_client.get("/me", headers=other_headers).status_code == 401
