"""End-to-end coverage of the connect flow, with Firestore, KMS, and Google faked.

These exist mostly to pin down failure modes, not the happy path: a single-use
nonce that stops being single-use, an `invalid_grant` that turns into a 500
instead of a reconnect prompt, or one user reaching another's calendar account
are all silent, and all serious.
"""

from urllib.parse import parse_qs, urlparse

import pytest

from app.core import pkce
from app.providers import AccountIdentity, CalendarRef, NeedsReauth, TokenSet
from app.providers import google as google_provider

from .conftest import GOOD_PASSWORD, REDIRECT_URI

PERSONAL = [
    CalendarRef("me@gmail.com", "Me", is_primary=True),
    CalendarRef("holidays@group.calendar.google.com", "Holidays in Sweden",
                is_primary=False, selected_by_default=False),
]
WORK = [CalendarRef("me@work.com", "Work", is_primary=True)]


@pytest.fixture
def google(monkeypatch):
    """Patch the Google provider so connects can run offline.

    `state` lets a test choose which account the next consent returns, so a
    user can link a personal *and* a work calendar.
    """
    state = {"sub": "sub-personal", "email": "me@gmail.com", "calendars": PERSONAL}

    async def exchange_code(self, *, code, code_verifier, redirect_uri):
        assert redirect_uri == REDIRECT_URI
        return (
            TokenSet(
                refresh_token=f"RT-{state['sub']}",
                access_token="AT-1",
                expires_in=3600,
                scopes=["openid", "email"],
            ),
            AccountIdentity(
                provider_account_id=state["sub"], email=state["email"]
            ),
        )

    async def list_calendars(self, *, access_token):
        return state["calendars"]

    async def refresh(self, *, refresh_token):
        return TokenSet(
            refresh_token=None, access_token="AT-2", expires_in=3599, scopes=["openid"]
        )

    cls = google_provider.GoogleCalendarProvider
    monkeypatch.setattr(cls, "exchange_code", exchange_code)
    monkeypatch.setattr(cls, "list_calendars", list_calendars)
    monkeypatch.setattr(cls, "refresh", refresh)
    return state


def connect(client, headers, *, follow=True):
    """Drive a signed-in user through consent. Returns the callback response."""
    link = client.post(
        "/me/calendar-accounts/connect-link", headers=headers
    ).json()["connect_url"]
    nonce = parse_qs(urlparse(link).query)["s"][0]
    client.get(f"/auth/google/start?s={nonce}", follow_redirects=False)
    return client.get(f"/auth/google/callback?code=CODE&state={nonce}"), nonce


# ---------------------------------------------------------------------------
# Auth gates
# ---------------------------------------------------------------------------


def test_internal_routes_reject_anonymous_callers(anon_client):
    assert (
        anon_client.post("/internal/connect-link", json={"user_id": "u1"}).status_code
        == 401
    )


def test_internal_routes_reject_unverifiable_tokens(anon_client):
    response = anon_client.post(
        "/internal/connect-link",
        json={"user_id": "u1"},
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )
    assert response.status_code == 401


def test_connect_link_requires_a_session(anon_client):
    assert anon_client.post("/me/calendar-accounts/connect-link").status_code == 401


def test_healthz_is_open(anon_client):
    assert anon_client.get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# Authorization request
# ---------------------------------------------------------------------------


def test_connect_link_does_not_leak_user_id(anon_client, user):
    """user_id in the URL would let anyone attach their account to it."""
    user_id, headers = user
    url = anon_client.post(
        "/me/calendar-accounts/connect-link", headers=headers
    ).json()["connect_url"]
    assert user_id not in url


def test_start_requests_offline_access_and_pkce(anon_client, user, store):
    _, headers = user
    url = anon_client.post(
        "/me/calendar-accounts/connect-link", headers=headers
    ).json()["connect_url"]
    nonce = parse_qs(urlparse(url).query)["s"][0]

    response = anon_client.get(f"/auth/google/start?s={nonce}", follow_redirects=False)
    assert response.status_code == 302
    params = parse_qs(urlparse(response.headers["location"]).query)

    # Without offline access there's no refresh token, and the scheduled
    # morning briefing can never run.
    assert params["access_type"] == ["offline"]
    # Without forcing consent, a reconnect returns no refresh token at all.
    assert params["prompt"] == ["consent"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == [nonce]
    assert params["code_challenge"] == [
        pkce.code_challenge_for(store.states[nonce].code_verifier)
    ]
    # The write scope is requested up front so add_calendar_event doesn't need
    # a second consent screen mid-conversation.
    assert "https://www.googleapis.com/auth/calendar.events" in params["scope"][0]


def test_start_is_idempotent(anon_client, user):
    """Clicking the link twice before consenting must not kill it."""
    _, headers = user
    url = anon_client.post(
        "/me/calendar-accounts/connect-link", headers=headers
    ).json()["connect_url"]
    nonce = parse_qs(urlparse(url).query)["s"][0]
    for _ in range(2):
        assert (
            anon_client.get(
                f"/auth/google/start?s={nonce}", follow_redirects=False
            ).status_code
            == 302
        )


def test_start_rejects_unknown_nonce(anon_client):
    assert (
        anon_client.get("/auth/google/start?s=made-up", follow_redirects=False).status_code
        == 400
    )


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------


def test_declined_consent_is_not_a_crash(anon_client):
    assert anon_client.get("/auth/google/callback?error=access_denied").status_code == 400


def test_callback_stores_account_and_calendars(anon_client, user, fake_crypto, google):
    user_id, headers = user
    response, _ = connect(anon_client, headers)
    assert response.status_code == 200

    accounts = anon_client.get("/me", headers=headers).json()["accounts"]
    assert len(accounts) == 1
    account = accounts[0]
    assert account["email"] == "me@gmail.com"
    assert account["status"] == "active"

    # Both calendars are recorded, but the subscribed holiday feed is off by
    # default — it's noise in a day plan.
    by_id = {c["calendar_id"]: c for c in account["calendars"]}
    assert by_id["me@gmail.com"]["selected"] is True
    assert by_id["holidays@group.calendar.google.com"]["selected"] is False


def test_state_is_single_use(anon_client, user, fake_crypto, google):
    """A replayed callback must not be able to re-bind an account."""
    _, headers = user
    _, nonce = connect(anon_client, headers)
    assert (
        anon_client.get(f"/auth/google/callback?code=CODE&state={nonce}").status_code
        == 400
    )


def test_missing_refresh_token_fails_loudly(anon_client, user, monkeypatch, fake_crypto, google):
    """A connection with no refresh token can never serve the 07:00 briefing,
    so it must not be stored as if it succeeded."""

    async def no_refresh_token(self, *, code, code_verifier, redirect_uri):
        return (
            TokenSet(refresh_token=None, access_token="AT", expires_in=3600, scopes=[]),
            AccountIdentity(provider_account_id="sub-1", email="me@gmail.com"),
        )

    monkeypatch.setattr(
        google_provider.GoogleCalendarProvider, "exchange_code", no_refresh_token
    )
    _, headers = user
    response, _ = connect(anon_client, headers)
    assert response.status_code == 400
    assert anon_client.get("/me", headers=headers).json()["accounts"] == []


# ---------------------------------------------------------------------------
# Multiple calendar accounts
# ---------------------------------------------------------------------------


def test_user_can_link_personal_and_work_accounts(anon_client, user, fake_crypto, google):
    _, headers = user
    connect(anon_client, headers)

    google.update(sub="sub-work", email="me@work.com", calendars=WORK)
    connect(anon_client, headers)

    accounts = anon_client.get("/me", headers=headers).json()["accounts"]
    assert {a["email"] for a in accounts} == {"me@gmail.com", "me@work.com"}


def test_reconnecting_the_same_account_does_not_duplicate(
    anon_client, user, fake_crypto, google
):
    """The document ID is derived from (provider, sub), so re-consenting heals
    the existing account instead of growing a second one."""
    _, headers = user
    connect(anon_client, headers)
    connect(anon_client, headers)
    assert len(anon_client.get("/me", headers=headers).json()["accounts"]) == 1


def test_reconnect_preserves_calendar_selection(anon_client, user, fake_crypto, google):
    """A user's choice about which calendars matter shouldn't be silently
    reset because a refresh token expired."""
    _, headers = user
    connect(anon_client, headers)
    account_id = anon_client.get("/me", headers=headers).json()["accounts"][0][
        "account_id"
    ]

    anon_client.patch(
        f"/me/calendar-accounts/{account_id}/calendars",
        headers=headers,
        json={"selected_calendar_ids": ["holidays@group.calendar.google.com"]},
    )
    connect(anon_client, headers)

    by_id = {
        c["calendar_id"]: c
        for c in anon_client.get("/me", headers=headers).json()["accounts"][0][
            "calendars"
        ]
    }
    assert by_id["holidays@group.calendar.google.com"]["selected"] is True
    assert by_id["me@gmail.com"]["selected"] is False


def test_internal_calendars_spans_every_account(anon_client, client, user, fake_crypto, google):
    """What makes multi-calendar planning work: one call, a flat list across
    personal and work."""
    user_id, headers = user
    connect(anon_client, headers)
    google.update(sub="sub-work", email="me@work.com", calendars=WORK)
    connect(anon_client, headers)

    body = client.get(f"/internal/calendars?user_id={user_id}").json()
    assert body["connected"] is True
    # Only selected calendars — the holiday feed stays out.
    assert {c["calendar_id"] for c in body["calendars"]} == {
        "me@gmail.com",
        "me@work.com",
    }
    # Each target carries the account it belongs to, so the agent knows which
    # access token to fetch for it.
    assert len({c["account_id"] for c in body["calendars"]}) == 2


def test_internal_calendars_surfaces_broken_accounts(
    anon_client, client, user, fake_crypto, google, monkeypatch
):
    """A work calendar that quietly stopped refreshing should be reported, not
    silently planned around."""
    user_id, headers = user
    connect(anon_client, headers)
    account_id = anon_client.get("/me", headers=headers).json()["accounts"][0][
        "account_id"
    ]

    async def revoked(self, *, refresh_token):
        raise NeedsReauth("revoked")

    monkeypatch.setattr(google_provider.GoogleCalendarProvider, "refresh", revoked)
    client.post("/internal/access-token", json={"user_id": user_id})

    body = client.get(f"/internal/calendars?user_id={user_id}").json()
    assert body["needs_reauth"] == [account_id]
    assert body["calendars"] == []


def test_calendar_selection_is_scoped_to_the_owner(anon_client, user, fake_crypto, google):
    """A logged-in user must not be able to touch another user's account by id."""
    _, headers = user
    connect(anon_client, headers)
    victim_account = anon_client.get("/me", headers=headers).json()["accounts"][0][
        "account_id"
    ]

    other = anon_client.post(
        "/auth/signup",
        json={"email": "attacker@example.com", "password": GOOD_PASSWORD},
    ).json()
    attacker = {"Authorization": f"Bearer {other['access_token']}"}

    response = anon_client.patch(
        f"/me/calendar-accounts/{victim_account}/calendars",
        headers=attacker,
        json={"selected_calendar_ids": []},
    )
    assert response.status_code == 404

    response = anon_client.delete(
        f"/me/calendar-accounts/{victim_account}", headers=attacker
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Token minting
# ---------------------------------------------------------------------------


def test_access_token_defaults_to_the_users_first_account(
    anon_client, client, user, fake_crypto, google
):
    user_id, headers = user
    connect(anon_client, headers)

    response = client.post("/internal/access-token", json={"user_id": user_id})
    assert response.status_code == 200
    assert response.json()["access_token"] == "AT-2"


def test_access_token_can_target_a_specific_account(
    anon_client, client, user, fake_crypto, google
):
    user_id, headers = user
    connect(anon_client, headers)
    google.update(sub="sub-work", email="me@work.com", calendars=WORK)
    connect(anon_client, headers)

    work = next(
        a
        for a in anon_client.get("/me", headers=headers).json()["accounts"]
        if a["email"] == "me@work.com"
    )
    response = client.post(
        "/internal/access-token",
        json={"user_id": user_id, "account_id": work["account_id"]},
    )
    assert response.status_code == 200
    assert response.json()["account_id"] == work["account_id"]


def test_unconnected_user_gets_409_not_500(client, user, fake_crypto):
    user_id, _ = user
    response = client.post("/internal/access-token", json={"user_id": user_id})
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "not_connected"


def test_dead_grant_becomes_needs_reauth(
    anon_client, client, user, store, fake_crypto, google, monkeypatch
):
    """invalid_grant is a normal state — user revoked access, six months idle,
    a password change. It must degrade into a reconnect prompt, not a 500."""
    user_id, headers = user
    connect(anon_client, headers)

    async def revoked(self, *, refresh_token):
        raise NeedsReauth("revoked")

    monkeypatch.setattr(google_provider.GoogleCalendarProvider, "refresh", revoked)

    response = client.post("/internal/access-token", json={"user_id": user_id})
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "needs_reauth"

    account = anon_client.get("/me", headers=headers).json()["accounts"][0]
    assert account["status"] == "needs_reauth"
    # The credential is known-useless; holding onto it is pure liability.
    stored = store.accounts[user_id][account["account_id"]]
    assert stored["encrypted_refresh_token"] is None


def test_rotated_refresh_token_is_persisted(
    anon_client, client, user, store, fake_crypto, google, monkeypatch
):
    """Google usually omits refresh_token on refresh, but rotates it in some
    flows. Dropping the new one leaves us using a dead credential."""
    user_id, headers = user
    connect(anon_client, headers)

    async def rotating(self, *, refresh_token):
        return TokenSet(
            refresh_token="RT-rotated", access_token="AT-3", expires_in=3600, scopes=[]
        )

    monkeypatch.setattr(google_provider.GoogleCalendarProvider, "refresh", rotating)
    assert (
        client.post("/internal/access-token", json={"user_id": user_id}).status_code
        == 200
    )

    account = next(iter(store.accounts[user_id].values()))
    assert account["encrypted_refresh_token"] == f"enc(RT-rotated|{user_id})"


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------


def test_disconnect_revokes_at_the_provider(
    anon_client, user, fake_crypto, google, monkeypatch
):
    user_id, headers = user
    connect(anon_client, headers)
    account_id = anon_client.get("/me", headers=headers).json()["accounts"][0][
        "account_id"
    ]

    revoked: list[str] = []

    async def revoke(self, *, refresh_token):
        revoked.append(refresh_token)

    monkeypatch.setattr(google_provider.GoogleCalendarProvider, "revoke", revoke)

    assert (
        anon_client.delete(
            f"/me/calendar-accounts/{account_id}", headers=headers
        ).status_code
        == 204
    )
    assert revoked == ["RT-sub-personal"], "grant must be revoked at Google too"
    assert anon_client.get("/me", headers=headers).json()["accounts"] == []


def test_deleting_the_default_account_promotes_another(
    anon_client, user, fake_crypto, google
):
    """Otherwise default_account_id dangles at a deleted id and every
    account-less token request 409s despite a live work calendar."""
    _, headers = user
    connect(anon_client, headers)
    google.update(sub="sub-work", email="me@work.com", calendars=WORK)
    connect(anon_client, headers)

    body = anon_client.get("/me", headers=headers).json()
    default_id = body["default_account_id"]
    anon_client.delete(f"/me/calendar-accounts/{default_id}", headers=headers)

    after = anon_client.get("/me", headers=headers).json()
    assert after["default_account_id"] is not None
    assert after["default_account_id"] != default_id
