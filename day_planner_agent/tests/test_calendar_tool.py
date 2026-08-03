"""Verifies calendar_tool's orchestration: needs_auth handling, fan-out
across multiple connected accounts, skipping stale ones without failing the
whole request, and merging/sorting events across calendars.

backend_client's own HTTP mechanics aren't re-tested here — day_planner_backend_internal's
own test suite already covers /internal/* extensively. What's tested is that
calendar_tool.py calls it correctly and handles every response shape it can
return.
"""

from day_planner_agent import backend_client, calendar_tool


class FakeEventsResource:
    def __init__(self, items: list[dict]) -> None:
        self._items = items
        self.list_calls: list[dict] = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return self

    def execute(self):
        return {"items": self._items}


class FakeCalendarService:
    def __init__(self, items: list[dict]) -> None:
        self._events = FakeEventsResource(items)

    def events(self):
        return self._events


def _google_item(event_id, summary, start, end, location=None):
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "location": location,
    }


async def test_needs_auth_when_nothing_connected(tool_context, monkeypatch):
    async def list_calendars(user_id):
        raise backend_client.NeedsAuth("https://connect.example/start", "not connected")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)

    result = await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-02")
    assert result == {
        "status": "needs_auth",
        "connect_url": "https://connect.example/start",
        "message": "not connected",
    }


async def test_merges_and_sorts_events_across_accounts(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {"account_id": "acct-personal", "calendar_id": "me@gmail.com"},
                {"account_id": "acct-work", "calendar_id": "me@work.com"},
            ],
        }

    async def access_token(user_id, account_id):
        return f"AT-{account_id}"

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)

    services = {
        "me@gmail.com": FakeCalendarService(
            [_google_item("e2", "Later", "2026-08-01T14:00:00Z", "2026-08-01T15:00:00Z")]
        ),
        "me@work.com": FakeCalendarService(
            [_google_item("e1", "Earlier", "2026-08-01T09:00:00Z", "2026-08-01T10:00:00Z")]
        ),
    }

    def fake_build(service_name, version, credentials):
        # calendar_id isn't passed to build() itself (only to .events().list()
        # later), so route by which access token was used instead — each
        # fake account minted a distinguishable one above.
        is_personal = credentials.token == "AT-acct-personal"
        return services["me@gmail.com" if is_personal else "me@work.com"]

    monkeypatch.setattr(calendar_tool, "build", fake_build)

    result = await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-02")
    assert result["status"] == "success"
    assert [e["event_id"] for e in result["events"]] == ["e1", "e2"]
    assert "note" not in result


async def test_surfaces_needs_reauth_accounts_as_a_note(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": ["acct-broken"],
            "calendars": [{"account_id": "acct-ok", "calendar_id": "me@gmail.com"}],
        }

    async def access_token(user_id, account_id):
        return "AT-1"

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(
        calendar_tool, "build", lambda *a, **k: FakeCalendarService([])
    )

    result = await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-02")
    assert result["status"] == "success"
    assert "need reconnecting" in result["note"]


async def test_skips_account_that_goes_stale_mid_request(tool_context, monkeypatch):
    """/internal/calendars said this account was active, but /internal/access-token
    409s anyway (it went stale in between) — must not fail the whole request."""

    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {"account_id": "acct-ok", "calendar_id": "me@gmail.com"},
                {"account_id": "acct-stale", "calendar_id": "me@work.com"},
            ],
        }

    async def access_token(user_id, account_id):
        return None if account_id == "acct-stale" else "AT-1"

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(
        calendar_tool,
        "build",
        lambda *a, **k: FakeCalendarService(
            [_google_item("e1", "Only one", "2026-08-01T09:00:00Z", "2026-08-01T10:00:00Z")]
        ),
    )

    result = await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-02")
    assert result["status"] == "success"
    assert len(result["events"]) == 1
    assert "went stale mid-request" in result["note"]


async def test_user_id_comes_only_from_tool_context(tool_context, monkeypatch):
    """The whole tenant boundary: get_calendar_events has no user_id
    parameter a model could fill in at all — confirm the call into
    backend_client is keyed on tool_context.session.user_id."""
    seen_user_ids = []

    async def list_calendars(user_id):
        seen_user_ids.append(user_id)
        raise backend_client.NeedsAuth("https://x", "nope")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)

    await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-02")
    assert seen_user_ids == ["user-1"]
    assert "user_id" not in calendar_tool.get_calendar_events.__code__.co_varnames[
        : calendar_tool.get_calendar_events.__code__.co_argcount
    ]
