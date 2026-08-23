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

