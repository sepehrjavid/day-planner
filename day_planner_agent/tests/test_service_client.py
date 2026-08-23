"""Coverage of _service_client.ServiceClient's token caching and shared
HTTP client (A2.1), extracted from backend_client.py into its own class
by A6.2 so backend_client.py and domain_client.py can each build one
without duplicating the concurrency-sensitive caching/pooling logic.
Every domain-specific test file (test_zone_tools.py, test_habit_tools.py,
etc.) monkeypatches backend_client's/domain_client's public functions
wholesale and never exercises this machinery — this file is the only
one that does, against a throwaway ServiceClient instance rather than
any real client module, so nothing here depends on which service ends
up calling it.
"""

import time

import pytest

from day_planner_agent import _service_client


@pytest.fixture
def client() -> _service_client.ServiceClient:
    return _service_client.ServiceClient("https://fake.example")


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
    rather than silently working.

    get_responses/post_responses (A2.3), when given, override the default
    fixed success response: each successive call pops the next response
    off the list, so a test can script "401 then success" or "401 then
    401" for get/post's retry-once-on-401 logic."""

    def __init__(self, get_responses=None, post_responses=None):
        self.get_calls: list[dict] = []
        self.post_calls: list[dict] = []
        self.closed = False
        self._get_responses = list(get_responses) if get_responses is not None else None
        self._post_responses = list(post_responses) if post_responses is not None else None

    async def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        if self._get_responses is not None:
            return self._get_responses.pop(0)
        return FakeHTTPXResponse({"ok": True})

    async def post(self, url, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        if self._post_responses is not None:
            return self._post_responses.pop(0)
        return FakeHTTPXResponse({"ok": True})

    async def aclose(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Token caching
# ---------------------------------------------------------------------------


async def test_token_minted_once_across_many_sequential_calls(client, monkeypatch):
    mint_calls = []

    async def fake_mint():
        mint_calls.append(1)
        return f"token-{len(mint_calls)}"

    monkeypatch.setattr(client, "_mint_id_token", fake_mint)

    tokens = [await client._get_id_token() for _ in range(10)]

    assert len(mint_calls) == 1
    assert len(set(tokens)) == 1


async def test_token_re_minted_after_expiry(client, monkeypatch):
    mint_calls = []

    async def fake_mint():
        mint_calls.append(1)
        return f"token-{len(mint_calls)}"

    monkeypatch.setattr(client, "_mint_id_token", fake_mint)

    first = await client._get_id_token()
    assert len(mint_calls) == 1

    # Simulate the cached token having been minted just past its TTL.
    client._token_minted_at = time.monotonic() - _service_client._TOKEN_TTL_SECONDS - 1

    second = await client._get_id_token()
    assert len(mint_calls) == 2
    assert second != first


async def test_token_not_yet_expired_is_not_re_minted(client, monkeypatch):
    mint_calls = []

    async def fake_mint():
        mint_calls.append(1)
        return "token"

    monkeypatch.setattr(client, "_mint_id_token", fake_mint)

    await client._get_id_token()
    # Well inside the TTL window.
    client._token_minted_at = time.monotonic() - 60

    await client._get_id_token()
    assert len(mint_calls) == 1


async def test_auth_headers_carries_the_current_token(client, monkeypatch):
    async def fake_mint():
        return "abc123"

    monkeypatch.setattr(client, "_mint_id_token", fake_mint)

    headers = await client._auth_headers()
    assert headers == {"Authorization": "Bearer abc123"}


# ---------------------------------------------------------------------------
# Shared HTTP client
# ---------------------------------------------------------------------------


async def test_client_is_not_created_until_first_use(client):
    assert client._http_client is None


async def test_client_instance_is_reused_across_calls(client):
    first = await client._get_client()
    second = await client._get_client()
    assert first is second


async def test_client_survives_multiple_backend_calls_without_closing(client, monkeypatch):
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

    monkeypatch.setattr(client, "_mint_id_token", fake_mint)
    monkeypatch.setattr(client, "_get_client", fake_get_client)

    await client.get("/foo")
    await client.post("/bar", json={})

    assert fake_client.closed is False
    assert len(fake_client.get_calls) == 1
    assert len(fake_client.post_calls) == 1


async def test_authorization_header_is_per_request_not_baked_into_client(client, monkeypatch):
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

    monkeypatch.setattr(client, "_mint_id_token", fake_mint)
    monkeypatch.setattr(client, "_get_client", fake_get_client)

    await client.get("/foo")
    # Force a re-mint before the second call, simulating token rotation
    # mid-session — the *client* must be unaffected either way.
    client._token_minted_at = time.monotonic() - _service_client._TOKEN_TTL_SECONDS - 1
    await client.get("/foo")

    headers_seen = [call["headers"]["Authorization"] for call in fake_client.get_calls]
    assert headers_seen == ["Bearer tok-1", "Bearer tok-2"]


# ---------------------------------------------------------------------------
# A2.3 — retry-once on 401 (a cached token gone stale before its TTL)
# ---------------------------------------------------------------------------


async def test_401_triggers_exactly_one_retry_with_a_freshly_minted_token(client, monkeypatch):
    mint_calls = []

    async def fake_mint():
        mint_calls.append(1)
        return f"token-{len(mint_calls)}"

    fake_client = FakeHTTPXClient(
        get_responses=[
            FakeHTTPXResponse({}, status_code=401),
            FakeHTTPXResponse({"ok": True}, status_code=200),
        ]
    )

    async def fake_get_client():
        return fake_client

    monkeypatch.setattr(client, "_mint_id_token", fake_mint)
    monkeypatch.setattr(client, "_get_client", fake_get_client)

    response = await client.get("/foo")

    assert response.json() == {"ok": True}
    assert len(mint_calls) == 2  # the original mint, plus one re-mint after the 401
    headers_seen = [call["headers"]["Authorization"] for call in fake_client.get_calls]
    assert headers_seen == ["Bearer token-1", "Bearer token-2"]


async def test_second_consecutive_401_is_not_retried_again(client, monkeypatch):
    """A second 401 right after the re-mint is a real auth failure, not a
    stale cache — retrying it again would loop."""

    async def fake_mint():
        return "tok"

    fake_client = FakeHTTPXClient(
        get_responses=[
            FakeHTTPXResponse({}, status_code=401),
            FakeHTTPXResponse({}, status_code=401),
        ]
    )

    async def fake_get_client():
        return fake_client

    monkeypatch.setattr(client, "_mint_id_token", fake_mint)
    monkeypatch.setattr(client, "_get_client", fake_get_client)

    response = await client.get("/foo")

    assert response.status_code == 401
    assert len(fake_client.get_calls) == 2


async def test_401_retry_clears_the_instance_cached_token(client, monkeypatch):
    """Not just this call's retry — the next unrelated call must also see
    a cleared cache rather than reusing the token that just got a 401."""
    mint_calls = []

    async def fake_mint():
        mint_calls.append(1)
        return f"token-{len(mint_calls)}"

    fake_client = FakeHTTPXClient(
        get_responses=[
            FakeHTTPXResponse({}, status_code=401),
            FakeHTTPXResponse({"ok": True}, status_code=200),
        ]
    )

    async def fake_get_client():
        return fake_client

    monkeypatch.setattr(client, "_mint_id_token", fake_mint)
    monkeypatch.setattr(client, "_get_client", fake_get_client)

    await client.get("/foo")

    assert client._token == "token-2"


async def test_401_retry_applies_to_writes_too(client, monkeypatch):
    """Both /internal/* and /agent/* gate at the router level, ahead of
    any handler body — a 401 means nothing was processed, so retrying a
    write after a fresh token is exactly as safe as retrying a read."""

    async def fake_mint():
        return "tok"

    fake_client = FakeHTTPXClient(
        post_responses=[
            FakeHTTPXResponse({}, status_code=401),
            FakeHTTPXResponse({"ok": True}, status_code=200),
        ]
    )

    async def fake_get_client():
        return fake_client

    monkeypatch.setattr(client, "_mint_id_token", fake_mint)
    monkeypatch.setattr(client, "_get_client", fake_get_client)

    response = await client.post("/foo", json={})

    assert response.json() == {"ok": True}
    assert len(fake_client.post_calls) == 2


# ---------------------------------------------------------------------------
# Isolation between instances — the whole point of extracting this class
# ---------------------------------------------------------------------------


async def test_two_instances_do_not_share_a_token_cache(monkeypatch):
    a = _service_client.ServiceClient("https://a.example")
    b = _service_client.ServiceClient("https://b.example")

    async def mint_for_a():
        return "token-a"

    async def mint_for_b():
        return "token-b"

    monkeypatch.setattr(a, "_mint_id_token", mint_for_a)
    monkeypatch.setattr(b, "_mint_id_token", mint_for_b)

    assert await a._get_id_token() == "token-a"
    assert await b._get_id_token() == "token-b"


async def test_two_instances_do_not_share_an_http_client(monkeypatch):
    a = _service_client.ServiceClient("https://a.example")
    b = _service_client.ServiceClient("https://b.example")

    client_a = await a._get_client()
    client_b = await b._get_client()

    assert client_a is not client_b
    assert client_a.base_url == "https://a.example"
    assert client_b.base_url == "https://b.example"
