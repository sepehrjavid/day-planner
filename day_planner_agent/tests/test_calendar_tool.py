"""Verifies calendar_tool's orchestration: needs_auth handling, fan-out
across multiple connected accounts, skipping stale ones without failing the
whole request, and merging/sorting events across calendars.

backend_client's own HTTP mechanics aren't re-tested here — day_planner_backend_internal's
own test suite already covers /internal/* extensively. What's tested is that
calendar_tool.py calls it correctly and handles every response shape it can
return.
"""

from types import SimpleNamespace

from googleapiclient.errors import HttpError

from day_planner_agent import backend_client, calendar_tool


class FakeEventsResource:
    """`inserted` doubles as the fixed response for both insert() and
    patch() — no test in this file exercises both on the same fake."""

    def __init__(
        self,
        items: list[dict],
        inserted: dict | None = None,
        patch_error: Exception | None = None,
    ) -> None:
        self._items = items
        self._inserted = inserted
        self._patch_error = patch_error
        self.list_calls: list[dict] = []
        self.insert_calls: list[dict] = []
        self.patch_calls: list[dict] = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return self

    def insert(self, **kwargs):
        self.insert_calls.append(kwargs)
        return self

    def patch(self, **kwargs):
        self.patch_calls.append(kwargs)
        return self

    def execute(self):
        if self.patch_calls and self._patch_error is not None:
            raise self._patch_error
        if self._inserted is not None:
            return self._inserted
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
    ) -> None:
        self._events = FakeEventsResource(items or [], inserted, patch_error)
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
    monkeypatch.setattr(backend_client, "upsert_habit_session", upsert_habit_session)
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
    monkeypatch.setattr(backend_client, "upsert_habit_session", upsert_habit_session)
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
    monkeypatch.setattr(backend_client, "upsert_habit_session", upsert_habit_session)
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
    monkeypatch.setattr(backend_client, "upsert_habit_session", upsert_habit_session)
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
    monkeypatch.setattr(backend_client, "upsert_habit_session", upsert_habit_session)
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
    monkeypatch.setattr(backend_client, "upsert_habit_session", upsert_habit_session)
    monkeypatch.setattr(calendar_tool, "build", lambda *a, **k: service)

    result = await calendar_tool.update_calendar_event(
        tool_context, "e1", "me@gmail.com", start_time="2026-08-04T09:00:00-07:00"
    )

    assert result["status"] == "success"
    assert called == []
