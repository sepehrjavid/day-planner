"""Coverage of domain_client.py's own request/response mapping — habits,
habit sessions, zones, and the sleep schedule.

Token caching, connection pooling, and 401-retry are
_service_client.ServiceClient's job and are tested once, generically, in
test_service_client.py — this file only covers what's specific to these
endpoints: the right path, the right body/params shape, and the
404 -> None / exists-False mapping each one owns. Every other test file
in this suite monkeypatches these functions wholesale rather than
exercising this module's own internals.
"""

import pytest

from day_planner_agent import domain_client


@pytest.fixture(autouse=True)
def _reset_client_state():
    domain_client._client._token = None
    domain_client._client._token_minted_at = 0.0
    domain_client._client._http_client = None
    yield
    domain_client._client._token = None
    domain_client._client._token_minted_at = 0.0
    domain_client._client._http_client = None


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

    monkeypatch.setattr(domain_client._client, "_mint_id_token", fake_mint)
    monkeypatch.setattr(domain_client._client, "_get_client", fake_get_client)


async def test_create_habit_posts_to_agent_habits(monkeypatch):
    fake_client = FakeHTTPXClient(post_responses=[FakeHTTPXResponse({"habit_id": "h1"})])
    _install_fake_client(monkeypatch, fake_client)

    result = await domain_client.create_habit("user-1", label="Gym", goal="3x/week")

    assert result == {"habit_id": "h1"}
    assert fake_client.post_calls[0]["url"] == "/agent/habits"
    assert fake_client.post_calls[0]["json"] == {
        "user_id": "user-1",
        "label": "Gym",
        "goal": "3x/week",
    }


async def test_list_habits_gets_from_agent_habits(monkeypatch):
    fake_client = FakeHTTPXClient(get_responses=[FakeHTTPXResponse({"habits": [{"habit_id": "h1"}]})])
    _install_fake_client(monkeypatch, fake_client)

    result = await domain_client.list_habits("user-1", status="active")

    assert result == [{"habit_id": "h1"}]
    assert fake_client.get_calls[0]["url"] == "/agent/habits"
    assert fake_client.get_calls[0]["params"] == {"user_id": "user-1", "status": "active"}


async def test_update_habit_returns_none_on_404(monkeypatch):
    fake_client = FakeHTTPXClient(post_responses=[FakeHTTPXResponse({}, status_code=404)])
    _install_fake_client(monkeypatch, fake_client)

    assert await domain_client.update_habit("user-1", "ghost", label="x") is None
    assert fake_client.post_calls[0]["url"] == "/agent/habits/update"


async def test_upsert_habit_session_posts_to_agent_habit_sessions(monkeypatch):
    fake_client = FakeHTTPXClient(post_responses=[FakeHTTPXResponse({"session_id": "s1"})])
    _install_fake_client(monkeypatch, fake_client)

    result = await domain_client.upsert_habit_session(
        "user-1",
        habit_id="h1",
        event_id="e1",
        calendar_id="me@gmail.com",
        planned_start="2026-08-04T07:00:00-07:00",
        planned_end="2026-08-04T07:30:00-07:00",
    )

    assert result == {"session_id": "s1"}
    assert fake_client.post_calls[0]["url"] == "/agent/habit-sessions"


async def test_set_habit_session_status_returns_none_on_404(monkeypatch):
    fake_client = FakeHTTPXClient(post_responses=[FakeHTTPXResponse({}, status_code=404)])
    _install_fake_client(monkeypatch, fake_client)

    result = await domain_client.set_habit_session_status(
        "user-1", calendar_id="me@gmail.com", event_id="never-planned", status="completed"
    )

    assert result is None
    assert fake_client.post_calls[0]["url"] == "/agent/habit-sessions/status"
    assert fake_client.post_calls[0]["json"]["marked_by"] == "agent"


async def test_list_habit_sessions_gets_from_agent_habit_sessions(monkeypatch):
    fake_client = FakeHTTPXClient(get_responses=[FakeHTTPXResponse({"sessions": []})])
    _install_fake_client(monkeypatch, fake_client)

    result = await domain_client.list_habit_sessions(
        "user-1", planned_from="2026-08-01T00:00:00Z", planned_to="2026-08-08T00:00:00Z"
    )

    assert result == []
    assert fake_client.get_calls[0]["url"] == "/agent/habit-sessions"


async def test_create_zone_posts_to_agent_zones(monkeypatch):
    fake_client = FakeHTTPXClient(post_responses=[FakeHTTPXResponse({"zone_id": "z1"})])
    _install_fake_client(monkeypatch, fake_client)

    result = await domain_client.create_zone(
        "user-1", label="Work", start_time="09:00", end_time="17:00", days_of_week=["mon"]
    )

    assert result == {"zone_id": "z1"}
    assert fake_client.post_calls[0]["url"] == "/agent/zones"


async def test_list_zones_gets_from_agent_zones(monkeypatch):
    fake_client = FakeHTTPXClient(get_responses=[FakeHTTPXResponse({"zones": []})])
    _install_fake_client(monkeypatch, fake_client)

    assert await domain_client.list_zones("user-1") == []
    assert fake_client.get_calls[0]["url"] == "/agent/zones"


async def test_update_zone_returns_none_on_404(monkeypatch):
    fake_client = FakeHTTPXClient(post_responses=[FakeHTTPXResponse({}, status_code=404)])
    _install_fake_client(monkeypatch, fake_client)

    assert await domain_client.update_zone("user-1", "ghost", end_time="18:00") is None
    assert fake_client.post_calls[0]["url"] == "/agent/zones/update"


async def test_get_sleep_schedule_returns_none_when_not_configured(monkeypatch):
    fake_client = FakeHTTPXClient(get_responses=[FakeHTTPXResponse({"exists": False, "schedule": None})])
    _install_fake_client(monkeypatch, fake_client)

    assert await domain_client.get_sleep_schedule("user-1") is None
    assert fake_client.get_calls[0]["url"] == "/agent/sleep-schedule"


async def test_get_sleep_schedule_returns_schedule_when_configured(monkeypatch):
    schedule = {"sleep_time": "23:00"}
    fake_client = FakeHTTPXClient(
        get_responses=[FakeHTTPXResponse({"exists": True, "schedule": schedule})]
    )
    _install_fake_client(monkeypatch, fake_client)

    assert await domain_client.get_sleep_schedule("user-1") == schedule


async def test_set_sleep_schedule_posts_to_agent_sleep_schedule(monkeypatch):
    fake_client = FakeHTTPXClient(post_responses=[FakeHTTPXResponse({"sleep_time": "23:00"})])
    _install_fake_client(monkeypatch, fake_client)

    result = await domain_client.set_sleep_schedule("user-1", sleep_time="23:00")

    assert result == {"sleep_time": "23:00"}
    assert fake_client.post_calls[0]["url"] == "/agent/sleep-schedule"
