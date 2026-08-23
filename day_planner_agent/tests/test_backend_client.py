"""Coverage of backend_client.py's own request/response mapping —
connect_link, list_calendars (+NeedsAuth), access_token (+409 -> None).

Token caching, connection pooling, and 401-retry are _service_client.
ServiceClient's job now (A6.2) and are tested once, generically, in
test_service_client.py — this file only covers what's specific to these
three credential endpoints. Every other test file in this suite
monkeypatches these functions wholesale rather than exercising this
module's own internals, so this is the only place that does.
"""

import pytest

from day_planner_agent import backend_client


@pytest.fixture(autouse=True)
def _reset_client_state():
    backend_client._client._token = None
    backend_client._client._token_minted_at = 0.0
    backend_client._client._http_client = None
    yield
    backend_client._client._token = None
    backend_client._client._token_minted_at = 0.0
    backend_client._client._http_client = None


class FakeHTTPXResponse:
    def __init__(self, json_body: dict, status_code: int = 200):
        self._json_body = json_body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_body


class FakeHTTPXClient:
    def __init__(self, get_responses=None, post_responses=None):
        self.get_calls: list[dict] = []
        self.post_calls: list[dict] = []
        self._get_responses = list(get_responses) if get_responses is not None else None
        self._post_responses = list(post_responses) if post_responses is not None else None

    async def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        return self._get_responses.pop(0)

    async def post(self, url, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        return self._post_responses.pop(0)


def _install_fake_client(monkeypatch, fake_client):
    async def fake_mint():
        return "tok"

    async def fake_get_client():
        return fake_client

    monkeypatch.setattr(backend_client._client, "_mint_id_token", fake_mint)
    monkeypatch.setattr(backend_client._client, "_get_client", fake_get_client)


async def test_connect_link_posts_and_returns_url(monkeypatch):
    fake_client = FakeHTTPXClient(
        post_responses=[FakeHTTPXResponse({"connect_url": "https://connect.example/start"})]
    )
    _install_fake_client(monkeypatch, fake_client)

    url = await backend_client.connect_link("user-1", provider="google")

    assert url == "https://connect.example/start"
    assert fake_client.post_calls[0]["url"] == "/internal/connect-link"
    assert fake_client.post_calls[0]["json"] == {"user_id": "user-1", "provider": "google"}


async def test_list_calendars_returns_body_when_connected(monkeypatch):
    body = {"connected": True, "needs_reauth": [], "calendars": [{"calendar_id": "me@gmail.com"}]}
    fake_client = FakeHTTPXClient(get_responses=[FakeHTTPXResponse(body)])
    _install_fake_client(monkeypatch, fake_client)

    result = await backend_client.list_calendars("user-1")

    assert result == body
    assert fake_client.get_calls[0]["url"] == "/internal/calendars"
    assert fake_client.get_calls[0]["params"] == {"user_id": "user-1"}


async def test_list_calendars_raises_needs_auth_when_not_connected(monkeypatch):
    fake_client = FakeHTTPXClient(
        get_responses=[FakeHTTPXResponse({"connected": False, "needs_reauth": [], "calendars": []})],
        post_responses=[FakeHTTPXResponse({"connect_url": "https://connect.example/start"})],
    )
    _install_fake_client(monkeypatch, fake_client)

    with pytest.raises(backend_client.NeedsAuth) as exc_info:
        await backend_client.list_calendars("user-1")

    assert exc_info.value.connect_url == "https://connect.example/start"


async def test_access_token_returns_token(monkeypatch):
    fake_client = FakeHTTPXClient(
        post_responses=[
            FakeHTTPXResponse(
                {"account_id": "a1", "access_token": "AT-1", "expires_at": "x", "scopes": []}
            )
        ]
    )
    _install_fake_client(monkeypatch, fake_client)

    token = await backend_client.access_token("user-1", "a1")

    assert token == "AT-1"
    assert fake_client.post_calls[0]["url"] == "/internal/access-token"
    assert fake_client.post_calls[0]["json"] == {"user_id": "user-1", "account_id": "a1"}


async def test_access_token_returns_none_on_409(monkeypatch):
    fake_client = FakeHTTPXClient(post_responses=[FakeHTTPXResponse({}, status_code=409)])
    _install_fake_client(monkeypatch, fake_client)

    assert await backend_client.access_token("user-1", "a1") is None
