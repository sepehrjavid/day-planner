"""Coverage of backend_client.py's token caching and shared HTTP client
(A2.1). Every other test file in this suite monkeypatches backend_client's
public functions wholesale and never exercises this machinery — this file
is the only one that does.

Module-level state (_token, _token_minted_at, _http_client) is reset
before and after every test via the autouse fixture below, since it would
otherwise leak between tests in the same process.
"""

import time

import pytest

from day_planner_agent import backend_client


@pytest.fixture(autouse=True)
def _reset_module_state():
    backend_client._token = None
    backend_client._token_minted_at = 0.0
    backend_client._http_client = None
    yield
    backend_client._token = None
    backend_client._token_minted_at = 0.0
    backend_client._http_client = None


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
    """Deliberately has no __aenter__/__aexit__ — A2.1's whole point is
    that call sites stop doing `async with await _client() as client`,
    which would close a shared client after the first use. If any call
    site regressed to that pattern, this fake would raise AttributeError
    rather than silently working."""

    def __init__(self):
        self.get_calls: list[dict] = []
        self.post_calls: list[dict] = []
        self.closed = False

    async def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        return FakeHTTPXResponse({"zones": []})

    async def post(self, url, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        return FakeHTTPXResponse({"zone_id": "z1"})

    async def aclose(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Token caching
# ---------------------------------------------------------------------------


async def test_token_minted_once_across_many_sequential_calls(monkeypatch):
    mint_calls = []

    async def fake_mint():
        mint_calls.append(1)
        return f"token-{len(mint_calls)}"

    monkeypatch.setattr(backend_client, "_mint_id_token", fake_mint)

    tokens = [await backend_client._get_id_token() for _ in range(10)]

    assert len(mint_calls) == 1
    assert len(set(tokens)) == 1


async def test_token_re_minted_after_expiry(monkeypatch):
    mint_calls = []

    async def fake_mint():
        mint_calls.append(1)
        return f"token-{len(mint_calls)}"

    monkeypatch.setattr(backend_client, "_mint_id_token", fake_mint)

    first = await backend_client._get_id_token()
    assert len(mint_calls) == 1

    # Simulate the cached token having been minted just past its TTL.
    backend_client._token_minted_at = (
        time.monotonic() - backend_client._TOKEN_TTL_SECONDS - 1
    )

    second = await backend_client._get_id_token()
    assert len(mint_calls) == 2
    assert second != first


async def test_token_not_yet_expired_is_not_re_minted(monkeypatch):
    mint_calls = []

    async def fake_mint():
        mint_calls.append(1)
        return "token"

    monkeypatch.setattr(backend_client, "_mint_id_token", fake_mint)

    await backend_client._get_id_token()
    # Well inside the TTL window.
    backend_client._token_minted_at = time.monotonic() - 60

    await backend_client._get_id_token()
    assert len(mint_calls) == 1


async def test_auth_headers_carries_the_current_token(monkeypatch):
    async def fake_mint():
        return "abc123"

    monkeypatch.setattr(backend_client, "_mint_id_token", fake_mint)

    headers = await backend_client._auth_headers()
    assert headers == {"Authorization": "Bearer abc123"}


# ---------------------------------------------------------------------------
# Shared HTTP client
# ---------------------------------------------------------------------------


async def test_client_is_not_created_until_first_use():
    assert backend_client._http_client is None


async def test_client_instance_is_reused_across_calls():
    first = await backend_client._get_client()
    second = await backend_client._get_client()
    assert first is second


async def test_client_survives_multiple_backend_calls_without_closing(monkeypatch):
    """The literal A2.1 regression this exists to catch: `async with
    await _client() as client` would close the shared client after the
    very first call, and a second call site using it would raise
    `RuntimeError: client has been closed`. FakeHTTPXClient has no
    __aenter__/__aexit__ at all, so a reintroduced `async with` here would
    fail loudly instead of silently passing."""

    async def fake_mint():
        return "tok"

    fake_client = FakeHTTPXClient()

    async def fake_get_client():
        return fake_client

    monkeypatch.setattr(backend_client, "_mint_id_token", fake_mint)
    monkeypatch.setattr(backend_client, "_get_client", fake_get_client)

    await backend_client.list_zones("user-1")
    await backend_client.create_zone(
        "user-1", label="Work", start_time="09:00", end_time="17:00", days_of_week=["mon"]
    )

    assert fake_client.closed is False
    assert len(fake_client.get_calls) == 1
    assert len(fake_client.post_calls) == 1


async def test_authorization_header_is_per_request_not_baked_into_client(monkeypatch):
    """The client itself must carry no Authorization header of its own —
    only individual requests do — since the client is long-lived but the
    token rotates roughly every 55 minutes."""
    mint_count = []

    async def fake_mint():
        mint_count.append(1)
        return f"tok-{len(mint_count)}"

    fake_client = FakeHTTPXClient()

    async def fake_get_client():
        return fake_client

    monkeypatch.setattr(backend_client, "_mint_id_token", fake_mint)
    monkeypatch.setattr(backend_client, "_get_client", fake_get_client)

    await backend_client.list_zones("user-1")
    # Force a re-mint before the second call, simulating token rotation
    # mid-session — the *client* must be unaffected either way.
    backend_client._token_minted_at = (
        time.monotonic() - backend_client._TOKEN_TTL_SECONDS - 1
    )
    await backend_client.list_zones("user-1")

    headers_seen = [call["headers"]["Authorization"] for call in fake_client.get_calls]
    assert headers_seen == ["Bearer tok-1", "Bearer tok-2"]
