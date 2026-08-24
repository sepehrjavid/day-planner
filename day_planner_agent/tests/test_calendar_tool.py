"""Verifies calendar_tool's orchestration: needs_auth handling, fan-out
across multiple connected accounts, skipping stale ones without failing the
whole request, and merging/sorting events across calendars.

backend_client's/domain_client's own HTTP mechanics aren't re-tested here —
day_planner_backend_internal's and day_planner_backend_app's own test
suites already cover their respective routes extensively. What's tested
is that calendar_tool.py calls them correctly and handles every response
shape they can return. _log_habit_session goes through domain_client
(A6.2), not backend_client, since habit sessions moved to
day_planner_backend_app in A6.1 — everything else calendar_tool.py calls
(list_calendars, access_token) stays on backend_client.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from googleapiclient.errors import HttpError

from day_planner_agent import backend_client, calendar_tool, domain_client


class FakeEventsResource:
    """`inserted` doubles as the fixed response for both insert() and
    patch() — no test in this file exercises both on the same fake.

    `pages`, when given, overrides `items`: each call to list().execute()
    returns the next raw response dict in order (so a test can hand back a
    `nextPageToken` and assert the follow-up call carries it), instead of
    always returning the same single-page `{"items": items}` response.

    `insert_effects`/`get_effects` (A2.3), when given, override `inserted`
    for insert()/get(): each successive .execute() pops the next item —
    an Exception instance is raised, anything else is returned — so a test
    can script "503 then success" or "409 then a tombstoned get" without a
    real Google Calendar retry loop."""

    def __init__(
        self,
        items: list[dict] | None = None,
        pages: list[dict] | None = None,
        inserted: dict | None = None,
        patch_error: Exception | None = None,
        insert_effects: list | None = None,
        get_effects: list | None = None,
    ) -> None:
        self._items = items or []
        self._pages = pages
        self._inserted = inserted
        self._patch_error = patch_error
        self._insert_effects = list(insert_effects) if insert_effects is not None else None
        self._get_effects = list(get_effects) if get_effects is not None else None
        self.list_calls: list[dict] = []
        self.insert_calls: list[dict] = []
        self.patch_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self._pending: str | None = None

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        self._pending = "list"
        return self

    def insert(self, **kwargs):
        self.insert_calls.append(kwargs)
        self._pending = "insert"
        return self

    def patch(self, **kwargs):
        self.patch_calls.append(kwargs)
        self._pending = "patch"
        return self

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        self._pending = "get"
        return self

    def execute(self):
        if self._pending == "get":
            effect = self._get_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        if self._pending == "patch":
            if self._patch_error is not None:
                raise self._patch_error
            return self._inserted
        if self._pending == "insert":
            if self._insert_effects is not None:
                effect = self._insert_effects.pop(0)
                if isinstance(effect, Exception):
                    raise effect
                return effect
            return self._inserted
        if self._pages is not None:
            return self._pages[len(self.list_calls) - 1]
        return {"items": self._items}


class FakeCalendarListResource:
    """calendarList().get(calendarId=...) — the only place accessRole (and,
    conveniently, timeZone) comes back, keyed by calendar_id per test."""

    def __init__(self, entries: dict[str, dict]) -> None:
        self._entries = entries
        self.get_calls: list[dict] = []
        self._current: dict = {}

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        self._current = self._entries[kwargs["calendarId"]]
        return self

    def execute(self):
        return self._current


class FakeCalendarService:
    def __init__(
        self,
        items: list[dict] | None = None,
        inserted: dict | None = None,
        calendar_list_entries: dict[str, dict] | None = None,
        patch_error: Exception | None = None,
        insert_effects: list | None = None,
        get_effects: list | None = None,
    ) -> None:
        self._events = FakeEventsResource(
            items=items or [],
            inserted=inserted,
            patch_error=patch_error,
            insert_effects=insert_effects,
            get_effects=get_effects,
        )
        self._calendar_list = FakeCalendarListResource(calendar_list_entries or {})

    def events(self):
        return self._events

    def calendarList(self):
        return self._calendar_list


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


def _google_item_with_habit_tag(event_id, summary, start, end, habit_id):
    item = _google_item(event_id, summary, start, end)
    item["extendedProperties"] = {"private": {"day_planner_habit_id": habit_id}}
    return item


async def test_get_calendar_events_surfaces_habit_id_only_when_tagged(
    tool_context, monkeypatch
):
    """This is the field instruction.md relies on to notice an
    already-scheduled habit session colliding with a newly-stated
    preference — a plain event must not carry the key at all."""

    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [{"account_id": "acct-personal", "calendar_id": "me@gmail.com"}],
        }

    async def access_token(user_id, account_id):
        return "AT-1"

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(
        calendar_tool,
        "build",
        lambda *a, **k: FakeCalendarService(
            [
                _google_item_with_habit_tag(
                    "e1", "Gym", "2026-08-04T07:00:00Z", "2026-08-04T07:30:00Z", "h1"
                ),
                _google_item("e2", "Dentist", "2026-08-04T09:00:00Z", "2026-08-04T09:30:00Z"),
            ]
        ),
    )

    result = await calendar_tool.get_calendar_events(tool_context, "2026-08-04", "2026-08-05")
    assert result["status"] == "success"
    by_id = {e["event_id"]: e for e in result["events"]}
    assert by_id["e1"]["habit_id"] == "h1"
    assert "habit_id" not in by_id["e2"]


def _single_calendar_service(user_id_calendars):
    async def list_calendars(user_id):
        return user_id_calendars

    async def access_token(user_id, account_id):
        return "AT-1"

    return list_calendars, access_token


async def test_fetches_all_pages_of_events(tool_context, monkeypatch):
    """Google caps events().list at 250 items per page — a busy month can
    span multiple pages, and every page must be collected, not just the
    first (this was the actual A0.1 bug: nextPageToken was ignored)."""
    list_calendars, access_token = _single_calendar_service(
        {
            "connected": True,
            "needs_reauth": [],
            "calendars": [{"account_id": "acct-1", "calendar_id": "me@gmail.com"}],
        }
    )
    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)

    service = FakeCalendarService()
    service._events._pages = [
        {
            "items": [_google_item("e1", "Page one", "2026-08-01T09:00:00Z", "2026-08-01T10:00:00Z")],
            "nextPageToken": "tok2",
        },
        {
            "items": [_google_item("e2", "Page two", "2026-08-02T09:00:00Z", "2026-08-02T10:00:00Z")],
        },
    ]
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-03")

    assert result["status"] == "success"
    assert [e["event_id"] for e in result["events"]] == ["e1", "e2"]
    list_calls = service.events().list_calls
    assert "pageToken" not in list_calls[0]
    assert list_calls[1]["pageToken"] == "tok2"


async def test_single_page_with_no_next_token_stops_after_one_call(tool_context, monkeypatch):
    list_calendars, access_token = _single_calendar_service(
        {
            "connected": True,
            "needs_reauth": [],
            "calendars": [{"account_id": "acct-1", "calendar_id": "me@gmail.com"}],
        }
    )
    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)

    service = FakeCalendarService(
        [_google_item("e1", "Only page", "2026-08-01T09:00:00Z", "2026-08-01T10:00:00Z")]
    )
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-02")

    assert result["status"] == "success"
    assert [e["event_id"] for e in result["events"]] == ["e1"]
    assert len(service.events().list_calls) == 1


async def test_page_cap_returns_partial_results_and_warns(tool_context, monkeypatch, caplog):
    """A calendar backend that never stops returning nextPageToken must not
    loop forever — the cap kicks in, what was collected so far is returned
    rather than raised, and a structured warning is emitted (not a second
    silent truncation)."""
    monkeypatch.setattr(calendar_tool, "_MAX_EVENT_PAGES", 2)

    list_calendars, access_token = _single_calendar_service(
        {
            "connected": True,
            "needs_reauth": [],
            "calendars": [{"account_id": "acct-1", "calendar_id": "me@gmail.com"}],
        }
    )
    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)

    service = FakeCalendarService()
    service._events._pages = [
        {
            "items": [_google_item("e1", "Page one", "2026-08-01T09:00:00Z", "2026-08-01T10:00:00Z")],
            "nextPageToken": "tok2",
        },
        {
            "items": [_google_item("e2", "Page two", "2026-08-02T09:00:00Z", "2026-08-02T10:00:00Z")],
            "nextPageToken": "tok3",
        },
        {
            "items": [_google_item("e3", "Page three (never fetched)", "2026-08-03T09:00:00Z", "2026-08-03T10:00:00Z")],
        },
    ]
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    with caplog.at_level("WARNING"):
        result = await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-04")

    assert result["status"] == "success"
    assert [e["event_id"] for e in result["events"]] == ["e1", "e2"]
    assert len(service.events().list_calls) == 2
    assert any("safety cap" in record.message for record in caplog.records)


def _google_inserted(event_id, summary, start, end, html_link="https://calendar.example/e"):
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "htmlLink": html_link,
    }


async def test_add_calendar_event_needs_auth_when_nothing_connected(tool_context, monkeypatch):
    async def list_calendars(user_id):
        raise backend_client.NeedsAuth("https://connect.example/start", "not connected")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)

    result = await calendar_tool.add_calendar_event(
        tool_context, "Dinner", "2026-08-04T20:00:00-07:00", "2026-08-04T21:30:00-07:00"
    )
    assert result == {
        "status": "needs_auth",
        "connect_url": "https://connect.example/start",
        "message": "not connected",
    }


async def test_add_calendar_event_defaults_to_primary_calendar(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-work",
                    "calendar_id": "me@work.com",
                    "summary": "Work",
                    "is_primary": False,
                },
                {
                    "account_id": "acct-personal",
                    "calendar_id": "me@gmail.com",
                    "summary": "Personal",
                    "is_primary": True,
                },
            ],
        }

    async def access_token(user_id, account_id):
        return f"AT-{account_id}"

    service = FakeCalendarService(
        inserted=_google_inserted(
            "e1", "Dinner with Arian", "2026-08-04T20:00:00-07:00", "2026-08-04T21:30:00-07:00"
        ),
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Dinner with Arian",
        "2026-08-04T20:00:00-07:00",
        "2026-08-04T21:30:00-07:00",
    )

    assert result == {
        "status": "success",
        "event": {
            "event_id": "e1",
            "calendar_id": "me@gmail.com",
            "title": "Dinner with Arian",
            "start_time": "2026-08-04T20:00:00-07:00",
            "end_time": "2026-08-04T21:30:00-07:00",
            "html_link": "https://calendar.example/e",
        },
    }
    # The primary calendar's id, not the first one in the raw list — and
    # never even checked "me@work.com", since the primary was writable.
    assert service.events().insert_calls[0]["calendarId"] == "me@gmail.com"
    assert service.calendarList().get_calls == [{"calendarId": "me@gmail.com"}]


async def test_add_calendar_event_uses_named_calendar(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-personal",
                    "calendar_id": "me@gmail.com",
                    "summary": "Personal",
                    "is_primary": True,
                },
                {
                    "account_id": "acct-work",
                    "calendar_id": "me@work.com",
                    "summary": "Work",
                    "is_primary": False,
                },
            ],
        }

    async def access_token(user_id, account_id):
        return f"AT-{account_id}"

    service = FakeCalendarService(
        inserted=_google_inserted("e2", "Standup", "2026-08-05T09:00:00-07:00", "2026-08-05T09:15:00-07:00"),
        calendar_list_entries={"me@work.com": {"accessRole": "writer"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Standup",
        "2026-08-05T09:00:00-07:00",
        "2026-08-05T09:15:00-07:00",
        calendar_summary="Work",
    )

    assert result["status"] == "success"
    assert service.events().insert_calls[0]["calendarId"] == "me@work.com"


async def test_add_calendar_event_unknown_calendar_name(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-personal",
                    "calendar_id": "me@gmail.com",
                    "summary": "Personal",
                    "is_primary": True,
                }
            ],
        }

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Standup",
        "2026-08-05T09:00:00-07:00",
        "2026-08-05T09:15:00-07:00",
        calendar_summary="Nonexistent",
    )

    assert result["status"] == "not_found"


async def test_add_calendar_event_stale_account_needs_auth(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-personal",
                    "calendar_id": "me@gmail.com",
                    "summary": "Personal",
                    "is_primary": True,
                }
            ],
        }

    async def access_token(user_id, account_id):
        return None

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)

    result = await calendar_tool.add_calendar_event(
        tool_context, "Standup", "2026-08-05T09:00:00-07:00", "2026-08-05T09:15:00-07:00"
    )

    assert result["status"] == "needs_auth"


async def test_add_calendar_event_all_day_uses_date_field(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-personal",
                    "calendar_id": "me@gmail.com",
                    "summary": "Personal",
                    "is_primary": True,
                }
            ],
        }

    async def access_token(user_id, account_id):
        return "AT-1"

    service = FakeCalendarService(
        inserted={
            "id": "e3",
            "summary": "Trip",
            "start": {"date": "2026-08-10"},
            "end": {"date": "2026-08-12"},
            "htmlLink": "https://calendar.example/e3",
        },
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.add_calendar_event(
        tool_context, "Trip", "2026-08-10", "2026-08-12"
    )

    assert result["status"] == "success"
    insert_body = service.events().insert_calls[0]["body"]
    assert insert_body["start"] == {"date": "2026-08-10"}
    assert insert_body["end"] == {"date": "2026-08-12"}


async def test_add_calendar_event_naive_time_resolves_calendar_timezone(tool_context, monkeypatch):
    """No UTC offset given ('8pm', not '8pm PST') — the tool must look up the
    target calendar's own timezone rather than asking the user for one."""

    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-personal",
                    "calendar_id": "me@gmail.com",
                    "summary": "Personal",
                    "is_primary": True,
                }
            ],
        }

    async def access_token(user_id, account_id):
        return "AT-1"

    service = FakeCalendarService(
        inserted=_google_inserted(
            "e4", "Dinner with Arian", "2026-08-04T20:00:00", "2026-08-04T21:30:00"
        ),
        calendar_list_entries={
            "me@gmail.com": {"accessRole": "owner", "timeZone": "America/Los_Angeles"}
        },
    )

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.add_calendar_event(
        tool_context, "Dinner with Arian", "2026-08-04T20:00:00", "2026-08-04T21:30:00"
    )

    assert result["status"] == "success"
    assert service.calendarList().get_calls[0]["calendarId"] == "me@gmail.com"
    insert_body = service.events().insert_calls[0]["body"]
    assert insert_body["start"] == {
        "dateTime": "2026-08-04T20:00:00",
        "timeZone": "America/Los_Angeles",
    }
    assert insert_body["end"] == {
        "dateTime": "2026-08-04T21:30:00",
        "timeZone": "America/Los_Angeles",
    }


async def test_add_calendar_event_explicit_offset_ignores_calendar_timezone(tool_context, monkeypatch):
    """When the user names a specific offset, the write-access check still
    runs (it always must), but the calendar's own timeZone must not get
    stapled onto a dateTime that already carries an explicit offset."""

    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-personal",
                    "calendar_id": "me@gmail.com",
                    "summary": "Personal",
                    "is_primary": True,
                }
            ],
        }

    async def access_token(user_id, account_id):
        return "AT-1"

    service = FakeCalendarService(
        inserted=_google_inserted(
            "e5", "Call", "2026-08-04T20:00:00-07:00", "2026-08-04T20:30:00-07:00"
        ),
        calendar_list_entries={
            "me@gmail.com": {"accessRole": "owner", "timeZone": "America/Los_Angeles"}
        },
    )

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.add_calendar_event(
        tool_context, "Call", "2026-08-04T20:00:00-07:00", "2026-08-04T20:30:00-07:00"
    )

    assert result["status"] == "success"
    insert_body = service.events().insert_calls[0]["body"]
    assert insert_body["start"] == {"dateTime": "2026-08-04T20:00:00-07:00"}
    assert insert_body["end"] == {"dateTime": "2026-08-04T20:30:00-07:00"}


async def test_add_calendar_event_skips_read_only_calendar_and_falls_back(tool_context, monkeypatch):
    """Reproduces the actual bug: no calendar is flagged primary, and the
    first one in the list is a read-only subscribed calendar (Google's
    @import.calendar.google.com convention for 'subscribe from URL' feeds,
    e.g. a public holiday calendar). Must not blindly write there — must
    skip it and use the real, writable one instead."""

    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-personal",
                    "calendar_id": "abc123@import.calendar.google.com",
                    "summary": "Holidays in Sweden",
                    "is_primary": False,
                },
                {
                    "account_id": "acct-personal",
                    "calendar_id": "me@gmail.com",
                    "summary": "Personal",
                    "is_primary": False,
                },
            ],
        }

    async def access_token(user_id, account_id):
        return "AT-1"

    service = FakeCalendarService(
        inserted=_google_inserted(
            "e6", "Dinner with Arian", "2026-08-04T20:00:00-07:00", "2026-08-04T21:30:00-07:00"
        ),
        calendar_list_entries={
            "abc123@import.calendar.google.com": {
                "accessRole": "reader",
                "summary": "Holidays in Sweden",
            },
            "me@gmail.com": {"accessRole": "owner"},
        },
    )

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Dinner with Arian",
        "2026-08-04T20:00:00-07:00",
        "2026-08-04T21:30:00-07:00",
    )

    assert result["status"] == "success"
    assert service.events().insert_calls[0]["calendarId"] == "me@gmail.com"
    checked_ids = [c["calendarId"] for c in service.calendarList().get_calls]
    assert checked_ids == ["abc123@import.calendar.google.com", "me@gmail.com"]


async def test_add_calendar_event_named_calendar_read_only(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-personal",
                    "calendar_id": "holidays-se@group.v.calendar.google.com",
                    "summary": "Holidays in Sweden",
                    "is_primary": False,
                }
            ],
        }

    async def access_token(user_id, account_id):
        return "AT-1"

    service = FakeCalendarService(
        calendar_list_entries={
            "holidays-se@group.v.calendar.google.com": {"accessRole": "reader"}
        }
    )

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Some event",
        "2026-08-04T20:00:00-07:00",
        "2026-08-04T20:30:00-07:00",
        calendar_summary="Holidays in Sweden",
    )

    assert result["status"] == "not_writable"
    assert service.events().insert_calls == []


async def test_add_calendar_event_all_candidates_read_only(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-personal",
                    "calendar_id": "holidays-se@group.v.calendar.google.com",
                    "summary": "Holidays in Sweden",
                    "is_primary": False,
                },
                {
                    "account_id": "acct-personal",
                    "calendar_id": "shared-by-friend@group.calendar.google.com",
                    "summary": "Friend's calendar",
                    "is_primary": False,
                },
            ],
        }

    async def access_token(user_id, account_id):
        return "AT-1"

    service = FakeCalendarService(
        calendar_list_entries={
            "holidays-se@group.v.calendar.google.com": {
                "accessRole": "reader",
                "summary": "Holidays in Sweden",
            },
            "shared-by-friend@group.calendar.google.com": {
                "accessRole": "freeBusyReader",
                "summary": "Friend's calendar",
            },
        }
    )

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.add_calendar_event(
        tool_context, "Some event", "2026-08-04T20:00:00-07:00", "2026-08-04T20:30:00-07:00"
    )

    assert result["status"] == "not_writable"
    assert "Holidays in Sweden" in result["message"]
    assert "Friend's calendar" in result["message"]
    assert service.events().insert_calls == []


async def test_update_calendar_event_no_fields_is_an_error(tool_context):
    result = await calendar_tool.update_calendar_event(
        tool_context, "e1", "me@gmail.com"
    )
    assert result["status"] == "error"


async def test_update_calendar_event_needs_auth_when_nothing_connected(tool_context, monkeypatch):
    async def list_calendars(user_id):
        raise backend_client.NeedsAuth("https://connect.example/start", "not connected")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)

    result = await calendar_tool.update_calendar_event(
        tool_context, "e1", "me@gmail.com", summary="Renamed"
    )
    assert result == {
        "status": "needs_auth",
        "connect_url": "https://connect.example/start",
        "message": "not connected",
    }


async def test_update_calendar_event_unknown_calendar_id(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {"account_id": "acct-personal", "calendar_id": "me@gmail.com"}
            ],
        }

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)

    result = await calendar_tool.update_calendar_event(
        tool_context, "e1", "me@work.com", summary="Renamed"
    )
    assert result["status"] == "not_found"


async def test_update_calendar_event_stale_account_needs_auth(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {"account_id": "acct-personal", "calendar_id": "me@gmail.com"}
            ],
        }

    async def access_token(user_id, account_id):
        return None

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)

    result = await calendar_tool.update_calendar_event(
        tool_context, "e1", "me@gmail.com", summary="Renamed"
    )
    assert result["status"] == "needs_auth"


async def test_update_calendar_event_read_only_calendar(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {"account_id": "acct-personal", "calendar_id": "me@gmail.com"}
            ],
        }

    async def access_token(user_id, account_id):
        return "AT-1"

    service = FakeCalendarService(
        calendar_list_entries={"me@gmail.com": {"accessRole": "reader"}}
    )

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.update_calendar_event(
        tool_context, "e1", "me@gmail.com", summary="Renamed"
    )
    assert result["status"] == "not_writable"
    assert service.events().patch_calls == []


async def test_update_calendar_event_not_found(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {"account_id": "acct-personal", "calendar_id": "me@gmail.com"}
            ],
        }

    async def access_token(user_id, account_id):
        return "AT-1"

    service = FakeCalendarService(
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
        patch_error=HttpError(SimpleNamespace(status=404, reason="Not Found"), b"{}"),
    )

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.update_calendar_event(
        tool_context, "e-missing", "me@gmail.com", summary="Renamed"
    )
    assert result["status"] == "not_found"


async def test_update_calendar_event_partial_update_only_sends_changed_fields(
    tool_context, monkeypatch
):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {"account_id": "acct-personal", "calendar_id": "me@gmail.com"}
            ],
        }

    async def access_token(user_id, account_id):
        return "AT-1"

    service = FakeCalendarService(
        inserted=_google_inserted(
            "e1", "Dinner with Arian", "2026-08-04T21:00:00", "2026-08-04T22:00:00"
        ),
        calendar_list_entries={
            "me@gmail.com": {"accessRole": "owner", "timeZone": "America/Los_Angeles"}
        },
    )

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.update_calendar_event(
        tool_context, "e1", "me@gmail.com", start_time="2026-08-04T21:00:00"
    )

    assert result["status"] == "success"
    assert result["event"]["calendar_id"] == "me@gmail.com"
    patch_call = service.events().patch_calls[0]
    assert patch_call["calendarId"] == "me@gmail.com"
    assert patch_call["eventId"] == "e1"
    assert patch_call["body"] == {
        "start": {"dateTime": "2026-08-04T21:00:00", "timeZone": "America/Los_Angeles"}
    }


# ---------------------------------------------------------------------------
# Habit tagging + plan logging (add_calendar_event/update_calendar_event)
# ---------------------------------------------------------------------------


def _single_calendar(calendar_id="me@gmail.com"):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-personal",
                    "calendar_id": calendar_id,
                    "summary": "Personal",
                    "is_primary": True,
                }
            ],
        }

    return list_calendars


async def _access_token(user_id, account_id):
    return "AT-1"


async def test_add_calendar_event_with_habit_id_tags_and_logs_session(tool_context, monkeypatch):
    service = FakeCalendarService(
        inserted=_google_inserted(
            "e1", "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"
        ),
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    logged = {}

    async def upsert_habit_session(user_id, *, habit_id, event_id, calendar_id, planned_start, planned_end):
        logged["args"] = (user_id, habit_id, event_id, calendar_id, planned_start, planned_end)
        return {}

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(domain_client, "upsert_habit_session", upsert_habit_session)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Gym",
        "2026-08-04T07:00:00-07:00",
        "2026-08-04T07:30:00-07:00",
        habit_id="h1",
    )

    assert result["status"] == "success"
    insert_body = service.events().insert_calls[0]["body"]
    assert insert_body["extendedProperties"] == {"private": {"day_planner_habit_id": "h1"}}
    assert logged["args"] == (
        "user-1",
        "h1",
        "e1",
        "me@gmail.com",
        "2026-08-04T07:00:00-07:00",
        "2026-08-04T07:30:00-07:00",
    )


async def test_add_calendar_event_without_habit_id_does_not_tag_or_log(tool_context, monkeypatch):
    service = FakeCalendarService(
        inserted=_google_inserted(
            "e1", "Dinner", "2026-08-04T20:00:00-07:00", "2026-08-04T21:00:00-07:00"
        ),
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )
    called = []

    async def upsert_habit_session(*args, **kwargs):
        called.append((args, kwargs))

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(domain_client, "upsert_habit_session", upsert_habit_session)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.add_calendar_event(
        tool_context, "Dinner", "2026-08-04T20:00:00-07:00", "2026-08-04T21:00:00-07:00"
    )

    assert result["status"] == "success"
    assert "extendedProperties" not in service.events().insert_calls[0]["body"]
    assert called == []


async def test_add_calendar_event_habit_session_log_failure_is_best_effort(
    tool_context, monkeypatch
):
    """A Firestore-side logging failure must not take down event creation —
    the calendar event landing is what matters; a missed log entry just
    means one session is invisible to a future review."""
    service = FakeCalendarService(
        inserted=_google_inserted(
            "e1", "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"
        ),
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    async def upsert_habit_session(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(domain_client, "upsert_habit_session", upsert_habit_session)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Gym",
        "2026-08-04T07:00:00-07:00",
        "2026-08-04T07:30:00-07:00",
        habit_id="h1",
    )

    assert result["status"] == "success"


async def test_update_calendar_event_reschedule_of_tagged_event_updates_log(
    tool_context, monkeypatch
):
    service = FakeCalendarService(
        inserted={
            "id": "e1",
            "summary": "Gym",
            "start": {"dateTime": "2026-08-04T18:00:00-07:00"},
            "end": {"dateTime": "2026-08-04T18:30:00-07:00"},
            "htmlLink": "https://calendar.example/e1",
            "extendedProperties": {"private": {"day_planner_habit_id": "h1"}},
        },
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )
    logged = {}

    async def upsert_habit_session(user_id, *, habit_id, event_id, calendar_id, planned_start, planned_end):
        logged["args"] = (habit_id, event_id, calendar_id, planned_start, planned_end)
        return {}

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(domain_client, "upsert_habit_session", upsert_habit_session)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.update_calendar_event(
        tool_context, "e1", "me@gmail.com", start_time="2026-08-04T18:00:00-07:00"
    )

    assert result["status"] == "success"
    assert logged["args"] == (
        "h1",
        "e1",
        "me@gmail.com",
        "2026-08-04T18:00:00-07:00",
        "2026-08-04T18:30:00-07:00",
    )


async def test_update_calendar_event_summary_only_does_not_touch_habit_log(
    tool_context, monkeypatch
):
    """Renaming a tagged habit session doesn't change when it's happening —
    nothing for review_habit_week's comparison to need updated."""
    service = FakeCalendarService(
        inserted={
            "id": "e1",
            "summary": "Renamed",
            "start": {"dateTime": "2026-08-04T07:00:00-07:00"},
            "end": {"dateTime": "2026-08-04T07:30:00-07:00"},
            "htmlLink": "https://calendar.example/e1",
            "extendedProperties": {"private": {"day_planner_habit_id": "h1"}},
        },
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )
    called = []

    async def upsert_habit_session(*args, **kwargs):
        called.append((args, kwargs))

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(domain_client, "upsert_habit_session", upsert_habit_session)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.update_calendar_event(
        tool_context, "e1", "me@gmail.com", summary="Renamed"
    )

    assert result["status"] == "success"
    assert called == []


async def test_update_calendar_event_untagged_event_never_logs(tool_context, monkeypatch):
    service = FakeCalendarService(
        inserted=_google_inserted(
            "e1", "Dentist", "2026-08-04T09:00:00-07:00", "2026-08-04T09:30:00-07:00"
        ),
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )
    called = []

    async def upsert_habit_session(*args, **kwargs):
        called.append((args, kwargs))

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(domain_client, "upsert_habit_session", upsert_habit_session)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.update_calendar_event(
        tool_context, "e1", "me@gmail.com", start_time="2026-08-04T09:00:00-07:00"
    )

    assert result["status"] == "success"
    assert called == []


# ---------------------------------------------------------------------------
# A2.2 — concurrent fetches (get_calendar_events)
# ---------------------------------------------------------------------------


async def test_get_calendar_events_fetches_tokens_concurrently(tool_context, monkeypatch):
    """access_token per account must actually run concurrently, not one
    at a time — verified by tracking peak overlap, not just call count."""
    in_flight = 0
    peak = 0

    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {"account_id": "acct-1", "calendar_id": "a@gmail.com"},
                {"account_id": "acct-2", "calendar_id": "b@gmail.com"},
                {"account_id": "acct-3", "calendar_id": "c@gmail.com"},
            ],
        }

    async def access_token(user_id, account_id):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return f"AT-{account_id}"

    async def get_events(token, calendar_id, date_from, date_to):
        return []

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "_fetch_google_events", get_events)

    await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-02")

    assert peak > 1, "access_token calls ran serially, not concurrently"


async def test_get_calendar_events_fetches_events_concurrently(tool_context, monkeypatch):
    in_flight = 0
    peak = 0

    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {"account_id": "acct-1", "calendar_id": "a@gmail.com"},
                {"account_id": "acct-2", "calendar_id": "b@gmail.com"},
                {"account_id": "acct-3", "calendar_id": "c@gmail.com"},
            ],
        }

    async def access_token(user_id, account_id):
        return f"AT-{account_id}"

    async def get_events(token, calendar_id, date_from, date_to):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return [
            {
                "event_id": f"e-{calendar_id}",
                "calendar_id": calendar_id,
                "title": "x",
                "start_time": "2026-08-01T09:00:00Z",
                "end_time": "2026-08-01T09:30:00Z",
                "location": None,
            }
        ]

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "_fetch_google_events", get_events)

    result = await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-02")

    assert peak > 1, "event fetches ran serially, not concurrently"
    assert len(result["events"]) == 3


async def test_get_calendar_events_sorts_correctly_even_when_completion_order_differs(
    tool_context, monkeypatch
):
    """gather() does not guarantee completion order matches submission
    order — a calendar whose fetch happens to finish first must not end
    up first in the result just because of that."""

    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {"account_id": "acct-1", "calendar_id": "a@gmail.com"},
                {"account_id": "acct-2", "calendar_id": "b@gmail.com"},
            ],
        }

    async def access_token(user_id, account_id):
        return f"AT-{account_id}"

    async def get_events(token, calendar_id, date_from, date_to):
        # The *later* calendar (b) resolves first but has the *earlier*
        # event — sorting must still win over completion order.
        if calendar_id == "a@gmail.com":
            await asyncio.sleep(0.02)
            return [
                {
                    "event_id": "e-later",
                    "calendar_id": calendar_id,
                    "title": "Later",
                    "start_time": "2026-08-01T14:00:00Z",
                    "end_time": "2026-08-01T14:30:00Z",
                    "location": None,
                }
            ]
        return [
            {
                "event_id": "e-earlier",
                "calendar_id": calendar_id,
                "title": "Earlier",
                "start_time": "2026-08-01T09:00:00Z",
                "end_time": "2026-08-01T09:30:00Z",
                "location": None,
            }
        ]

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "_fetch_google_events", get_events)

    result = await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-02")

    assert [e["event_id"] for e in result["events"]] == ["e-earlier", "e-later"]


async def test_get_calendar_events_http_error_from_one_calendar_returns_error_status(
    tool_context, monkeypatch
):
    """Error semantics must be unchanged: one calendar's HttpError still
    surfaces as {"status": "error"}, even with the other fetch running
    concurrently alongside it via gather(return_exceptions=True)."""

    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {"account_id": "acct-1", "calendar_id": "a@gmail.com"},
                {"account_id": "acct-2", "calendar_id": "b@gmail.com"},
            ],
        }

    async def access_token(user_id, account_id):
        return f"AT-{account_id}"

    async def get_events(token, calendar_id, date_from, date_to):
        if calendar_id == "a@gmail.com":
            raise HttpError(SimpleNamespace(status=500, reason="boom"), b"{}")
        return []

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "_fetch_google_events", get_events)

    result = await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-02")

    assert result["status"] == "error"


async def test_get_calendar_events_non_http_error_is_not_swallowed(tool_context, monkeypatch):
    """Only HttpError is turned into a status:error response — anything
    else must still propagate, matching the original serial loop's
    behaviour of never catching non-HttpError exceptions at all."""

    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [{"account_id": "acct-1", "calendar_id": "a@gmail.com"}],
        }

    async def access_token(user_id, account_id):
        return "AT-1"

    async def get_events(token, calendar_id, date_from, date_to):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "_fetch_google_events", get_events)

    with pytest.raises(RuntimeError, match="unexpected"):
        await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-02")


# ---------------------------------------------------------------------------
# A2.2 — per-invocation memoization (add_calendar_event)
# ---------------------------------------------------------------------------


async def test_add_calendar_event_memoizes_list_calendars_within_one_invocation(
    tool_context, monkeypatch
):
    """The literal acceptance criterion: a turn placing 5 events performs
    1 list_calendars call, not 5."""
    calls = []

    async def list_calendars(user_id):
        calls.append(1)
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-personal",
                    "calendar_id": "me@gmail.com",
                    "summary": "Personal",
                    "is_primary": True,
                }
            ],
        }

    service = FakeCalendarService(
        inserted=_google_inserted("e1", "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"),
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    for _ in range(5):
        result = await calendar_tool.add_calendar_event(
            tool_context, "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"
        )
        assert result["status"] == "success"

    assert len(calls) == 1


async def test_add_calendar_event_memoizes_calendar_list_entry_within_one_invocation(
    tool_context, monkeypatch
):
    service = FakeCalendarService(
        inserted=_google_inserted("e1", "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"),
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    for _ in range(3):
        await calendar_tool.add_calendar_event(
            tool_context, "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"
        )

    assert service.calendarList().get_calls == [{"calendarId": "me@gmail.com"}]


async def test_add_calendar_event_shared_calendar_gets_correct_role_per_account(
    tool_context, monkeypatch
):
    """A calendar shared across two of the user's connected accounts can
    carry a different accessRole for each — accessRole is per-caller, not
    a property of the calendar itself (see _fetch_calendar_list_entry's
    own docstring). The cache must not let the first account's cached
    role silently answer the second account's lookup of the same
    calendar_id (review follow-up to A2.2)."""

    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-work",
                    "calendar_id": "shared@group.calendar.google.com",
                    "summary": "Shared",
                    "is_primary": False,
                },
                {
                    "account_id": "acct-personal",
                    "calendar_id": "shared@group.calendar.google.com",
                    "summary": "Shared",
                    "is_primary": False,
                },
            ],
        }

    async def access_token(user_id, account_id):
        return f"AT-{account_id}"

    # Same calendar_id, different accessRole depending on which account's
    # token is asking — work only reads it, personal can write to it.
    work_service = FakeCalendarService(
        calendar_list_entries={
            "shared@group.calendar.google.com": {"accessRole": "reader", "summary": "Shared"}
        }
    )
    personal_service = FakeCalendarService(
        inserted=_google_inserted(
            "e1", "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"
        ),
        calendar_list_entries={
            "shared@group.calendar.google.com": {"accessRole": "writer"}
        },
    )

    def fake_build(service_name, version, credentials):
        return work_service if credentials.token == "AT-acct-work" else personal_service

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "build", fake_build)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Gym",
        "2026-08-04T07:00:00-07:00",
        "2026-08-04T07:30:00-07:00",
        calendar_summary="Shared",
    )

    # If the cache incorrectly keyed on calendar_id alone, the personal
    # account's lookup would reuse work's cached "reader" entry, and this
    # would come back not_writable instead of success.
    assert result["status"] == "success"
    assert personal_service.events().insert_calls[0]["calendarId"] == (
        "shared@group.calendar.google.com"
    )
    assert work_service.calendarList().get_calls == [
        {"calendarId": "shared@group.calendar.google.com"}
    ]
    assert personal_service.calendarList().get_calls == [
        {"calendarId": "shared@group.calendar.google.com"}
    ]


async def test_add_calendar_event_cache_does_not_leak_across_invocations(monkeypatch):
    """Out of scope, made explicit: caching is per-invocation only. A
    second, later invocation (a new turn) must not reuse the first's
    cached list_calendars result — the user's connected calendars can
    change between turns."""
    from conftest import FakeToolContext

    calls = []

    async def list_calendars(user_id):
        calls.append(1)
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-personal",
                    "calendar_id": "me@gmail.com",
                    "summary": "Personal",
                    "is_primary": True,
                }
            ],
        }

    service = FakeCalendarService(
        inserted=_google_inserted("e1", "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"),
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    first_turn = FakeToolContext(user_id="user-1", invocation_id="inv-1")
    await calendar_tool.add_calendar_event(
        first_turn, "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"
    )
    second_turn = FakeToolContext(user_id="user-1", invocation_id="inv-2")
    await calendar_tool.add_calendar_event(
        second_turn, "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"
    )

    assert len(calls) == 2


# ---------------------------------------------------------------------------
# A2.3 — idempotency keys and retry-with-backoff on insert
# ---------------------------------------------------------------------------


async def _fast_sleep(_delay: float) -> None:
    """Replaces calendar_tool._sleep in retry tests so backoff waits don't
    actually elapse real time."""


def _http_error(status: int) -> HttpError:
    return HttpError(SimpleNamespace(status=status, reason="error"), b"{}")


async def test_add_calendar_event_habit_tagged_insert_uses_deterministic_event_id(
    tool_context, monkeypatch
):
    service = FakeCalendarService(
        inserted=_google_inserted(
            "whatever-google-echoes", "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"
        ),
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    await calendar_tool.add_calendar_event(
        tool_context,
        "Gym",
        "2026-08-04T07:00:00-07:00",
        "2026-08-04T07:30:00-07:00",
        habit_id="h1",
    )

    expected_id = calendar_tool._habit_session_event_id(
        "user-1", "h1", "me@gmail.com", "2026-08-04T07:00:00-07:00"
    )
    assert service.events().insert_calls[0]["body"]["id"] == expected_id
    # Calendar's own constraint on a caller-supplied id: base32hex, i.e.
    # lowercase a-v and 0-9 only, 5-1024 characters.
    assert 5 <= len(expected_id) <= 1024
    assert all(c in "0123456789abcdefghijklmnopqrstuv" for c in expected_id)
    # Deterministic: recomputing from the same inputs gets the same id, so
    # a retried or re-issued call for the same logical session lands on it.
    assert expected_id == calendar_tool._habit_session_event_id(
        "user-1", "h1", "me@gmail.com", "2026-08-04T07:00:00-07:00"
    )


async def test_add_calendar_event_plain_appointment_uses_distinct_random_ids_for_two_calls(
    tool_context, monkeypatch
):
    """Two separately-requested plain appointments that happen to share a
    summary, calendar and start time are two real events, not a retry of
    one another — a content-derived id would silently collapse them."""
    service = FakeCalendarService(
        insert_effects=[
            _google_inserted(
                "e1", "Drinks", "2026-08-04T20:00:00-07:00", "2026-08-04T21:00:00-07:00"
            ),
            _google_inserted(
                "e2", "Drinks", "2026-08-04T20:00:00-07:00", "2026-08-04T21:00:00-07:00"
            ),
        ],
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    first = await calendar_tool.add_calendar_event(
        tool_context, "Drinks", "2026-08-04T20:00:00-07:00", "2026-08-04T21:00:00-07:00"
    )
    second = await calendar_tool.add_calendar_event(
        tool_context, "Drinks", "2026-08-04T20:00:00-07:00", "2026-08-04T21:00:00-07:00"
    )

    assert first["status"] == "success"
    assert second["status"] == "success"
    insert_ids = [c["body"]["id"] for c in service.events().insert_calls]
    assert len(insert_ids) == 2
    assert insert_ids[0] != insert_ids[1]


async def test_add_calendar_event_retries_after_503_then_succeeds(tool_context, monkeypatch):
    service = FakeCalendarService(
        insert_effects=[
            _http_error(503),
            _google_inserted("e1", "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"),
        ],
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)
    monkeypatch.setattr(calendar_tool, "_sleep", _fast_sleep)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Gym",
        "2026-08-04T07:00:00-07:00",
        "2026-08-04T07:30:00-07:00",
        habit_id="h1",
    )

    assert result["status"] == "success"
    assert result["retry_count"] == 1
    assert len(service.events().insert_calls) == 2


async def test_add_calendar_event_retries_after_connection_error_then_succeeds(
    tool_context, monkeypatch
):
    service = FakeCalendarService(
        insert_effects=[
            ConnectionError("boom"),
            _google_inserted("e1", "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"),
        ],
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)
    monkeypatch.setattr(calendar_tool, "_sleep", _fast_sleep)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Gym",
        "2026-08-04T07:00:00-07:00",
        "2026-08-04T07:30:00-07:00",
        habit_id="h1",
    )

    assert result["status"] == "success"
    assert result["retry_count"] == 1


async def test_add_calendar_event_404_does_not_retry(tool_context, monkeypatch):
    service = FakeCalendarService(
        insert_effects=[_http_error(404)],
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)
    monkeypatch.setattr(calendar_tool, "_sleep", _fast_sleep)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Gym",
        "2026-08-04T07:00:00-07:00",
        "2026-08-04T07:30:00-07:00",
        habit_id="h1",
    )

    assert result["status"] == "error"
    assert len(service.events().insert_calls) == 1


async def test_add_calendar_event_409_with_live_existing_event_is_treated_as_success(
    tool_context, monkeypatch
):
    existing_raw = {
        "id": "some-id",
        "status": "confirmed",
        "summary": "Gym",
        "start": {"dateTime": "2026-08-04T07:00:00-07:00"},
        "end": {"dateTime": "2026-08-04T07:30:00-07:00"},
        "htmlLink": "https://calendar.example/e",
    }
    service = FakeCalendarService(
        insert_effects=[_http_error(409)],
        get_effects=[existing_raw],
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)
    monkeypatch.setattr(calendar_tool, "_sleep", _fast_sleep)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Gym",
        "2026-08-04T07:00:00-07:00",
        "2026-08-04T07:30:00-07:00",
        habit_id="h1",
    )

    assert result["status"] == "success"
    assert "retry_count" not in result
    assert len(service.events().insert_calls) == 1
    assert len(service.events().get_calls) == 1


async def test_add_calendar_event_409_with_cancelled_existing_event_mints_fresh_id_and_retries(
    tool_context, monkeypatch
):
    """Google retains a deleted event's id as a tombstone — a habit
    session re-placed at a slot it once (and no longer) occupied can 409
    against that tombstone. Reporting success there would leave the model
    believing an event exists that doesn't."""
    tombstoned = {"id": "gone", "status": "cancelled"}
    service = FakeCalendarService(
        insert_effects=[
            _http_error(409),
            _google_inserted("e2", "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"),
        ],
        get_effects=[tombstoned],
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)
    monkeypatch.setattr(calendar_tool, "_sleep", _fast_sleep)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Gym",
        "2026-08-04T07:00:00-07:00",
        "2026-08-04T07:30:00-07:00",
        habit_id="h1",
    )

    assert result["status"] == "success"
    assert result["retry_count"] == 1
    insert_ids = [c["body"]["id"] for c in service.events().insert_calls]
    deterministic_id = calendar_tool._habit_session_event_id(
        "user-1", "h1", "me@gmail.com", "2026-08-04T07:00:00-07:00"
    )
    assert insert_ids == [deterministic_id, insert_ids[1]]
    assert insert_ids[1] != deterministic_id


async def test_add_calendar_event_409_with_missing_existing_event_mints_fresh_id_and_retries(
    tool_context, monkeypatch
):
    """A 404 fetching the "existing" event behind a 409 is the same
    tombstone case as a cancelled event — the id is unusable, not proof of
    a prior success."""
    service = FakeCalendarService(
        insert_effects=[
            _http_error(409),
            _google_inserted("e2", "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"),
        ],
        get_effects=[_http_error(404)],
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)
    monkeypatch.setattr(calendar_tool, "_sleep", _fast_sleep)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Gym",
        "2026-08-04T07:00:00-07:00",
        "2026-08-04T07:30:00-07:00",
        habit_id="h1",
    )

    assert result["status"] == "success"
    assert result["retry_count"] == 1


async def test_add_calendar_event_placing_same_habit_session_twice_yields_one_event(
    tool_context, monkeypatch
):
    """The literal acceptance criterion: two separate add_calendar_event
    calls for the same habit at the same planned_start — not a retry of
    one call, two genuinely separate tool invocations — create one
    calendar event, not two."""
    live_after_first_insert = {
        "id": "e1",
        "status": "confirmed",
        "summary": "Gym",
        "start": {"dateTime": "2026-08-04T07:00:00-07:00"},
        "end": {"dateTime": "2026-08-04T07:30:00-07:00"},
        "htmlLink": "https://calendar.example/e",
    }
    service = FakeCalendarService(
        insert_effects=[
            _google_inserted("e1", "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"),
            _http_error(409),
        ],
        get_effects=[live_after_first_insert],
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)
    monkeypatch.setattr(calendar_tool, "_sleep", _fast_sleep)

    first = await calendar_tool.add_calendar_event(
        tool_context,
        "Gym",
        "2026-08-04T07:00:00-07:00",
        "2026-08-04T07:30:00-07:00",
        habit_id="h1",
    )
    second = await calendar_tool.add_calendar_event(
        tool_context,
        "Gym",
        "2026-08-04T07:00:00-07:00",
        "2026-08-04T07:30:00-07:00",
        habit_id="h1",
    )

    assert first["status"] == "success"
    assert second["status"] == "success"
    assert first["event"]["event_id"] == second["event"]["event_id"] == "e1"
    assert len(service.events().insert_calls) == 2
    insert_ids = [c["body"]["id"] for c in service.events().insert_calls]
    assert insert_ids[0] == insert_ids[1]


async def test_add_calendar_event_gives_up_after_max_attempts_on_persistent_503(
    tool_context, monkeypatch
):
    service = FakeCalendarService(
        insert_effects=[_http_error(503) for _ in range(10)],
        calendar_list_entries={"me@gmail.com": {"accessRole": "owner"}},
    )

    monkeypatch.setattr(backend_client, "list_calendars", _single_calendar())
    monkeypatch.setattr(backend_client, "access_token", _access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)
    monkeypatch.setattr(calendar_tool, "_sleep", _fast_sleep)

    result = await calendar_tool.add_calendar_event(
        tool_context,
        "Gym",
        "2026-08-04T07:00:00-07:00",
        "2026-08-04T07:30:00-07:00",
        habit_id="h1",
    )

    assert result["status"] == "error"
    assert len(service.events().insert_calls) == calendar_tool._MAX_INSERT_ATTEMPTS


# ---------------------------------------------------------------------------
# A2.6: backend failures (as opposed to NeedsAuth, an expected state)
# return {"status": "error", ...} instead of crashing the turn.
# ---------------------------------------------------------------------------


async def test_get_calendar_events_list_calendars_backend_failure(tool_context, monkeypatch):
    async def list_calendars(user_id):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)

    result = await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-02")
    assert result["status"] == "error"


async def test_get_calendar_events_access_token_backend_failure(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [{"account_id": "acct-1", "calendar_id": "me@gmail.com"}],
        }

    async def access_token(user_id, account_id):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)

    result = await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-02")
    assert result["status"] == "error"


async def test_get_calendar_events_list_calendars_programming_error_still_propagates(
    tool_context, monkeypatch
):
    """A2.6's scope item 3: only HTTP/network/auth classes are caught —
    a real bug must keep surfacing loudly."""

    async def list_calendars(user_id):
        raise TypeError("not a backend failure")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)

    with pytest.raises(TypeError):
        await calendar_tool.get_calendar_events(tool_context, "2026-08-01", "2026-08-02")


async def test_add_calendar_event_list_calendars_backend_failure(tool_context, monkeypatch):
    async def list_calendars(user_id):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)

    result = await calendar_tool.add_calendar_event(
        tool_context, "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"
    )
    assert result["status"] == "error"


async def test_add_calendar_event_access_token_backend_failure(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [{"account_id": "acct-1", "calendar_id": "me@gmail.com", "is_primary": True}],
        }

    async def access_token(user_id, account_id):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)

    result = await calendar_tool.add_calendar_event(
        tool_context, "Gym", "2026-08-04T07:00:00-07:00", "2026-08-04T07:30:00-07:00"
    )
    assert result["status"] == "error"


async def test_update_calendar_event_list_calendars_backend_failure(tool_context, monkeypatch):
    async def list_calendars(user_id):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)

    result = await calendar_tool.update_calendar_event(
        tool_context, "e1", "me@gmail.com", summary="Renamed"
    )
    assert result["status"] == "error"


async def test_update_calendar_event_access_token_backend_failure(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [{"account_id": "acct-1", "calendar_id": "me@gmail.com"}],
        }

    async def access_token(user_id, account_id):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)

    result = await calendar_tool.update_calendar_event(
        tool_context, "e1", "me@gmail.com", summary="Renamed"
    )
    assert result["status"] == "error"


async def test_delete_calendar_event_list_calendars_backend_failure(tool_context, monkeypatch):
    async def list_calendars(user_id):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)

    result = await calendar_tool.delete_calendar_event(tool_context, "e1", "me@gmail.com")
    assert result["status"] == "error"


async def test_delete_calendar_event_access_token_backend_failure(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [{"account_id": "acct-1", "calendar_id": "me@gmail.com"}],
        }

    async def access_token(user_id, account_id):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)

    result = await calendar_tool.delete_calendar_event(tool_context, "e1", "me@gmail.com")
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# resolve_reference_timezone (A4.2) — scheduling_tool.py's own reference
# for interpreting zone/sleep-schedule wall-clock times.
# ---------------------------------------------------------------------------


async def test_resolve_reference_timezone_uses_primary_calendar(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-work",
                    "calendar_id": "me@work.com",
                    "summary": "Work",
                    "is_primary": False,
                },
                {
                    "account_id": "acct-personal",
                    "calendar_id": "me@gmail.com",
                    "summary": "Personal",
                    "is_primary": True,
                },
            ],
        }

    async def access_token(user_id, account_id):
        return f"AT-{account_id}"

    service = FakeCalendarService(
        calendar_list_entries={
            "me@gmail.com": {"accessRole": "owner", "timeZone": "America/New_York"},
            "me@work.com": {"accessRole": "owner", "timeZone": "America/Los_Angeles"},
        }
    )
    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    tz = await calendar_tool.resolve_reference_timezone(tool_context, "user-1")

    assert tz == "America/New_York"
    # Never even had to check the non-primary calendar.
    assert service.calendarList().get_calls == [{"calendarId": "me@gmail.com"}]


async def test_resolve_reference_timezone_falls_back_past_a_stale_account(
    tool_context, monkeypatch
):
    async def list_calendars(user_id):
        return {
            "connected": True,
            "needs_reauth": [],
            "calendars": [
                {
                    "account_id": "acct-stale",
                    "calendar_id": "gone@gmail.com",
                    "summary": "Gone",
                    "is_primary": True,
                },
                {
                    "account_id": "acct-live",
                    "calendar_id": "me@work.com",
                    "summary": "Work",
                    "is_primary": False,
                },
            ],
        }

    async def access_token(user_id, account_id):
        return None if account_id == "acct-stale" else f"AT-{account_id}"

    service = FakeCalendarService(
        calendar_list_entries={"me@work.com": {"accessRole": "owner", "timeZone": "UTC"}}
    )
    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)
    monkeypatch.setattr(backend_client, "access_token", access_token)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    tz = await calendar_tool.resolve_reference_timezone(tool_context, "user-1")

    assert tz == "UTC"


async def test_resolve_reference_timezone_none_when_nothing_connected(tool_context, monkeypatch):
    async def list_calendars(user_id):
        return {"connected": False, "needs_reauth": [], "calendars": []}

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)

    tz = await calendar_tool.resolve_reference_timezone(tool_context, "user-1")

    assert tz is None


async def test_resolve_reference_timezone_propagates_needs_auth(tool_context, monkeypatch):
    async def list_calendars(user_id):
        raise backend_client.NeedsAuth("https://connect.example/start", "not connected")

    monkeypatch.setattr(backend_client, "list_calendars", list_calendars)

    with pytest.raises(backend_client.NeedsAuth):
        await calendar_tool.resolve_reference_timezone(tool_context, "user-1")
